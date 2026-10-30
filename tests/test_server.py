"""Tests for the OpenAI-compatible server. The browser is mocked; no sign-in needed."""
import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import Future
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import server  # noqa: E402
from conversations import ConversationStore  # noqa: E402
from copilot import CopilotError, LoginRequired, PromptTooLong  # noqa: E402

REPLY = "Hello from Copilot! " * 10
PIECES = ["<think>\nPondering.\n</think>\n\n", "## Hello", "\n\nThis is **streamed**.", "\n\n- one\n- two"]
MODELS = ["Auto", "Quick response", "Think deeper", "GPT-5.6 Sol Quick response"]


class FakeSession:
    """Simulates Copilot. Each new chat gets a conversation URL; replies are numbered.
    `chats` can be shared between sessions to simulate several tabs on one account."""

    alive, pending = True, 0

    def __init__(self, chats=None, delay=0.0):
        self.last_prompt, self.last_resume, self.last_model = None, None, None
        self.chats = {} if chats is None else chats
        self.broken = set()
        self.n = 0
        self.delay = delay
        self.names = list(MODELS)
        self.current_model = "Auto"

    def model_names(self, wait=0.0):
        return list(self.names)

    def model_keys(self):
        return {"GPT-5.6 Sol Quick response": "Gpt_5_6_Chat"}

    def close(self, timeout=15):
        self.alive = False
        return True

    def abort(self):
        self.alive = False

    def submit(self, prompt, on_delta=None, resume=None, model=None):
        self.last_prompt, self.last_resume, self.last_model = prompt, resume, model
        fut = Future()
        fut.streamed_text, fut.truncated, fut.answer_plain = "", False, ""
        fut.chat_url, fut.resumed, fut.model = None, False, None
        fut.set_running_or_notify_cancel()

        def work():
            if model:
                self.current_model = model
            fut.model = self.current_model
            if "slow" in prompt:
                time.sleep(0.3)
            time.sleep(self.delay)
            if "truncated" in prompt:
                fut.truncated = True
            if "boom" in prompt:
                return fut.set_exception(CopilotError("selector missing"))
            if "login" in prompt:
                return fut.set_exception(LoginRequired("sign in"))
            if "toolong" in prompt:
                return fut.set_exception(PromptTooLong("Copilot accepted only 10 of 20 characters"))
            if resume and resume["url"] in self.chats and resume["url"] not in self.broken:
                url, fut.resumed = resume["url"], True
                self.sent_prompt = resume["prompt"]
            else:
                url = f"https://copilot.cloud.microsoft/chat/conversation/{uuid.uuid4()}"
                self.chats[url] = 0
                self.sent_prompt = prompt
            self.chats[url] += 1
            self.n += 1
            fut.chat_url = url
            if "nodeltas" in prompt:
                fut.answer_plain = REPLY.strip()
                return fut.set_result(REPLY)
            if on_delta:
                for p in PIECES:
                    on_delta(p)
                    fut.streamed_text += p
                    time.sleep(0.02)
                fut.answer_plain = "Hello This is streamed. one two"
                return fut.set_result("".join(PIECES))
            fut.answer_plain = f"Answer number {self.n} {uuid.uuid4().hex[:6]}."
            fut.set_result(f"<think>\nthought {self.n}\n</think>\n\n{fut.answer_plain}")
        threading.Thread(target=work, daemon=True).start()
        return fut


@pytest.fixture()
def url(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MODEL_ID", "m365-copilot")
    monkeypatch.setattr(config, "STRICT_MODELS", False)
    server.Handler.session = FakeSession()
    store = ConversationStore(str(tmp_path / "conversations.db"))
    server.Handler.store = store
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    store.close()


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


def stream_chunks(body):
    return [json.loads(e) for e in sse_events(body)[:-1]]


def chat(url, messages, stream=False, model="m"):
    if stream:
        _, body = req(url, "POST", "/v1/chat/completions",
                      {"model": model, "messages": messages, "stream": True}, raw=True)
        return "".join(c["choices"][0]["delta"].get("content", "") for c in stream_chunks(body) if c["choices"])
    return req(url, "POST", "/v1/chat/completions", {"model": model, "messages": messages})[1][
        "choices"][0]["message"]["content"]


def test_completion_shape(url):
    s, b = req(url, "POST", "/v1/chat/completions", user("hi"))
    assert s == 200 and b["object"] == "chat.completion" and b["id"].startswith("chatcmpl-")
    c = b["choices"][0]
    assert c["message"]["role"] == "assistant" and "Answer" in c["message"]["content"]
    assert c["finish_reason"] == "stop" and "usage" in b and b["model"] == "m365-copilot"
    assert server.Handler.session.last_prompt == "hi"


def test_truncated_reply_finish_reason_length(url):
    s, b = req(url, "POST", "/v1/chat/completions", user("truncated"))
    assert s == 200 and b["choices"][0]["finish_reason"] == "length"
    s, body = req(url, "POST", "/v1/chat/completions", user("truncated", stream=True), raw=True)
    finishes = [c["choices"][0]["finish_reason"] for c in stream_chunks(body) if c["choices"]]
    assert finishes[-1] == "length"


def test_flatten_system_and_history(url):
    msgs = [{"role": "system", "content": "Be brief."},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
            {"role": "user", "content": [{"type": "text", "text": "And times 3?"}]}]
    req(url, "POST", "/v1/chat/completions", {"model": "x", "messages": msgs})
    p = server.Handler.session.last_prompt
    assert p.startswith("Instructions:\nBe brief.")
    assert "User: What is 2+2?" in p and "Assistant: 4" in p and p.endswith("And times 3?")


def test_think_blocks_stripped_from_history(url):
    msgs = [{"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "<think>\nprivate reasoning\n</think>\n\nA1"},
            {"role": "user", "content": "Q2"}]
    req(url, "POST", "/v1/chat/completions", {"model": "x", "messages": msgs})
    p = server.Handler.session.last_prompt
    assert "Assistant: A1" in p and "private reasoning" not in p


def test_live_streaming(url):
    s, body = req(url, "POST", "/v1/chat/completions",
                  user("hi", stream=True, stream_options={"include_usage": True}), raw=True)
    assert s == 200
    assert sse_events(body)[-1] == "[DONE]"
    chunks = stream_chunks(body)
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    contents = [c["choices"][0]["delta"].get("content") for c in chunks[1:] if c["choices"]
                and c["choices"][0]["delta"].get("content")]
    assert contents == PIECES
    assert any(c["choices"] and c["choices"][0]["finish_reason"] == "stop" for c in chunks)
    assert "usage" in chunks[-1]


def test_stream_delivers_every_delta_before_stop(url):
    s, body = req(url, "POST", "/v1/chat/completions", user("slow", stream=True), raw=True)
    chunks = [c for c in stream_chunks(body) if c["choices"]]
    finish_idx = next(i for i, c in enumerate(chunks) if c["choices"][0]["finish_reason"])
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks[:finish_idx])
    assert text == "".join(PIECES)
    assert finish_idx == len(chunks) - 1


def test_stream_chunks_arrive_incrementally(url):
    host, port = url.split("//")[1].split(":")
    conn = http.client.HTTPConnection(host, int(port))
    t0 = time.time()
    conn.request("POST", "/v1/chat/completions", json.dumps(user("slow", stream=True)),
                 {"Content-Type": "application/json"})
    r = conn.getresponse()
    first_content_at = done_at = None
    for line in r:
        line = line.decode().strip()
        if first_content_at is None and line.startswith("data: {") and "Pondering" in line:
            first_content_at = time.time() - t0
        if line == "data: [DONE]":
            done_at = time.time() - t0
            break
    assert first_content_at is not None and first_content_at < done_at - 0.03


def test_stream_falls_back_to_whole_reply(url):
    s, body = req(url, "POST", "/v1/chat/completions", user("nodeltas", stream=True), raw=True)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in stream_chunks(body) if c["choices"])
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
    b = req(url, "GET", "/health")[1]
    assert b["status"] == "ok" and b["stored_conversations"] == 0
    assert b["models"] == MODELS and b["current_model"] == "Auto" and b["port"] == int(url.rsplit(":", 1)[1])


def test_keepalive_after_404_and_system_prompt(url):
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


# ---------------------------------------------------------------- models
def test_models_list(url):
    s, b = req(url, "GET", "/v1/models")
    assert s == 200 and b["object"] == "list"
    assert [m["id"] for m in b["data"]] == ["m365-copilot", "m365-copilot-auto", "m365-copilot-quick-response",
                                            "m365-copilot-think-deeper", "m365-copilot-gpt-5-6-sol-quick-response"]
    assert b["data"][0]["name"] == "Current selection (Auto)" and b["data"][3]["name"] == "Think deeper"


def test_model_lookup(url):
    assert req(url, "GET", "/v1/models/m365-copilot-think-deeper")[1]["name"] == "Think deeper"
    assert req(url, "GET", "/v1/models/gpt-4o")[1]["id"] == "gpt-4o"


def test_model_lookup_strict(url, monkeypatch):
    monkeypatch.setattr(config, "STRICT_MODELS", True)
    assert req(url, "GET", "/v1/models/gpt-4o")[0] == 404


def test_request_selects_model(url):
    fake = server.Handler.session
    s, b = req(url, "POST", "/v1/chat/completions", {**user("hi"), "model": "m365-copilot-think-deeper"})
    assert s == 200 and fake.last_model == "Think deeper" and b["model"] == "m365-copilot-think-deeper"
    req(url, "POST", "/v1/chat/completions", {**user("hi"), "model": "Quick Response"})
    assert fake.last_model == "Quick response"
    req(url, "POST", "/v1/chat/completions", user("hi"))
    assert fake.last_model is None


def test_submenu_model_by_id_name_or_key(url):
    fake = server.Handler.session
    for m in ("m365-copilot-gpt-5-6-sol-quick-response", "GPT-5.6 Sol Quick response", "Gpt_5_6_Chat",
              "gpt-5-6-chat"):
        fake.last_model = None
        s, _ = req(url, "POST", "/v1/chat/completions", {**user("hi"), "model": m})
        assert s == 200 and fake.last_model == "GPT-5.6 Sol Quick response", m


def test_unknown_model_uses_current_selection(url):
    fake = server.Handler.session
    s, b = req(url, "POST", "/v1/chat/completions", {**user("hi"), "model": "test"})
    assert s == 200 and fake.last_model is None and b["model"] == "test"


def test_unknown_model_strict(url, monkeypatch):
    monkeypatch.setattr(config, "STRICT_MODELS", True)
    s, b = req(url, "POST", "/v1/chat/completions", {**user("hi"), "model": "gpt-4o"})
    assert s == 400 and b["error"]["code"] == "model_not_found"
    assert "m365-copilot-think-deeper" in b["error"]["message"]


def test_streaming_selects_model(url):
    chat(url, [{"role": "user", "content": "hi"}], stream=True, model="m365-copilot-auto")
    assert server.Handler.session.last_model == "Auto"


# ---------------------------------------------------------------- conversation resuming
SYSTEM = {"role": "system", "content": "You are the TrainOps assistant. " * 50}


def test_resume_sends_only_new_message(url):
    fake = server.Handler.session
    msgs = [SYSTEM, {"role": "user", "content": "Q1"}]
    a1 = chat(url, msgs)
    assert fake.last_resume is None and fake.sent_prompt.startswith("Instructions:")
    first_url = next(iter(fake.chats))
    msgs += [{"role": "assistant", "content": a1}, {"role": "user", "content": "Q2"}]
    a2 = chat(url, msgs)
    assert fake.last_resume["url"] == first_url and fake.last_resume["prompt"] == "Q2"
    assert fake.sent_prompt == "Q2"
    assert len(fake.chats) == 1 and fake.chats[first_url] == 2
    msgs += [{"role": "assistant", "content": a2}, {"role": "user", "content": "Q3"}]
    chat(url, msgs)
    assert fake.sent_prompt == "Q3" and fake.last_resume["turns"] == 2
    assert fake.chats[first_url] == 3


def test_resumed_chat_is_released_after_reply(url):
    msgs = [SYSTEM, {"role": "user", "content": "Q1"}]
    a1 = chat(url, msgs)
    msgs += [{"role": "assistant", "content": a1}, {"role": "user", "content": "Q2"}]
    chat(url, msgs)
    time.sleep(0.05)
    assert server.Handler.store._busy == set()


def test_resume_same_model_continues_other_model_starts_new_chat(url):
    fake = server.Handler.session
    msgs = [SYSTEM, {"role": "user", "content": "Q1"}]
    a1 = chat(url, msgs, model="m365-copilot-think-deeper")
    msgs += [{"role": "assistant", "content": a1}, {"role": "user", "content": "Q2"}]
    a2 = chat(url, msgs, model="m365-copilot-think-deeper")
    assert fake.sent_prompt == "Q2" and len(fake.chats) == 1
    msgs2 = msgs + [{"role": "assistant", "content": a2}, {"role": "user", "content": "Q3"}]
    chat(url, msgs2, model="m365-copilot-quick-response")
    assert fake.last_resume is None and "Conversation so far" in fake.sent_prompt


def test_resume_tolerates_client_reformatting_reply(url):
    fake = server.Handler.session
    msgs = [SYSTEM, {"role": "user", "content": "Q1"}]
    a1 = chat(url, msgs)
    reformatted = "**" + a1.split("</think>")[1].strip() + "**\n"
    msgs += [{"role": "assistant", "content": reformatted}, {"role": "user", "content": "Q2"}]
    chat(url, msgs)
    assert fake.sent_prompt == "Q2"


def test_resume_with_streaming(url):
    fake = server.Handler.session
    msgs = [SYSTEM, {"role": "user", "content": "Q1"}]
    a1 = chat(url, msgs, stream=True)
    msgs += [{"role": "assistant", "content": a1}, {"role": "user", "content": "Q2"}]
    chat(url, msgs, stream=True)
    assert fake.sent_prompt == "Q2" and len(fake.chats) == 1


def test_regenerated_or_edited_history_starts_new_chat(url):
    fake = server.Handler.session
    msgs = [SYSTEM, {"role": "user", "content": "Q1"}]
    a1 = chat(url, msgs)
    edited = msgs + [{"role": "assistant", "content": a1 + " (edited by user)"},
                     {"role": "user", "content": "Q2"}]
    chat(url, edited)
    assert fake.last_resume is None and len(fake.chats) == 2
    assert "Conversation so far" in fake.sent_prompt


def test_system_prompt_change_starts_new_chat(url):
    fake = server.Handler.session
    msgs = [SYSTEM, {"role": "user", "content": "Q1"}]
    a1 = chat(url, msgs)
    changed = [{"role": "system", "content": "Different rules."}, msgs[1],
               {"role": "assistant", "content": a1}, {"role": "user", "content": "Q2"}]
    chat(url, changed)
    assert fake.last_resume is None and len(fake.chats) == 2


def test_failed_reopen_falls_back_and_rerecords(url):
    fake = server.Handler.session
    msgs = [SYSTEM, {"role": "user", "content": "Q1"}]
    a1 = chat(url, msgs)
    fake.broken.add(next(iter(fake.chats)))
    msgs += [{"role": "assistant", "content": a1}, {"role": "user", "content": "Q2"}]
    a2 = chat(url, msgs)
    assert fake.last_resume is not None and "Conversation so far" in fake.sent_prompt
    assert len(fake.chats) == 2
    msgs += [{"role": "assistant", "content": a2}, {"role": "user", "content": "Q3"}]
    chat(url, msgs)
    new_url = [u for u in fake.chats if u not in fake.broken][0]
    assert fake.last_resume["url"] == new_url and fake.sent_prompt == "Q3"


def test_max_turns_starts_new_chat(url, monkeypatch):
    monkeypatch.setattr(config, "MAX_TURNS", 1)
    fake = server.Handler.session
    msgs = [SYSTEM, {"role": "user", "content": "Q1"}]
    a1 = chat(url, msgs)
    msgs += [{"role": "assistant", "content": a1}, {"role": "user", "content": "Q2"}]
    chat(url, msgs)
    assert fake.last_resume is None and len(fake.chats) == 2


def test_resume_disabled(url):
    server.Handler.store = None
    fake = server.Handler.session
    msgs = [SYSTEM, {"role": "user", "content": "Q1"}]
    a1 = chat(url, msgs)
    msgs += [{"role": "assistant", "content": a1}, {"role": "user", "content": "Q2"}]
    chat(url, msgs)
    assert fake.last_resume is None and len(fake.chats) == 2


# ---------------------------------------------------------------- stream always finishes
def test_stream_finishes_even_if_recording_fails(url, monkeypatch):
    def broken_put(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(server.Handler.store, "put", broken_put)
    s, body = req(url, "POST", "/v1/chat/completions", user("hi", stream=True), raw=True)
    events = sse_events(body)
    assert events[-1] == "[DONE]"
    finishes = [json.loads(e)["choices"][0]["finish_reason"] for e in events[:-1] if json.loads(e).get("choices")]
    assert finishes[-1] == "stop"


class Stuck(FakeSession):
    def submit(self, prompt, on_delta=None, resume=None, model=None):
        f = Future()
        f.set_running_or_notify_cancel()
        return f


def test_stream_ends_if_worker_never_finishes(url, monkeypatch):
    server.Handler.session = Stuck()
    monkeypatch.setattr(server, "KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(server, "result_timeout", lambda: -29.8)
    s, body = req(url, "POST", "/v1/chat/completions", user("hi", stream=True), raw=True)
    events = sse_events(body)
    assert events[-1] == "[DONE]" and json.loads(events[-2])["error"]["code"] == "timeout"


def test_stream_ends_if_worker_dies(url, monkeypatch):
    dead = Stuck()
    dead.alive = False
    server.Handler.session = dead
    monkeypatch.setattr(server, "KEEPALIVE_SECONDS", 0.05)
    s, body = req(url, "POST", "/v1/chat/completions", user("hi", stream=True), raw=True)
    events = sse_events(body)
    assert events[-1] == "[DONE]" and json.loads(events[-2])["error"]["code"] == "automation_failed"


def test_openai_sdk(url):
    openai = pytest.importorskip("openai")
    c = openai.OpenAI(base_url=url + "/v1", api_key="x")
    assert [m.id for m in c.models.list()][0] == "m365-copilot"
    r = c.chat.completions.create(model="m365-copilot", messages=[{"role": "user", "content": "hi"}])
    assert "Answer" in r.choices[0].message.content
    out = "".join(ch.choices[0].delta.content or "" for ch in
                  c.chat.completions.create(model="m", stream=True, messages=[{"role": "user", "content": "hi"}])
                  if ch.choices)
    assert out == "".join(PIECES)
