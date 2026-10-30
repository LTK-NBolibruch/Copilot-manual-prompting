"""Which page element is the new reply? Runs the browser-side helpers (PICK_JS) in node."""
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from conversations import normalize  # noqa: E402
from copilot import PICK_JS  # noqa: E402

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def js(expr, **values):
    binds = "".join(f"const {k} = {json.dumps(v)};\n" for k, v in values.items())
    out = subprocess.run([NODE, "-e", PICK_JS + binds + f"process.stdout.write(JSON.stringify({expr}));"],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def item(text, new=True, user=False, reasoning=False):
    return {"isNew": new, "inUser": user, "inReasoning": reasoning, "n": normalize(text)}


PROMPT = "Instructions:\nYou are the TrainOps assistant... Respond to this message:\nwhat is headway?"
P = normalize(PROMPT)[:2000]


def pick(items, p=P):
    return js("relayPickReply(items, p)", items=items, p=p)


def test_norm_matches_python():
    for s in ["Héadway **is** 120 s — T0/T1!", "Ünïcode ½ 2²", "<b>x</b>"]:
        assert js("relayNorm(s)", s=s) == normalize(s)


def test_own_message_bubble_is_never_the_reply():
    assert pick([item("old reply", new=False), item(PROMPT)])["idx"] == -1
    assert pick([item("You said: " + PROMPT[:300])])["idx"] == -1
    assert pick([item("old", new=False), item(PROMPT), item("Headway is the time between trains.")]) == \
        {"idx": 2, "start": 2}


def test_short_resumed_prompt():
    p = normalize("Q2")
    assert pick([item("Q2")], p)["idx"] == -1
    assert pick([item("You Q2")], p)["idx"] == -1
    assert pick([item("Q2"), item("Q2 is about headway.")], p)["idx"] == 1
    assert pick([item("Q2 is about headway.")], p)["idx"] == 0


def test_marked_old_replies_are_skipped():
    assert pick([item("older", new=False), item("previous answer", new=False)])["idx"] == -1


def test_old_reply_rerendered_before_your_bubble_is_ignored():
    items = [item("previous answer"), item(PROMPT), item("New answer.")]
    assert pick(items)["idx"] == 2
    assert pick(items[:2])["idx"] == -1


def test_reasoning_and_user_containers_skipped():
    items = [item(PROMPT), item("Thinking about headway", reasoning=True), item("x", user=True)]
    assert pick(items)["idx"] == -1
    assert pick(items + [item("Answer.")])["idx"] == 3


def test_new_chat_without_bubble_match():
    assert pick([item("Answer starts")]) == {"idx": 0, "start": 0}


def test_browser_scripts_are_valid_javascript():
    from copilot import MARK_JS, MENU_JS, MENU_OPEN_JS, PICKER_JS, SNAPSHOT_JS
    for src in (SNAPSHOT_JS, MARK_JS, PICKER_JS, MENU_JS, MENU_OPEN_JS):
        subprocess.run([NODE, "-e", f"const f = {src}; if (typeof f !== 'function') process.exit(1);"],
                       check=True, capture_output=True)
