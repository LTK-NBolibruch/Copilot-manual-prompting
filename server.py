"""OpenAI-compatible Chat Completions API for local clients (standard library only).

    python server.py            # http://127.0.0.1:5000/v1
    python server.py --show     # watch the browser

Point any OpenAI client at base_url="http://127.0.0.1:5000/v1" with any api_key
(it is ignored - no authentication, localhost only).

Endpoints:
    GET  /v1/models
    GET  /v1/models/{id}
    POST /v1/chat/completions   (stream true/false; also accepted at POST /v1)
    GET  /health
"""
import argparse
import json
import logging
import os
import queue
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
from copilot import CopilotSession, CopilotError, LoginRequired, PromptTooLong

log = logging.getLogger("server")
MAX_BODY = 5_000_000
KEEPALIVE_SECONDS = 10
CREATED = int(time.time())
_DONE = object()  # end-of-stream sentinel


def model_obj():
    return {"id": config.MODEL_ID, "object": "model", "created": CREATED, "owned_by": "microsoft"}


def flatten_messages(messages):
    """Turn an OpenAI messages[] list into one Copilot prompt.

    Copilot has no memory between prompts (each is a fresh chat), so system
    instructions and prior turns are included as plain text.
    """
    def text_of(content):
        if isinstance(content, list):  # [{"type":"text","text":...}, {"type":"image_url",...}]
            return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        return content or ""

    msgs = [m for m in messages if isinstance(m, dict) and text_of(m.get("content")).strip()]
    if not msgs:
        raise ValueError("'messages' must contain at least one message with text content.")

    if len(msgs) == 1 and msgs[0].get("role", "user") == "user":
        return text_of(msgs[0]["content"]).strip()

    system = [text_of(m["content"]) for m in msgs if m.get("role") in ("system", "developer")]
    convo = [m for m in msgs if m.get("role") not in ("system", "developer")]
    parts = []
    if system:
        parts.append("Instructions:\n" + "\n\n".join(system))
    if len(convo) > 1:
        history = "\n\n".join(f"{m.get('role', 'user').capitalize()}: {text_of(m['content'])}" for m in convo[:-1])
        parts.append("Conversation so far:\n" + history)
    if convo:
        parts.append(("Respond to this message:\n" if parts else "") + text_of(convo[-1]["content"]))
    return "\n\n".join(parts).strip()


def error_status(exc):
    if isinstance(exc, ValueError):
        return 400, "invalid_request_error", "invalid_request"
    if isinstance(exc, PromptTooLong):
        return 400, "invalid_request_error", "context_length_exceeded"
    if isinstance(exc, LoginRequired):
        return 503, "service_unavailable", "login_required"
    if isinstance(exc, TimeoutError):
        return 504, "timeout", "timeout"
    if isinstance(exc, CopilotError):
        return 502, "api_error", "automation_failed"
    return 500, "api_error", "internal_error"


class Handler(BaseHTTPRequestHandler):
    session: CopilotSession = None
    protocol_version = "HTTP/1.1"
    server_version = "CopilotLocal/1.1"

    # ---------- helpers
    def _json(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, message, etype="invalid_request_error", code=None):
        # OpenAI error shape
        log.warning("%d %s: %s", status, code or etype, message)
        self._json(status, {"error": {"message": message, "type": etype, "param": None, "code": code}})

    def _read_body(self):
        """Read the body for both Content-Length and Transfer-Encoding: chunked requests."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            data = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass  # discard trailers
                    break
                data += self.rfile.read(size)
                self.rfile.readline()  # CRLF after each chunk
                if len(data) > MAX_BODY:
                    raise OverflowError
            return bytes(data)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise OverflowError
        return self.rfile.read(length) if length else b""

    def _path(self):
        return self.path.split("?")[0].rstrip("/")

    # ---------- methods
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = self._path()
        if p in ("/health", "/v1/health"):
            return self._json(200, {"status": "ok", "worker_alive": self.session.alive,
                                    "queue_depth": self.session.pending})
        if p == "/v1/models":
            return self._json(200, {"object": "list", "data": [model_obj()]})
        if p.startswith("/v1/models/"):
            # accept any model id so clients with hard-coded names still work
            return self._json(200, {**model_obj(), "id": p.rsplit("/", 1)[1]})
        self._error(404, f"Unknown endpoint: GET {p}", code="not_found")

    def do_POST(self):
        p = self._path()
        # Always consume the body first. With HTTP/1.1 keep-alive, an unread
        # body is left in the socket and corrupts the NEXT request on that connection.
        try:
            raw = self._read_body()
        except OverflowError:
            self.close_connection = True
            return self._error(413, "Request body too large.")

        if p not in ("/v1/chat/completions", "/chat/completions", "/v1", ""):
            return self._error(404, f"Unknown endpoint: POST {p}. Use POST /v1/chat/completions", code="not_found")
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body.get("messages"), list):
                raise ValueError("'messages' is required and must be an array.")
            prompt = flatten_messages(body["messages"])
            if len(prompt) > config.MAX_PROMPT_CHARS:
                raise ValueError(f"Prompt exceeds {config.MAX_PROMPT_CHARS} characters after flattening.")
        except json.JSONDecodeError:
            return self._error(400, "Body must be valid JSON.")
        except ValueError as e:
            return self._error(400, str(e))

        model = body.get("model") or config.MODEL_ID
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        t0 = time.time()

        if body.get("stream"):
            return self._stream(prompt, model, cid, created, body, t0)

        try:
            text = self.session.ask(prompt)
        except Exception as e:
            status, etype, code = error_status(e)
            if status == 500:
                log.exception("Unhandled error")
            return self._error(status, str(e), etype, code)

        log.info("completion %s %.1fs in=%d out=%d chars", cid, time.time() - t0, len(prompt), len(text))
        self._json(200, {
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "system_fingerprint": None,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text, "refusal": None},
                         "logprobs": None, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def _stream(self, prompt, model, cid, created, body, t0):
        """Live Server-Sent Events.

        The worker pushes each newly written piece of Copilot's reply into a
        queue as it appears on the page; this thread forwards them as
        chat.completion.chunk events, with keep-alive comments while idle.
        """
        events: "queue.Queue" = queue.Queue()
        try:
            fut = self.session.submit(prompt, on_delta=events.put)
        except Exception as e:
            status, etype, code = error_status(e)
            return self._error(status, str(e), etype, code)
        fut.add_done_callback(lambda _f: events.put(_DONE))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True

        def send(obj):
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()

        def chunk(delta, finish=None):
            send({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                  "system_fingerprint": None,
                  "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}]})

        first_token_at, sent_chars = None, 0
        try:
            chunk({"role": "assistant", "content": ""})
            while True:
                try:
                    item = events.get(timeout=KEEPALIVE_SECONDS)
                except queue.Empty:
                    # keep-alive comment so clients/proxies don't drop an idle connection
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if item is _DONE:
                    break
                if first_token_at is None:
                    first_token_at = time.time()
                sent_chars += len(item)
                chunk({"content": item})

            exc = fut.exception()
            if exc:
                status, etype, code = error_status(exc)
                log.warning("stream %s failed: %d %s: %s", cid, status, code, exc)
                send({"error": {"message": str(exc), "type": etype, "param": None, "code": code}})
            else:
                # If nothing was streamed (e.g. reply rewritten before first poll), send it whole.
                if sent_chars == 0 and fut.result():
                    chunk({"content": fut.result()})
                chunk({}, "stop")
                if (body.get("stream_options") or {}).get("include_usage"):
                    send({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                          "choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
                ttft = f"{first_token_at - t0:.1f}s" if first_token_at else "n/a"
                log.info("stream %s %.1fs first-text=%s in=%d streamed=%d final=%d chars",
                         cid, time.time() - t0, ttft, len(prompt), sent_chars, len(fut.result()))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            log.warning("client disconnected during %s (Copilot will finish the reply in the background)", cid)

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.client_address[0], fmt % args)


def main():
    ap = argparse.ArgumentParser(description="Local OpenAI-compatible API backed by M365 Copilot Chat")
    ap.add_argument("--show", action="store_true", help="show the browser window")
    ap.add_argument("--port", type=int, default=config.PORT)
    args = ap.parse_args()

    os.makedirs(config.LOG_DIR, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(os.path.join(config.LOG_DIR, "server.log"), encoding="utf-8")])

    Handler.session = CopilotSession(show_window=args.show)
    server = ThreadingHTTPServer((config.HOST, args.port), Handler)
    log.info("OpenAI-compatible API at http://%s:%d/v1  (model: %s)", config.HOST, args.port, config.MODEL_ID)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        Handler.session.close()


if __name__ == "__main__":
    main()
