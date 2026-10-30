"""Conversation store and chat reopening, no browser needed."""
import os
import sqlite3
import sys
import time

import pytest
from playwright.sync_api import TimeoutError as PWTimeout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from conversations import (ConversationStore, conversation_url, fingerprint, history_key,  # noqa: E402
                           normalize)
from copilot import CopilotSession, LoginRequired  # noqa: E402

URL = "https://copilot.cloud.microsoft/chat/conversation/1cac3cc2-202d-4db5-b298-5cc4487b8945"


@pytest.fixture(autouse=True)
def tags(monkeypatch):
    monkeypatch.setattr(config, "THINK_TAG", "think")
    monkeypatch.setattr(config, "POLL_INTERVAL", 0.001)


# ---------------------------------------------------------------- keys
def test_conversation_url():
    assert conversation_url(URL) == URL
    assert conversation_url(URL + "?foo=1#x") == URL
    assert conversation_url("https://m365.cloud.microsoft/chat/") is None
    assert conversation_url("") is None


def test_normalize_ignores_formatting_and_reasoning():
    assert normalize("<think>\nprivate\n</think>\n\n## Head-way **is** 120 s.") == "headwayis120s"
    assert fingerprint("Headway is 120 s") == fingerprint("**Headway** is\n\n120 s.")


def test_history_key_robust_but_specific():
    a = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Q1"},
         {"role": "assistant", "content": "<think>\nx\n</think>\n\n**A1**"}]
    b = [{"role": "developer", "content": "Be  brief"}, {"role": "user", "content": [{"type": "text", "text": "Q1"}]},
         {"role": "assistant", "content": "A1\n"}]
    assert history_key(a) == history_key(b)
    assert history_key(a) != history_key(a[:-1] + [{"role": "assistant", "content": "A2"}])
    assert history_key(a) != history_key([{"role": "system", "content": "Be verbose."}] + a[1:])
    assert history_key([{"role": "user", "content": "x"}]) != history_key([{"role": "assistant", "content": "x"}])


# ---------------------------------------------------------------- store
def test_store_put_get_update(tmp_path):
    s = ConversationStore(str(tmp_path / "c.db"))
    assert s.get("k") is None
    s.put("k", URL, 1, "h1", "Auto")
    assert s.get("k") == {"url": URL, "turns": 1, "reply_hash": "h1", "model": "Auto"}
    s.put("k", URL, 2, "h2")
    assert s.get("k")["turns"] == 2 and s.get("k")["model"] is None and len(s) == 1


def test_store_upgrades_old_database(tmp_path):
    """A conversations.db created by v8-v10 has no model column."""
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE conversations (key TEXT PRIMARY KEY, chat_url TEXT NOT NULL, turns INTEGER NOT NULL,
                  reply_hash TEXT NOT NULL, created REAL NOT NULL, last_used REAL NOT NULL)""")
    db.execute("INSERT INTO conversations VALUES ('k', ?, 1, 'h', ?, ?)", (URL, time.time(), time.time()))
    db.commit()
    db.close()
    s = ConversationStore(path)
    assert s.get("k") == {"url": URL, "turns": 1, "reply_hash": "h", "model": None}
    s.put("k2", URL, 1, "h", "Think deeper")
    assert s.get("k2")["model"] == "Think deeper"


def test_store_persists_and_prunes(tmp_path, monkeypatch):
    path = str(tmp_path / "c.db")
    s = ConversationStore(path)
    s.put("old", URL, 1, "h")
    s.put("new", URL, 1, "h")
    s.db.execute("UPDATE conversations SET last_used = ? WHERE key = 'old'", (time.time() - 40 * 86400,))
    s.db.commit()
    s.close()
    monkeypatch.setattr(config, "CONVERSATION_TTL_DAYS", 30)
    s2 = ConversationStore(path)
    assert s2.get("new") and s2.get("old") is None


# ---------------------------------------------------------------- reopening a chat
class NavPage:
    def __init__(self, last_reply, landed=None, load_error=None):
        self.last_reply, self.landed, self.load_error = last_reply, landed, load_error
        self.url = "about:blank"

    def goto(self, url, **kw):
        self.url = self.landed or url

    def wait_for_load_state(self, *a, **kw):
        pass

    def wait_for_function(self, *a, **kw):
        if self.load_error:
            raise self.load_error

    def evaluate(self, script, arg=None):
        return {"reasoning": "", "text": self.last_reply, "blocks": []}


def session():
    s = CopilotSession.__new__(CopilotSession)
    s.show_window, s._prompt_norm = False, ""
    return s


def resume_for(reply):
    return {"url": URL, "turns": 1, "reply_hash": fingerprint(reply), "prompt": "next"}


def test_open_existing_matches():
    assert session()._open_existing(NavPage("Headway is 120 s."), resume_for("**Headway** is 120 s")) is True


def test_open_existing_chat_moved_on():
    assert session()._open_existing(NavPage("A different, later reply."), resume_for("Headway is 120 s")) is False


def test_open_existing_redirected_elsewhere():
    page = NavPage("Headway is 120 s", landed="https://m365.cloud.microsoft/chat/")
    assert session()._open_existing(page, resume_for("Headway is 120 s")) is False


def test_open_existing_load_failure():
    page = NavPage("x", load_error=PWTimeout("no replies"))
    assert session()._open_existing(page, resume_for("x")) is False


def test_open_existing_login_required_propagates():
    page = NavPage("x")
    page.goto = lambda url, **kw: setattr(page, "url", "https://login.microsoftonline.com/abc")
    with pytest.raises(LoginRequired):
        session()._open_existing(page, resume_for("x"))
