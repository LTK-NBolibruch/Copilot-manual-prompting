"""Streaming logic tests: DeltaTracker and _wait_for_completion against a fake page."""
import os
import sys
from concurrent.futures import Future

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import copilot  # noqa: E402
from copilot import CopilotSession, DeltaTracker  # noqa: E402


def test_tracker_appends():
    t = DeltaTracker()
    assert t.feed("") == ""
    assert t.feed("Hel") == "Hel"
    assert t.feed("Hello") == "lo"
    assert t.feed("Hello") == ""
    assert t.feed("Hello world") == " world"
    assert t.sent == "Hello world" and not t.diverged


def test_tracker_pauses_on_rewrite_and_resumes():
    t = DeltaTracker()
    t.feed("Name Value")
    assert t.feed("Name\tValue") == ""          # re-rendered into a table -> paused
    assert t.diverged
    assert t.feed("Name\tValue more") == ""     # still diverged
    assert t.feed("Name Value more") == " more" # extends sent text again -> resumes
    assert not t.diverged


class FakeLocator:
    def __init__(self, page):
        self.page = page

    def count(self):
        return 1 if self.page.generating else 0


class FakePage:
    """Each evaluate() returns the next snapshot; the last one repeats."""

    def __init__(self, snapshots, generating_until=0):
        self.snapshots, self.i = snapshots, 0
        self.generating_until = generating_until
        self.generating = True

    def evaluate(self, script, arg=None):
        snap = self.snapshots[min(self.i, len(self.snapshots) - 1)]
        self.generating = self.i < self.generating_until
        self.i += 1
        return snap

    def locator(self, sel):
        return FakeLocator(self)


def make_session():
    s = CopilotSession.__new__(CopilotSession)  # skip browser start
    s.markdown = False
    return s


def fast(monkeypatch):
    monkeypatch.setattr(config, "STREAM_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(config, "POLL_INTERVAL", 0.001)
    monkeypatch.setattr(config, "STABLE_SECONDS", 0.02)
    monkeypatch.setattr(config, "REPLY_TIMEOUT", 5)


def test_wait_streams_deltas(monkeypatch):
    fast(monkeypatch)
    page = FakePage(["", "The", "The quick", "The quick brown", "The quick brown fox."], generating_until=5)
    got, fut = [], Future()
    fut.streamed_text = ""
    result = make_session()._wait_for_completion(page, fut, got.append)
    assert got == ["The", " quick", " brown", " fox."]
    assert "".join(got) == result == fut.streamed_text == "The quick brown fox."


def test_wait_waits_for_stop_button(monkeypatch):
    fast(monkeypatch)
    # text pauses for a while but Copilot is still generating -> must not finish early
    snaps = ["Part one"] * 30 + ["Part one and two"]
    page = FakePage(snaps, generating_until=31)
    fut = Future(); fut.streamed_text = ""
    got = []
    assert make_session()._wait_for_completion(page, fut, got.append) == "Part one and two"
    assert "".join(got) == "Part one and two"


def test_wait_rewrite_mid_stream(monkeypatch):
    fast(monkeypatch)
    page = FakePage(["Col A Col B", "Col A Col B 1 2", "Col A\tCol B\n1\t2"], generating_until=2)
    fut = Future(); fut.streamed_text = ""
    got = []
    result = make_session()._wait_for_completion(page, fut, got.append)
    assert "".join(got) == "Col A Col B 1 2"       # nothing garbled was emitted
    assert result == "Col A\tCol B\n1\t2"          # final reply is still complete


def test_wait_non_streaming_unchanged(monkeypatch):
    fast(monkeypatch)
    page = FakePage(["a", "ab", "abc"])
    fut = Future()
    assert make_session()._wait_for_completion(page, fut) == "abc"


def test_callback_errors_dont_break_worker(monkeypatch):
    fast(monkeypatch)
    page = FakePage(["x", "xy"])
    fut = Future(); fut.streamed_text = ""

    def bad(_):
        raise RuntimeError("client gone")
    assert make_session()._wait_for_completion(page, fut, bad) == "xy"
