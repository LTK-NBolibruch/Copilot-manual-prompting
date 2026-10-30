"""Central settings. Override any value with an environment variable."""
import os


def _env(name, default, cast=str):
    return cast(os.environ.get(name, default))


def _flag(name, default):
    return _env(name, default) not in ("0", "false", "no", "")


# ---------------------------------------------------------------------------
# Reasoning tags
# Copilot's reasoning ("thinking") text is sent before the answer, wrapped as
#     <think>
#     ...reasoning...
#     </think>
# Change THINK_TAG to "thinking", "reasoning", etc. - the closing tag always matches.
THINK_TAG         = _env("COPILOT_THINK_TAG", "think")
INCLUDE_REASONING = _flag("COPILOT_INCLUDE_REASONING", "1")
# Elements holding Copilot's reasoning text. This is a best guess - verify in DevTools
# (inspect the "Thinking..." / "Reasoned for Ns" section of a reply) and adjust.
REASONING_SEL     = _env("COPILOT_REASONING_SEL",
                         '[data-testid*="reasoning" i], [data-testid*="thinking" i], '
                         '[data-testid*="chain-of-thought" i], [class*="reasoning" i], '
                         '[class*="thinking" i], [class*="ChainOfThought"]')
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Models
# /v1/models lists the models in Copilot's model picker, read from the web UI.
# A request's "model" selects that model in the picker before the prompt is sent.
#   "m365-copilot"            -> keep whatever is currently selected
#   "m365-copilot-<name>"     -> select <name>, e.g. m365-copilot-auto
#   "<name>"                  -> also accepted, e.g. "Auto" (case-insensitive)
MODEL_ID     = _env("COPILOT_MODEL_ID", "m365-copilot")   # id meaning "current selection"; prefix for the rest
# Comma-separated model names. If set, these are listed INSTEAD of reading the picker
# (use it to show only some models, or if reading the picker fails), e.g. "Auto,Quick response,Think deeper"
MODELS       = _env("COPILOT_MODELS", "")
MODELS_REFRESH_MINUTES = _env("COPILOT_MODELS_REFRESH_MINUTES", 60, float)  # re-read the picker this often
# Unknown model names: 0 = use the current selection and log a warning (clients with
# hard-coded names like "gpt-4o" keep working); 1 = reject with HTTP 400.
STRICT_MODELS = _flag("COPILOT_STRICT_MODELS", "0")
# The model picker button and its menu. From the picker's HTML (id="gptModeSwitcher").
MODEL_PICKER_SEL = _env("COPILOT_MODEL_PICKER_SEL", "#gptModeSwitcher")
MODEL_MENU_SEL   = _env("COPILOT_MODEL_MENU_SEL", '[role="menu"], [role="listbox"]')
MODEL_OPTION_SEL = _env("COPILOT_MODEL_OPTION_SEL",
                        '[role="menuitemradio"], [role="menuitemcheckbox"], [role="menuitem"], [role="option"]')
# Options that open a sub-menu of more models (e.g. a "GPT-5.6 Sol" entry whose sub-menu holds
# "GPT-5.6 Sol Quick response" and "GPT-5.6 Sol Think deeper"). Their models are listed too.
MODEL_SUBMENU_SEL = _env("COPILOT_MODEL_SUBMENU_SEL",
                         '[aria-haspopup="menu"], [aria-haspopup="true"], :has(> .fui-MenuItem__submenuIndicator)')
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Conversation resuming
# When a request's earlier messages match a Copilot chat this server created,
# that chat is reopened and only the new user message is sent.
RESUME_CONVERSATIONS = _flag("COPILOT_RESUME_CONVERSATIONS", "1")
CONVERSATION_DB      = _env("COPILOT_CONVERSATION_DB", "./conversations.db")  # stores hashes + chat URLs only
CONVERSATION_TTL_DAYS = _env("COPILOT_CONVERSATION_TTL_DAYS", 30, float)      # forget chats unused this long
MAX_TURNS            = _env("COPILOT_MAX_TURNS", 30, int)                     # start a new chat after this many
LOG_CONVERSATIONS    = _flag("COPILOT_LOG_CONVERSATIONS", "0")                # 1 = also save full text to
                                                                              #     logs/conversations.jsonl
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Debugging
# 1 = save every reply's HTML, the markdown produced from it and the page text to
#     logs/replies/<time>.{html,md,txt}, to diagnose formatting problems.
DEBUG_REPLIES = _flag("COPILOT_DEBUG_REPLIES", "0")
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
# Your own messages in the chat. REPLY_SEL can also match them; anything inside these is never
# treated as Copilot's reply. Best guess - verify in DevTools. (Your message is also recognised
# by its text, so this is a second line of defence.)
USER_MSG_SEL = _env("COPILOT_USER_MSG_SEL",
                    '[data-testid*="user-message" i], [data-testid*="usermessage" i], '
                    '[class*="userMessage" i], [data-author="user"], [data-message-author-role="user"]')
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
RESUME_LOAD_TIMEOUT = _env("COPILOT_RESUME_LOAD_TIMEOUT", 30, int)  # reopening an existing chat
MODEL_TIMEOUT = _env("COPILOT_MODEL_TIMEOUT", 10, int)    # opening the model picker

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
LOG_DIR  = _env("COPILOT_LOG_DIR", "./logs")
MAX_PROMPT_CHARS = _env("COPILOT_MAX_PROMPT_CHARS", 200000, int)  # server-side sanity limit only
