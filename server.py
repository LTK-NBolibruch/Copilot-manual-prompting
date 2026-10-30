"""OpenAI-compatible Chat Completions API for local clients (standard library only).

    python server.py            # http://127.0.0.1:5000/v1
    python server.py --show     # watch the browser

Point any OpenAI client at base_url="http://127.0.0.1:5000/v1" with any api_key
(it is ignored - no authentication, localhost only).

Endpoints:
    GET  /v1/models             models in Copilot's model picker
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
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
from conversations import (ConversationStore, fingerprint, history_key, log_conversation, strip_reasoning,
                           text_of)
from copilot import CopilotSession, CopilotError, LoginRequired, PromptTooLong
from models import model_id, resolve

log = logging.getLogger("server")
MAX_BODY = 5_000_000
KEEPALIVE_SECONDS = 10
CREATED = int(time.time())
_DONE = object()  # end-of-stream sentinel


def model_obj(mid, name=None):
    obj = {"id": mid, "object": "model", "created": CREATED, "owned_by": "microsoft"}
    if name:
        obj["name"] = name
    return obj


def clean_messages(messages):
    """Messages with text content; earlier replies' reasoning blocks removed."""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            m = {**m, "content": strip_reasoning(text_of(m.get("content")))}
        if text_of(m.get("content")).strip():
            out.append(m)
    return out


def flatten_messages(messages):
    """Turn an OpenAI messages[] list into one Copilot prompt for a NEW chat.

    System instructions and prior turns are included as plain text.
    """
    msgs = clean_messages(messages)
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
    if isinstance(exc, (TimeoutError, FutureTimeout)):
        return 504, "timeout", "timeout"
    if isinstance(exc, CopilotError):
        return 502, "api_error", "automation_failed"
    return 500, "api_error", "internal_error"


def finish_reason(fut):
    """'length' if Copilot was still writing when COPILOT_REPLY_TIMEOUT ran out."""
    return "length" if getattr(fut, "truncated", False) else "stop"


def result_timeout():
    return (config.REPLY_TIMEOUT + config.START_TIMEOUT + config.INPUT_TIMEOUT
            + config.RESUME_LOAD_TIMEOUT + 2 * config.MODEL_TIMEOUT + 120)


class Handler(BaseHTTPRequestHandler):
    session: CopilotSession = None
    store: ConversationStore = None   # None = resuming disabled
    protocol_version = "HTTP/1.1"
    server_version = "CopilotLocal/1.9"

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

    # ---------- models
    def _model_names(self):
        return self.session.model_names(wait=config.MODEL_TIMEOUT * 3)

    def _models_list(self):
        names = self._model_names()
        current = self.session.current_model
        data = [model_obj(config.MODEL_ID, f"Current selection ({current})" if current else "Current selection")]
        data += [model_obj(model_id(n), n) for n in names]
        return data

    def _resolve_model(self, requested):
        """(picker name or None, error message or None) for a request's "model"."""
        names = self._model_names()
        name, known = resolve(requested, names, self.session.model_keys())
        if known:
            return name, None
        valid = [config.MODEL_ID] + [model_id(n) for n in names]
        if config.STRICT_MODELS:
            return None, f"Unknown model {requested!r}. Available: {', '.join(valid)}"
        log.warning("Unknown model %r - using the current selection. Available: %s", requested, ", ".join(valid))
        return None, None

    # ---------- conversation resuming
    def _plan_resume(self, messages, model=None):
        """resume dict for CopilotSession.submit(), or None to start a new chat."""
        if self.store is None:
            return None
        msgs = clean_messages(messages)
        if len(msgs) < 3 or msgs[-1].get("role", "user") != "user" or msgs[-2].get("role") != "assistant":
            return None
        hit = self.store.get(history_key(msgs[:-1]))
        if not hit:
            return None
        if hit["turns"] >= config.MAX_TURNS:
            log.info("Chat %s reached COPILOT_MAX_TURNS=%d - starting a new chat", hit["url"], config.MAX_TURNS)
            return None
        if model and hit.get("model") and hit["model"].lower() != model.lower():
            log.info("Chat %s was answered by %r, this request asks for %r - starting a new chat",
                     hit["url"], hit["model"], model)
            return None
        return {**hit, "prompt": text_of(msgs[-1]["content"]).strip()}

    def _record(self, messages, fut, client_text, resume):
        """Remember which Copilot chat now holds messages + this reply. Never raises: a failure
        here must not stop the reply reaching the client."""
        try:
            reply = client_text or ""
            log_conversation(messages, reply, getattr(fut, "chat_url", None), getattr(fut, "resumed", False))
            if self.store is None or not getattr(fut, "chat_url", None) or not getattr(fut, "answer_plain", ""):
                return
            turns = resume["turns"] + 1 if getattr(fut, "resumed", False) and resume else 1
            history = clean_messages(messages)
            self.store.put(history_key(history + [{"role": "assistant", "content": reply}]),
                           fut.chat_url, turns, fingerprint(fut.answer_plain), getattr(fut, "model", None))
        except Exception:
            log.exception("Could not record the conversation (the reply was still sent)")

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
                                    "queue_depth": self.session.pending,
                                    "current_model": self.session.current_model,
                                    "models": self.session.model_names(),
                                    "stored_conversations": len(self.store) if self.store is not None else None})
        if p == "/v1/models":
            return self._json(200, {"object": "list", "data": self._models_list()})
        if p.startswith("/v1/models/"):
            mid = p.rsplit("/", 1)[1]
            for m in self._models_list():
                if m["id"] == mid:
                    return self._json(200, m)
            name, known = resolve(mid, self.session.model_names(), self.session.model_keys())
            if known and name:
                return self._json(200, model_obj(model_id(name), name))
            if config.STRICT_MODELS:
                return self._error(404, f"Unknown model {mid!r}.", code="model_not_found")
            # accept any model id so clients with hard-coded names still work
            return self._json(200, model_obj(mid))
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
            messages = body["messages"]
            prompt = flatten_messages(messages)
            if len(prompt) > config.MAX_PROMPT_CHARS:
                raise ValueError(f"Prompt exceeds {config.MAX_PROMPT_CHARS} characters after flattening.")
        except json.JSONDecodeError:
            return self._error(400, "Body must be valid JSON.")
        except ValueError as e:
            return self._error(400, str(e))

        requested = body.get("model") or config.MODEL_ID
        select, err = self._resolve_model(requested)
        if err:
            return self._error(400, err, code="model_not_found")
        resume = self._plan_resume(messages, select)
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        t0 = time.time()

        if body.get("stream"):
            return self._stream(prompt, resume, select, messages, requested, cid, created, body, t0)

        try:
            fut = self.session.submit(prompt, resume=resume, model=select)
            text = fut.result(timeout=result_timeout())
        except Exception as e:
            status, etype, code = error_status(e)
            if status == 500:
                log.exception("Unhandled error")
            return self._error(status, str(e), etype, code)

        self._record(messages, fut, text, resume)
        reason = finish_reason(fut)
        log.info("completion %s %.1fs in=%d out=%d chars finish=%s %s model=%s",
                 cid, time.time() - t0, len(prompt), len(text), reason,
                 "resumed" if getattr(fut, "resumed", False) else "new-chat", getattr(fut, "model", None))
        self._json(200, {
            "id": cid, "object": "chat.completion", "created": created, "model": requested,
            "system_fingerprint": None,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text, "refusal": None},
                         "logprobs": None, "finish_reason": reason}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def _stream(self, prompt, resume, select, messages, model, cid, created, body, t0):
        """Live Server-Sent Events.

        The worker pushes each new piece of Copilot's reply into a queue as it
        appears on the page and, when the reply ends, whatever the client has
        not received yet. This thread forwards them as chat.completion.chunk
        events, with keep-alive comments while idle. The stream always ends
        with a final chunk or an error event, then [DONE].
        """
        events: "queue.Queue" = queue.Queue()
        try:
            fut = self.session.submit(prompt, on_delta=events.put, resume=resume, model=select)
        except Exception as e:
            status, etype, code = error_status(e)
            return self._error(status, str(e), etype, code)
        # Runs after the worker's last on_delta call, so _DONE is always queued last.
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

        first_token_at, sent = None, []
        deadline = t0 + result_timeout() + 30
        try:
            try:
                chunk({"role": "assistant", "content": ""})
                while True:
                    try:
                        item = events.get(timeout=KEEPALIVE_SECONDS)
                    except queue.Empty:
                        if time.time() > deadline:
                            raise TimeoutError("No end of reply from the browser worker in time.")
                        if not self.session.alive:
                            raise CopilotError("The browser worker stopped; restart the server.")
                        # keep-alive comment so clients/proxies don't drop an idle connection
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    if item is _DONE:
                        break
                    if first_token_at is None:
                        first_token_at = time.time()
                    sent.append(item)
                    chunk({"content": item})

                exc = fut.exception(timeout=0) if fut.done() else TimeoutError("Reply did not finish.")
                if exc:
                    raise exc
                # If nothing was streamed, send the whole reply at once.
                if not sent and fut.result():
                    sent.append(fut.result())
                    chunk({"content": fut.result()})
                client_text = "".join(sent)
                self._record(messages, fut, client_text, resume)
                reason = finish_reason(fut)
                chunk({}, reason)
                if (body.get("stream_options") or {}).get("include_usage"):
                    send({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                          "choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
                ttft = f"{first_token_at - t0:.1f}s" if first_token_at else "n/a"
                log.info("stream %s %.1fs first-text=%s in=%d streamed=%d final=%d chars finish=%s %s model=%s",
                         cid, time.time() - t0, ttft, len(prompt), len(client_text), len(fut.result()), reason,
                         "resumed" if getattr(fut, "resumed", False) else "new-chat", getattr(fut, "model", None))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                raise
            except Exception as exc:  # anything else: tell the client, then end the stream properly
                status, etype, code = error_status(exc)
                if status == 500:
                    log.exception("stream %s: unexpected error", cid)
                else:
                    log.warning("stream %s failed: %d %s: %s", cid, status, code, exc)
                send({"error": {"message": str(exc), "type": etype, "param": None, "code": code}})
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

    Handler.store = ConversationStore() if config.RESUME_CONVERSATIONS else None
    Handler.session = CopilotSession(show_window=args.show)
    server = ThreadingHTTPServer((config.HOST, args.port), Handler)
    log.info("OpenAI-compatible API at http://%s:%d/v1  (resume chats: %s)", config.HOST, args.port,
             "on" if Handler.store is not None else "off")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        Handler.session.close()
        if Handler.store is not None:
            Handler.store.close()


if __name__ == "__main__":
    main()
