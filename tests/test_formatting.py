"""Markdown conversion and the plain-text fallback (the cause of unformatted replies in v6-v8)."""
import os
import sys
from concurrent.futures import Future

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import copilot  # noqa: E402
from copilot import CopilotSession, html_to_md  # noqa: E402

pytestmark = pytest.mark.skipif(copilot._to_md is None, reason="markdownify not installed")


def session():
    s = CopilotSession.__new__(CopilotSession)
    s.markdown, s._md_cache, s._warned_no_stop, s._prompt_norm = True, {}, False, ""
    return s


def test_common_blocks():
    assert html_to_md("<h2>Headway</h2>") == "## Headway"
    assert html_to_md("<p>Time between <strong>trains</strong> and <em>buses</em>.</p>") == \
        "Time between **trains** and *buses*."
    assert html_to_md("<ul><li>T0</li><li>T1</li></ul>") == "- T0\n- T1"
    assert html_to_md("<ol><li>Load</li><li>Run</li></ol>") == "1. Load\n2. Run"
    md = html_to_md("<table><tr><th>Branch</th><th>Value</th></tr><tr><td>T0</td><td>120 s</td></tr></table>")
    assert "| Branch | Value |" in md and "| T0 | 120 s |" in md and "---" in md
    assert html_to_md("<p>See <a href='https://x.test/doc'>the doc</a></p>") == "See [the doc](https://x.test/doc)"


def test_code_block_keeps_language_and_underscores():
    md = html_to_md('<pre><code class="language-python">def run_sdr(trip_id):\n    pass</code></pre>')
    assert md.startswith("```python") and "def run_sdr(trip_id):" in md
    assert html_to_md("<p>Use <code>dcsr_token</code> in batch_mode</p>") == "Use `dcsr_token` in batch_mode"


def test_aria_roles_after_browser_normalization():
    # What SNAPSHOT_JS produces from role="heading"/"table"/"row"/"cell" markup
    assert html_to_md("<h3>Summary</h3>") == "### Summary"
    md = html_to_md("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>")
    assert "| A | B |" in md and "| 1 | 2 |" in md


class Page:
    def __init__(self, snap):
        self.snap = snap

    def evaluate(self, script, arg=None):
        return self.snap

    def locator(self, sel):
        class L:
            def count(self_inner):
                return 0
        return L()


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(config, "POLL_INTERVAL", 0.001)
    monkeypatch.setattr(config, "STABLE_SECONDS_NO_STOP", 0.02)
    monkeypatch.setattr(config, "STREAM_MODE", "markdown")


def new_future():
    f = Future()
    f.streamed_text, f.truncated, f.answer_plain, f.chat_url, f.resumed = "", False, "", None, False
    return f


BLOCKS = ["<h2>Headway</h2>", "<p>Time between <strong>trains</strong>.</p>",
          "<ul><li>T0: 120 s</li><li>T1: 90 s</li></ul>"]


def test_buttons_and_sources_do_not_force_plain_text():
    """Regression: page text includes Copy buttons, citation numbers and a long sources list that
    the markdown (correctly) leaves out. That must not make the server fall back to plain text."""
    visible = ("Headway\nTime between trains.1\nT0: 120 s\nT1: 90 s\nCopy\nLike\nDislike\n"
               "Sources\n" + "\n".join(f"{i}. TrainOps help file section {i} - SharePoint" for i in range(1, 15)))
    clean = "Headway Time between trains. T0: 120 s T1: 90 s"
    result = session()._wait_for_completion(Page({"text": visible, "clean": clean, "blocks": BLOCKS}),
                                            new_future())
    assert result == "## Headway\n\nTime between **trains**.\n\n- T0: 120 s\n- T1: 90 s"


def test_streamed_reply_is_formatted():
    got = []
    visible = "Headway Time between trains. T0: 120 s T1: 90 s Copy"
    result = session()._wait_for_completion(
        Page({"text": visible, "clean": visible[:-5], "blocks": BLOCKS}), new_future(), got.append)
    assert "".join(got) == result and result.startswith("## Headway") and "**trains**" in result


def test_real_content_loss_still_falls_back():
    clean = "Intro. " + "Important detail that must not be lost. " * 10
    result = session()._wait_for_completion(
        Page({"text": clean, "clean": clean, "blocks": ["<p>Intro.</p>"]}), new_future())
    assert result.count("Important detail") == 10


def test_debug_dump(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEBUG_REPLIES", True)
    monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
    session()._wait_for_completion(Page({"text": "Headway", "clean": "Headway", "blocks": BLOCKS}), new_future())
    files = sorted(os.listdir(tmp_path / "replies"))
    assert [f.rsplit(".", 1)[1] for f in files] == ["html", "md", "txt"]
