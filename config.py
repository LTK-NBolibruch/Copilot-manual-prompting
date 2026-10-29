"""Central settings. Override any value with an environment variable."""
import os


def _env(name, default, cast=str):
    return cast(os.environ.get(name, default))


CHAT_URL        = _env("COPILOT_CHAT_URL", "https://m365.cloud.microsoft/chat/")
PROFILE_DIR     = _env("COPILOT_PROFILE_DIR", "./playwright_profile")
BROWSER_CHANNEL = _env("COPILOT_BROWSER_CHANNEL", "msedge")

# Selectors - edit these when the Copilot UI changes
INPUT_SEL   = _env("COPILOT_INPUT_SEL", '#m365-chat-editor-target-element, textarea, [contenteditable="true"]')
REPLY_SEL   = _env("COPILOT_REPLY_SEL", '[data-testid="markdown-reply"], .fai-ChatMessage__content')
SEND_SEL    = _env("COPILOT_SEND_SEL", 'button[title*="Submit"], button[aria-label*="Send"]')
STOP_SEL    = _env("COPILOT_STOP_SEL", 'button[aria-label*="Stop"]')  # verify in DevTools
POPOVER_SEL = _env("COPILOT_POPOVER_SEL", "button.fai-next, .fui-TeachingPopover button")

# Timeouts (seconds)
INPUT_TIMEOUT  = _env("COPILOT_INPUT_TIMEOUT", 20, int)
START_TIMEOUT  = _env("COPILOT_START_TIMEOUT", 60, int)     # wait for reply to appear
REPLY_TIMEOUT  = _env("COPILOT_REPLY_TIMEOUT", 180, int)    # wait for reply to finish
LOGIN_TIMEOUT  = _env("COPILOT_LOGIN_TIMEOUT", 300, int)    # manual sign-in with --show
STABLE_SECONDS = _env("COPILOT_STABLE_SECONDS", 4, float)   # unchanged text for this long = done
POLL_INTERVAL  = _env("COPILOT_POLL_INTERVAL", 1.0, float)  # page polling, non-streaming
STREAM_POLL_INTERVAL = _env("COPILOT_STREAM_POLL_INTERVAL", 0.3, float)  # page polling while streaming

# Server (local only)
HOST     = _env("COPILOT_HOST", "127.0.0.1")
PORT     = _env("COPILOT_PORT", 5000, int)
MODEL_ID = _env("COPILOT_MODEL_ID", "m365-copilot")
LOG_DIR  = _env("COPILOT_LOG_DIR", "./logs")
MAX_PROMPT_CHARS = _env("COPILOT_MAX_PROMPT_CHARS", 200000, int)  # server-side sanity limit only
