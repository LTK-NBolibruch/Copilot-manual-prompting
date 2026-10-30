"""Reply-reading and streaming tests against a simulated Copilot page (no browser needed).

The browser-side SNAPSHOT_JS is not executed here; FakePage returns the
{reasoning, text, blocks} snapshots it would produce.
"""
import os
import sys
from concurrent.futures import Future

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import copilot  # noqa: E402
from copilot import BlockStreamer, CopilotSession, DeltaTracker, extend, remainder  # noqa: E402

needs_md = pytest.mark.skipif(copilot._to_md is None, reason="markdownify not installed")


# ---------------------------------------------------------------- unit: alignment
def test_extend_exact_and_formatting_insensitive():
    assert extend("", "abc") == "abc"
    assert extend("Hello", "Hello world") == " world"
    assert extend("One", "One changed") == " changed"
    assert extend("Time between trains.", "Time between **trains**. Next") == " Next"
    assert extend("Hello world", "Goodbye") is None


def test_remainder_after_rewrite_of_sent_text():
    assert remainder("One\n\nTwo", "One with citation\n\nTwo\n\nThree") == "\n\nThree"
    assert remainder("Col A Col B 1 2", "| Col A | Col B |\n|---|---|\n| 1 | 2 |\n\nAfter table") == "\n\nAfter table"
    assert remainder("Same", "Same") == ""


def test_remainder_unalignable_returns_none():
    assert remainder("completely different text here", "nothing in common at all, really") is None


# ---------------------------------------------------------------- unit: trackers
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
    assert t.feed("Name\tValue") == ""
    assert t.diverged
    assert t.feed("Name Value more") == " more"
    assert not t.diverged


def test_block_streamer_holds_back_last_block():
    b = BlockStreamer()
    assert b.update(["# Title"]) == ""
    assert b.update(["# Title", "Para"]) == "# Title"
    assert b.update(["# Title", "Para one"]) == ""
    assert b.update(["# Title", "Para one", "- a"]) == "\n\nPara one"
    assert b.update(["# Title", "Para one", "- a\n- b"], final=True) == "\n\n- a\n- b"
    assert b.sent == "# Title\n\nPara one\n\n- a\n- b"


def test_block_streamer_pauses_when_sent_text_rewritten():
    b = BlockStreamer()
    b.update(["One", "Two", "x"])
    assert b.update(["Totally new", "Two", "Three", "x"]) == ""
    assert b.diverged


# ---------------------------------------------------------------- fake page
class FakeLocator:
    def __init__(self, page):
        self.page = page

    def count(self):
        return 1 if self.page.generating else 0


class FakePage:
    """Each evaluate() returns the next snapshot; the last one repeats.

    A snapshot is a list of HTML blocks, a plain string, or
    {"reasoning": str, "answer": str or [blocks]}.
    generating_until: number of polls during which the Stop button is visible.
    """

    def __init__(self, snapshots, generating_until=0):
        self.snapshots, self.i = snapshots, 0
        self.generating_until = generating_until
        self.generating = True

    def evaluate(self, script, arg=None):
        snap = self.snapshots[min(self.i, len(self.snapshots) - 1)]
        self.generating = self.i < self.generating_until
        self.i += 1
        if isinstance(snap, dict):
            ans = snap.get("answer", "")
            blocks = ans if isinstance(ans, list) else ([f"<p>{ans}</p>"] if ans else [])
            text = "\n".join(ans) if isinstance(ans, list) else ans
            return {"reasoning": snap.get("reasoning", ""), "text": text, "blocks": blocks}
        if isinstance(snap, str):
            return {"text": snap, "blocks": [f"<p>{snap}</p>"] if snap else []}
        return {"text": "\n".join(snap), "blocks": snap}

    def locator(self, sel):
        return FakeLocator(self)


class EndlessPage(FakePage):
    """Reply that never stops changing."""

    def __init__(self):
        super().__init__([""], generating_until=10 ** 9)

    def evaluate(self, script, arg=None):
        self.i += 1
        self.generating = True
        t = "word " * self.i
        return {"text": t, "blocks": [f"<p>{t}</p>"]}


def make_session(markdown=False):
    s = CopilotSession.__new__(CopilotSession)  # skip browser start
    s.markdown = markdown
    s._md_cache = {}
    s._warned_no_stop = False
    s._prompt_norm = ""
    return s


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(config, "STREAM_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(config, "POLL_INTERVAL", 0.001)
    monkeypatch.setattr(config, "STABLE_SECONDS", 0.02)
    monkeypatch.setattr(config, "STABLE_SECONDS_NO_STOP", 0.2)
    monkeypatch.setattr(config, "STUCK_SECONDS", 1.0)
    monkeypatch.setattr(config, "REPLY_TIMEOUT", 5)
    monkeypatch.setattr(config, "THINK_TAG", "think")
    monkeypatch.setattr(config, "INCLUDE_REASONING", True)
    monkeypatch.setattr(config, "DEBUG_REPLIES", False)


def new_future():
    f = Future()
    f.streamed_text, f.truncated, f.answer_plain = "", False, ""
    f.chat_url, f.resumed = None, False
    return f


def run(page, mode, monkeypatch, markdown=False):
    monkeypatch.setattr(config, "STREAM_MODE", mode)
    got, fut = [], new_future()
    result = make_session(markdown)._wait_for_completion(page, fut, got.append)
    return got, result, fut


# ---------------------------------------------------------------- markdown mode
GROWING_REPLY = [
    ["<h2>Head"],
    ["<h2>Headway</h2>", "<p>Time between **tr"],
    ["<h2>Headway</h2>", "<p>Time between <strong>trains</strong>.</p>"],
    ["<h2>Headway</h2>", "<p>Time between <strong>trains</strong>.</p>",
     "<table><tr><th>Branch</th><th>Value</th></tr></table>"],
    ["<h2>Headway</h2>", "<p>Time between <strong>trains</strong>.</p>",
     "<table><tr><th>Branch</th><th>Value</th></tr><tr><td>T0</td><td>120 s</td></tr></table>"],
    ["<h2>Headway</h2>", "<p>Time between <strong>trains</strong>.</p>",
     "<table><tr><th>Branch</th><th>Value</th></tr><tr><td>T0</td><td>120 s</td></tr></table>",
     "<p>Done.</p>"],
]


@needs_md
def test_markdown_stream_matches_final(monkeypatch):
    got, result, fut = run(FakePage(GROWING_REPLY, generating_until=6), "markdown", monkeypatch)
    streamed = "".join(got)
    assert streamed == result == fut.streamed_text
    assert result.startswith("## Headway\n\nTime between **trains**.")
    assert "| T0 | 120 s |" in result and result.endswith("Done.")
    assert "\\*" not in streamed            # half-rendered (escaped) ** never sent


@needs_md
def test_markdown_non_streaming_same_output(monkeypatch):
    result = make_session(markdown=True)._wait_for_completion(FakePage(GROWING_REPLY, 6), new_future())
    got, streamed_result, _ = run(FakePage(GROWING_REPLY, 6), "markdown", monkeypatch)
    assert result == streamed_result == "".join(got)


@needs_md
def test_markdown_tail_sent_when_block_structure_collapses(monkeypatch):
    snaps = [["<p>Alpha one</p>", "<p>Beta two</p>", "<p>Gamma thr"],
             ["<p>Alpha one</p>", "<p>Beta two</p>", "<p>Gamma three</p>", "<p>Delta"],
             ["<div><p>Alpha one</p><p>Beta two</p><p>Gamma three</p><p>Delta four, the end.</p></div>"]]
    got, result, _ = run(FakePage(snaps, generating_until=3), "markdown", monkeypatch)
    streamed = "".join(got)
    assert "Delta four, the end." in streamed
    assert streamed.count("Alpha one") == 1


@needs_md
def test_markdown_sent_block_changed_later_tail_still_sent(monkeypatch):
    snaps = [["<p>One</p>", "<p>Two</p>", "<p>x"],
             ["<p>One with a citation</p>", "<p>Two</p>", "<p>Three final</p>"]]
    got, result, _ = run(FakePage(snaps, generating_until=2), "markdown", monkeypatch)
    streamed = "".join(got)
    assert streamed.startswith("One\n\nTwo") and streamed.endswith("Three final")
    assert streamed.count("Two") == 1


@needs_md
def test_markdown_conversion_losing_content_falls_back_to_plain(monkeypatch):
    class LossyPage(FakePage):
        def evaluate(self, script, arg=None):
            self.i += 1
            self.generating = False
            text = "Intro paragraph. " + "Important detail that must not be lost. " * 10
            return {"text": text, "blocks": ["<p>Intro paragraph.</p>"]}
    result = make_session(markdown=True)._wait_for_completion(LossyPage([""]), new_future())
    assert result.count("Important detail") == 10


def test_markdown_mode_falls_back_to_text_without_markdownify(monkeypatch):
    monkeypatch.setattr(copilot, "_to_md", None)
    got, result, _ = run(FakePage(["a", "ab", "abc"], generating_until=3), "markdown", monkeypatch)
    assert "".join(got) == result == "abc"


# ---------------------------------------------------------------- completion detection
def test_waits_for_stop_button(monkeypatch):
    snaps = ["Part one"] * 30 + ["Part one and two"]
    got, result, _ = run(FakePage(snaps, generating_until=31), "text", monkeypatch)
    assert result == "Part one and two" == "".join(got)


def test_pause_without_stop_button_does_not_end_reply(monkeypatch):
    snaps = ["Part one"] * 60 + ["Part one and part two."]
    got, result, fut = run(FakePage(snaps, generating_until=0), "text", monkeypatch)
    assert result == "Part one and part two." == "".join(got)
    assert not fut.truncated


def test_stuck_stop_button_eventually_finishes(monkeypatch):
    got, result, fut = run(FakePage(["done"], generating_until=10 ** 9), "text", monkeypatch)
    assert result == "done" and not fut.truncated


def test_hard_timeout_marks_truncated(monkeypatch):
    monkeypatch.setattr(config, "REPLY_TIMEOUT", 0.1)
    got, result, fut = run(EndlessPage(), "text", monkeypatch)
    assert fut.truncated and result and "".join(got).strip() == result


def test_answer_plain_recorded(monkeypatch):
    got, result, fut = run(FakePage(["The", "The answer."], generating_until=2), "text", monkeypatch)
    assert fut.answer_plain == "The answer."


# ---------------------------------------------------------------- text mode
def test_text_mode_streams_deltas(monkeypatch):
    page = FakePage(["", "The", "The quick", "The quick brown", "The quick brown fox."], generating_until=5)
    got, result, fut = run(page, "text", monkeypatch)
    assert got == ["The", " quick", " brown", " fox."]
    assert "".join(got) == result == fut.streamed_text


def test_text_mode_rewrite_mid_stream_still_sends_tail(monkeypatch):
    page = FakePage(["Col A Col B", "Col A Col B 1 2",
                     "Col A\tCol B\n1\t2\n\nText after the table that must arrive."], generating_until=2)
    got, result, _ = run(page, "text", monkeypatch)
    streamed = "".join(got)
    assert streamed.startswith("Col A Col B 1 2")
    assert streamed.endswith("Text after the table that must arrive.")


def test_non_streaming_plain(monkeypatch):
    assert make_session()._wait_for_completion(FakePage(["a", "ab", "abc"]), new_future()) == "abc"


def test_callback_errors_dont_break_worker(monkeypatch):
    def bad(_):
        raise RuntimeError("client gone")
    monkeypatch.setattr(config, "STREAM_MODE", "text")
    assert make_session()._wait_for_completion(FakePage(["x", "xy"]), new_future(), bad) == "xy"


# ---------------------------------------------------------------- reasoning
REASONING_REPLY = [
    {"reasoning": "Thinking..."},
    {"reasoning": "Thinking... The user asks about headway."},
    {"reasoning": "Thinking... The user asks about headway. Check T0 and T1."},
    {"reasoning": "Thinking... The user asks about headway. Check T0 and T1.", "answer": "Headway is"},
    {"reasoning": "", "answer": "Headway is the time between trains."},   # panel collapsed
]
TAGGED = ("<think>\nThinking... The user asks about headway. Check T0 and T1.\n</think>\n\n"
          "Headway is the time between trains.")


@pytest.mark.parametrize("mode", ["text", pytest.param("markdown", marks=needs_md)])
def test_reasoning_streamed_in_think_tags_before_answer(monkeypatch, mode):
    got, result, fut = run(FakePage(REASONING_REPLY, generating_until=5), mode, monkeypatch)
    streamed = "".join(got)
    assert streamed == result == fut.streamed_text == TAGGED
    assert got[0] == "<think>\n" and got[1] == "Thinking..."     # reasoning streams live
    assert fut.answer_plain == "Headway is the time between trains."


def test_custom_think_tag(monkeypatch):
    monkeypatch.setattr(config, "THINK_TAG", "reasoning")
    got, result, _ = run(FakePage(REASONING_REPLY, generating_until=5), "text", monkeypatch)
    assert result.startswith("<reasoning>\n") and "\n</reasoning>\n\n" in result
    assert "<think>" not in result and "".join(got) == result


def test_reasoning_non_streaming(monkeypatch):
    assert make_session()._wait_for_completion(FakePage(REASONING_REPLY, 5), new_future()) == TAGGED


def test_reasoning_disabled(monkeypatch):
    monkeypatch.setattr(config, "INCLUDE_REASONING", False)
    got, result, _ = run(FakePage(REASONING_REPLY, generating_until=5), "text", monkeypatch)
    assert result == "".join(got) == "Headway is the time between trains."


def test_no_reasoning_no_tags(monkeypatch):
    got, result, _ = run(FakePage(["The", "The answer."], generating_until=2), "text", monkeypatch)
    assert result == "".join(got) == "The answer."


def test_reasoning_only_seen_at_end_still_wrapped(monkeypatch):
    snaps = [{"reasoning": "Short thought.", "answer": "Answer"}, {"reasoning": "Short thought.", "answer": "Answer."}]
    got, result, _ = run(FakePage(snaps, generating_until=2), "text", monkeypatch)
    assert result == "<think>\nShort thought.\n</think>\n\nAnswer."


def test_long_reasoning_phase_does_not_finish_reply(monkeypatch):
    snaps = [{"reasoning": "step"}] * 80 + [{"reasoning": "step done", "answer": "Final answer."}]
    got, result, _ = run(FakePage(snaps, generating_until=0), "text", monkeypatch)
    assert result.endswith("Final answer.") and "".join(got) == result
