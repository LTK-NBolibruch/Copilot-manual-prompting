"""Local store that maps OpenAI-style conversation histories to Copilot chats.

Key   = hash of the conversation so far (every message, normalized).
Value = the Copilot chat URL holding that conversation, its turn count, a
        hash of the last reply (used to check the chat is still in that
        state) and the model the chat was started with.

Only hashes, URLs and model names are stored - no prompt or reply text (unless
COPILOT_LOG_CONVERSATIONS=1, which appends full text to logs/conversations.jsonl).

Normalization makes matching robust to how clients store replies: reasoning
blocks (<THINK_TAG>...</THINK_TAG>) are removed and only letters/digits are
kept, lower-cased, so whitespace, punctuation and markdown changes don't matter.

Several sessions (manager.py) share one store. claim()/release() make sure two
sessions never continue the same Copilot chat at the same time.
"""
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time

import config

log = logging.getLogger("conversations")

CONVERSATION_URL_RE = re.compile(r"^(https?://[^?#]+?/chat/conversation/[0-9a-fA-F-]{36})")


def conversation_url(url: str):
    """The canonical chat URL if `url` is a Copilot conversation page, else None."""
    m = CONVERSATION_URL_RE.match(url or "")
    return m.group(1) if m else None


def text_of(content) -> str:
    if isinstance(content, list):  # [{"type":"text","text":...}, {"type":"image_url",...}]
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return content or ""


def strip_reasoning(text: str) -> str:
    tag = re.escape(config.THINK_TAG)
    return re.sub(rf"<{tag}>.*?</{tag}>\s*", "", text or "", flags=re.S)


def normalize(text: str) -> str:
    return "".join(ch.lower() for ch in strip_reasoning(text) if ch.isalnum())


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def _role(m) -> str:
    r = m.get("role", "user")
    return "system" if r in ("system", "developer") else r


def history_key(messages) -> str:
    """Hash of a conversation; messages without text are ignored."""
    canon = [[_role(m), normalize(text_of(m.get("content")))]
             for m in messages if isinstance(m, dict)]
    canon = [c for c in canon if c[1]]
    return hashlib.sha256(json.dumps(canon).encode()).hexdigest()


class ConversationStore:
    def __init__(self, path=None):
        self.path = path or config.CONVERSATION_DB
        self.lock = threading.Lock()
        self._busy: set[str] = set()       # chat URLs being continued right now
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS conversations (
                             key TEXT PRIMARY KEY, chat_url TEXT NOT NULL, turns INTEGER NOT NULL,
                             reply_hash TEXT NOT NULL, created REAL NOT NULL, last_used REAL NOT NULL,
                             model TEXT)""")
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(conversations)")]
        if "model" not in cols:  # database created by v8-v10
            self.db.execute("ALTER TABLE conversations ADD COLUMN model TEXT")
        self.db.commit()
        self.prune()

    def get(self, key):
        with self.lock:
            row = self.db.execute("SELECT chat_url, turns, reply_hash, model FROM conversations WHERE key = ?",
                                  (key,)).fetchone()
            if row:
                self.db.execute("UPDATE conversations SET last_used = ? WHERE key = ?", (time.time(), key))
                self.db.commit()
        return {"url": row[0], "turns": row[1], "reply_hash": row[2], "model": row[3]} if row else None

    def put(self, key, chat_url, turns, reply_hash, model=None):
        now = time.time()
        with self.lock:
            self.db.execute("""INSERT INTO conversations (key, chat_url, turns, reply_hash, created, last_used, model)
                               VALUES (?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(key) DO UPDATE SET chat_url=excluded.chat_url, turns=excluded.turns,
                               reply_hash=excluded.reply_hash, last_used=excluded.last_used, model=excluded.model""",
                            (key, chat_url, turns, reply_hash, now, now, model))
            self.db.commit()

    def claim(self, chat_url) -> bool:
        """Reserve a chat for one request. False if another request is already continuing it."""
        with self.lock:
            if chat_url in self._busy:
                return False
            self._busy.add(chat_url)
            return True

    def release(self, chat_url):
        with self.lock:
            self._busy.discard(chat_url)

    def prune(self):
        cutoff = time.time() - config.CONVERSATION_TTL_DAYS * 86400
        with self.lock:
            n = self.db.execute("DELETE FROM conversations WHERE last_used < ?", (cutoff,)).rowcount
            self.db.commit()
        if n:
            log.info("Pruned %d conversation(s) unused for %s days", n, config.CONVERSATION_TTL_DAYS)

    def __len__(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]

    def close(self):
        with self.lock:
            self.db.close()


def log_conversation(messages, reply, chat_url, resumed):
    """Optional full-text log (COPILOT_LOG_CONVERSATIONS=1). Contains prompt and reply text."""
    if not config.LOG_CONVERSATIONS:
        return
    os.makedirs(config.LOG_DIR, exist_ok=True)
    with open(os.path.join(config.LOG_DIR, "conversations.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.time(), "chat_url": chat_url, "resumed": resumed,
                            "messages": messages, "reply": reply}) + "\n")
