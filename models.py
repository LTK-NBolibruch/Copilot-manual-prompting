"""Model names <-> OpenAI model ids, and parsing what the model picker shows.

A Copilot model is identified by its name as shown in the picker ("Auto",
"Think deeper", ...). Its OpenAI id is MODEL_ID + "-" + slug(name), e.g.
"m365-copilot-think-deeper". MODEL_ID on its own means "whatever is selected".
"""
import re

import config


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def model_id(name: str) -> str:
    return f"{config.MODEL_ID}-{slug(name)}"


def picker_label(aria_label: str, text: str) -> str:
    """Current model from the picker button: aria-label "Auto, Model Selector" -> "Auto"."""
    a = (aria_label or "").strip()
    if a:
        head = re.split(r",\s*model selector\s*$", a, flags=re.I)[0].strip()
        if head and head.lower() != a.lower():
            return head
    t = (text or "").strip().splitlines()
    return t[0].strip() if t else a


def option_name(raw: dict) -> str:
    """Model name from a menu option: its main-content element if present, else its first line.
    Options often have a second line describing the model."""
    for key in ("content", "text"):
        lines = [l.strip() for l in (raw.get(key) or "").splitlines() if l.strip()]
        if lines:
            return lines[0]
    return ""


def checkmark_key(raw: dict) -> str:
    """Copilot's internal model key from the option's checkmark icon, e.g. "Gpt_5_6_Chat"."""
    return (raw.get("key") or "").strip()


def resolve(requested: str, names: list[str], keys: dict | None = None):
    """Map a request's "model" to a picker name.

    keys: optional {name: internal key} (from the checkmark icons); keys are accepted too,
    e.g. "Gpt_5_6_Reasoning" or "gpt-5-6-reasoning".
    Returns (name or None, known). None means "keep the current selection".
    known is False if `requested` was given but didn't match any model.
    """
    r = (requested or "").strip()
    if not r or r.lower() == config.MODEL_ID.lower():
        return None, True
    rl = r.lower()
    for n in names:
        if rl in (n.lower(), model_id(n), slug(n)):
            return n, True
    for n, k in (keys or {}).items():
        if k and (rl == k.lower() or slug(rl) == slug(k) or rl == model_id(k)):
            return n, True
    return None, False
