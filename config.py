"""Central settings. Override any value with an environment variable."""
import os


def _env(name, default, cast=str):
    return cast(os.environ.get(name, default))


# ---------------------------------------------------------------------------
# Reasoning tags
# Copilot's reasoning ("thinking") text is sent before the answer, wrapped as
#     <think>
#     ...reasoning...
#     </think>
# Change THINK_TAG to "thinking", "reasoning", etc. - the closing tag always matches.
THINK_TAG         = _env("COPILOT_THINK_TAG", "think")
INCLUDE_REASONING = _env("COPILOT_INCLUDE_REASONING", "1") not in ("0", "false", "no", "")
# Elements holding Copilot's reasoning text. This is a best guess - verify in DevTools
# (inspect the "Thinking..." / "Reasoned for Ns" section of a reply) and adjust.
REASONING_SEL     = _env("COPILOT_REASONING_SEL",
                         '[data-testid*="reasoning" i], [data-testid*="thinking" i], '
                         '[data-testid*="chain-of-thought" i], [class*="reasoning" i], '
                         '[class*="thinking" i], [class*="ChainOfThought"]')
# ---------------------------------------------------------------------------


CHAT_URL        = _env("COPILOT_CHAT_URL", "https://m365.cloud.microsoft/chat/")
PROFILE_DIR     = _env("COPILOT_PROFILE_DIR", "./playwright_profile")
BROWSER_CHANNEL = _env("COPILOT_BROWSER_CHANNEL", "msedge")

# Selectors - edit these when the Copilot UI changes
INPUT_SEL   = _env("COPILOT_INPUT_SEL", '#m365-chat-editor-target-element, textarea, [contenteditable="true"]')
REPLY_SEL   = _env("COPILOT_REPLY_SEL", '[data-testid="markdown-reply"], .fai-ChatMessage__content')
SEND_SEL    = _env("COPILOT_SEND_SEL", 'button[title*="Submit"], button[aria-label*="Send"]')
STOP_SEL    = _env("COPILOT_STOP_SEL", 'button[aria-label*="Stop"]')  # verify in DevTools
POPOVER_SEL = _env("COPILOT_POPOVER_SEL", "button.fai-next, .fui-TeachingPopover button")
# UI elements inside a reply that are not part of the answer (Copy buttons, icons, citation markers).
# Removed before converting to markdown. Drop "sup" if you need superscripts in replies.
NOISE_SEL   = _env("COPILOT_NOISE_SEL", "button, svg, sup")

# Streaming
#   markdown - send finished blocks (paragraph, list, table, code block, heading) as markdown (default)
#   text     - send plain text as it is typed; pauses when Copilot reformats earlier text
STREAM_MODE = _env("COPILOT_STREAM_MODE", "markdown")

# Timeouts (seconds)
INPUT_TIMEOUT = _env("COPILOT_INPUT_TIMEOUT", 20, int)
START_TIMEOUT = _env("COPILOT_START_TIMEOUT", 120, int)   # wait for reasoning or reply to appear
REPLY_TIMEOUT = _env("COPILOT_REPLY_TIMEOUT", 600, int)   # hard cap per reply; hitting it -> finish_reason "length"
LOGIN_TIMEOUT = _env("COPILOT_LOGIN_TIMEOUT", 300, int)   # manual sign-in with --show

# Completion detection: a reply is finished when its text has not changed for
STABLE_SECONDS         = _env("COPILOT_STABLE_SECONDS", 4, float)          # ...and the Stop button is gone
STABLE_SECONDS_NO_STOP = _env("COPILOT_STABLE_SECONDS_NO_STOP", 15, float) # ...when no Stop button was ever seen
                                                                           #    (STOP_SEL wrong: wait out pauses)
STUCK_SECONDS          = _env("COPILOT_STUCK_SECONDS", 90, float)          # ...even if a Stop button is still shown
                                                                           #    (STOP_SEL matches something permanent)

# Page polling
POLL_INTERVAL        = _env("COPILOT_POLL_INTERVAL", 1.0, float)         # non-streaming
STREAM_POLL_INTERVAL = _env("COPILOT_STREAM_POLL_INTERVAL", 0.3, float)  # while streaming

# Server (local only)
HOST     = _env("COPILOT_HOST", "127.0.0.1")
PORT     = _env("COPILOT_PORT", 5000, int)
MODEL_ID = _env("COPILOT_MODEL_ID", "m365-copilot")
LOG_DIR  = _env("COPILOT_LOG_DIR", "./logs")
MAX_PROMPT_CHARS = _env("COPILOT_MAX_PROMPT_CHARS", 200000, int)  # server-side sanity limit only
