"""Tests for the OpenAI-compatible server. The browser is mocked; no sign-in needed.
Run: pytest -q
"""
import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402
from copilot import CopilotError, LoginRequired, PromptTooLong  # noqa: E402

REPLY = "Hello from Copilot! " * 10
PIECES = ["Hello ", "from ", "Copilot! ", "This ", "is ", "streamed."]


class FakeSession:
    alive, pending = True, 0

    def __init__(self):
        self.last_prompt = None

    def submit(self, prompt, on_delta=None):
        self.last_prompt = prompt
        fut = Future()
        fut.streamed_text = ""
        fut.set_running_or_notify_cancel()

        def work():
            if "slow" in prompt:
                time.sleep(0.3)
            if "boom" in prompt:
                fut.set_exception(CopilotError("selector missing"))
            elif "login" in prompt:
                fut.set_exception(LoginRequired("sign in"))
            elif "toolong" in prompt:
                fut.set_exception(PromptTooLong("Copilot accepted only 10 of 20 characters"))
            elif "nodeltas" in prompt:
                fut.set_result(REPLY)  # e.g. reply re-rendered before any delta could be sent
            elif on_delta:
                for p in PIECES:
                    on_delta(p)
                    fut.streamed_text += p
                    time.sleep(0.05)
                fut.set_result("".join(PIECES))
            else:
                fut.set_result(REPLY)
        threading.Thread(target=work, daemon=True).start()
        return fut

    def ask(self, prompt):
        return self.submit(prompt).result()


@pytest.fixture()
def url():
    server.Handler.session = FakeSession()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def req(url, method, path, payload=None, raw=False):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url + path, data, {"Content-Type": "application/json",
                                                  "Authorization": "Bearer anything"}, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            body = resp.read().decode()
            return resp.status, body if raw else json.loads(body)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def user(text, **kw):
    return {"model": "m365-copilot", "messages": [{"role": "user", "content": text}], **kw}


def sse_events(body):
    return [l[6:] for l in body.splitlines() if l.startswith("data: ")]


def test_models(url):
    s, b = req(url, "GET", "/v1/models")
    assert s == 200 and b["object"] == "list" and b["data"][0]["id"] == "m365-copilot"
    assert req(url, "GET", "/v1/models/gpt-4o")[1]["id"] == "gpt-4o"


def test_completion_shape(url):
    s, b = req(url, "POST", "/v1/chat/completions", user("hi"))
    assert s == 200 and b["object"] == "chat.completion" and b["id"].startswith("chatcmpl-")
    c = b["choices"][0]
    assert c["message"]["role"] == "assistant" and c["message"]["content"] == REPLY
    assert c["finish_reason"] == "stop" and "usage" in b
    assert server.Handler.session.last_prompt == "hi"


def test_flatten_system_and_history(url):
    msgs = [{"role": "system", "content": "Be brief."},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
            {"role": "user", "content": [{"type": "text", "text": "And times 3?"}]}]
    req(url, "POST", "/v1/chat/completions", {"model": "x", "messages": msgs})
    p = server.Handler.session.last_prompt
    assert p.startswith("Instructions:\nBe brief.")
    assert "User: What is 2+2?" in p and "Assistant: 4" in p and p.endswith("And times 3?")


def test_live_streaming(url):
    s, body = req(url, "POST", "/v1/chat/completions",
                  user("hi", stream=True, stream_options={"include_usage": True}), raw=True)
    assert s == 200
    events = sse_events(body)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    contents = [c["choices"][0]["delta"].get("content") for c in chunks[1:] if c["choices"]
                and c["choices"][0]["delta"].get("content")]
    assert contents == PIECES                       # one SSE chunk per delta, in order
    assert any(c["choices"] and c["choices"][0]["finish_reason"] == "stop" for c in chunks)
    assert "usage" in chunks[-1]


def test_stream_chunks_arrive_incrementally(url):
    """First content must arrive before the reply is finished."""
    host, port = url.split("//")[1].split(":")
    conn = http.client.HTTPConnection(host, int(port))
    t0 = time.time()
    conn.request("POST", "/v1/chat/completions", json.dumps(user("slow", stream=True)),
                 {"Content-Type": "application/json"})
    r = conn.getresponse()
    first_content_at = None
    for line in r:
        line = line.decode().strip()
        if line.startswith("data: {") and '"content": "Hello' in line:
            first_content_at = time.time() - t0
        if line == "data: [DONE]":
            done_at = time.time() - t0
            break
    assert first_content_at is not None and first_content_at < done_at - 0.15


def test_stream_falls_back_to_whole_reply(url):
    s, body = req(url, "POST", "/v1/chat/completions", user("nodeltas", stream=True), raw=True)
    chunks = [json.loads(e) for e in sse_events(body)[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == REPLY


def test_stream_error_event(url):
    s, body = req(url, "POST", "/v1/chat/completions", user("boom", stream=True), raw=True)
    events = sse_events(body)
    assert s == 200 and events[-1] == "[DONE]"
    assert json.loads(events[-2])["error"]["code"] == "automation_failed"


def test_errors_openai_shape(url):
    s, b = req(url, "POST", "/v1/chat/completions", {"model": "x"})
    assert s == 400 and b["error"]["type"] == "invalid_request_error"
    assert req(url, "POST", "/v1/chat/completions", user("  "))[0] == 400
    s, b = req(url, "POST", "/v1/chat/completions", user("boom"))
    assert s == 502 and b["error"]["code"] == "automation_failed"
    s, b = req(url, "POST", "/v1/chat/completions", user("login"))
    assert s == 503 and b["error"]["code"] == "login_required"
    s, b = req(url, "POST", "/v1/chat/completions", user("toolong"))
    assert s == 400 and b["error"]["code"] == "context_length_exceeded"
    assert req(url, "POST", "/v1/embeddings", {})[0] == 404


def test_health(url):
    assert req(url, "GET", "/health")[1]["status"] == "ok"


def test_keepalive_after_404_and_system_prompt(url):
    """An unread body on a 404 must not corrupt the next request on the same connection."""
    host, port = url.split("//")[1].split(":")
    conn = http.client.HTTPConnection(host, int(port))
    payload = json.dumps({"model": "test", "stream": False, "temperature": 0.7, "top_k": 40,
                          "repetition_penalty": 1.1,
                          "messages": [{"role": "system", "content": "Never apologize, just do."},
                                       {"role": "user", "content": "hi"}]})
    conn.request("POST", "/v1/nope", payload, {"Content-Type": "application/json"})
    r = conn.getresponse(); r.read()
    assert r.status == 404
    conn.request("POST", "/v1/chat/completions", payload, {"Content-Type": "application/json"})
    r = conn.getresponse()
    assert r.status == 200 and json.loads(r.read())["choices"][0]["message"]["content"]
    p = server.Handler.session.last_prompt
    assert p.startswith("Instructions:\nNever apologize, just do.") and p.endswith("hi")
    conn.request("POST", "/v1", payload, {"Content-Type": "application/json"})
    r = conn.getresponse(); r.read()
    assert r.status == 200


def test_chunked_body_with_long_system_prompt(url):
    host, port = url.split("//")[1].split(":")
    payload = json.dumps({"model": "test", "stream": False, "top_k": 1,
                          "messages": [{"role": "system", "content": "Rule. " * 8000},
                                       {"role": "user", "content": "what do you know about trainops?"}]}).encode()

    def chunks():
        for i in range(0, len(payload), 4096):
            yield payload[i:i + 4096]
    conn = http.client.HTTPConnection(host, int(port))
    conn.request("POST", "/v1/chat/completions", chunks(), {"Content-Type": "application/json"},
                 encode_chunked=True)
    r = conn.getresponse()
    assert r.status == 200, r.read()
    r.read()
    p = server.Handler.session.last_prompt
    assert p.startswith("Instructions:\nRule.") and p.endswith("what do you know about trainops?")


def test_openai_sdk(url):
    openai = pytest.importorskip("openai")
    c = openai.OpenAI(base_url=url + "/v1", api_key="x")
    r = c.chat.completions.create(model="m365-copilot", messages=[{"role": "user", "content": "hi"}])
    assert r.choices[0].message.content == REPLY
    out = "".join(ch.choices[0].delta.content or "" for ch in
                  c.chat.completions.create(model="m", stream=True, messages=[{"role": "user", "content": "hi"}])
                  if ch.choices)
    assert out == "".join(PIECES)
