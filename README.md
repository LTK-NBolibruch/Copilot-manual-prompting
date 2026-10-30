# Copilot Local: OpenAI-compatible API

`server.py` gives you a **local, OpenAI-compatible Chat Completions endpoint**. Behind it, Playwright drives Microsoft 365 Copilot Chat in Edge, signed in with your saved session.

There's no authentication or token handling: the server listens on `127.0.0.1` only, and any `api_key` a client sends is ignored.

```
OpenAI client ──▶ http://127.0.0.1:5000/v1 ──queue──▶ one Edge session ──▶ Copilot chat (new or resumed)
```

## Setup

```powershell
pip install -r requirements.txt
python ask.py --show "Reply with OK"   # one-time sign-in (saved in .\playwright_profile)
python server.py                       # http://127.0.0.1:5000/v1
```

## Use from any OpenAI client

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:5000/v1` |
| API key | anything (e.g. `local`); some clients refuse an empty key |
| Model | `m365-copilot` (any name is accepted) |

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:5000/v1", api_key="local")

# normal
r = client.chat.completions.create(model="m365-copilot", messages=[{"role": "user", "content": "Hello"}])
print(r.choices[0].message.content)

# live streaming
for chunk in client.chat.completions.create(model="m365-copilot", stream=True,
                                            messages=[{"role": "user", "content": "Hello"}]):
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="", flush=True)
```

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/chat/completions` (also `/v1`) | Supports `stream: true/false` and `stream_options.include_usage` |
| GET | `/v1/models` | Lists `m365-copilot` |
| GET | `/v1/models/{id}` | Echoes any id, so hard-coded model names work |
| GET | `/health` | Worker status, queue depth and number of stored conversations |

## Resuming conversations

OpenAI clients send the whole conversation with every request. Instead of starting a new Copilot chat each time and pasting that history in, the server **continues the Copilot chat that already holds the conversation** and sends only the new user message.

**How it works:**

1. After each reply, the server records which Copilot chat (`.../chat/conversation/<id>`) now holds the conversation. The record goes in a local SQLite file, `conversations.db`. It's keyed on a hash of every message, including the reply just sent.
2. On the next request, it hashes the earlier messages, everything except the new user message, and looks them up.
3. **If there's a match,** the server reopens that chat and checks that its last reply is the one it recorded. If it is, it sends only the new message, so the system prompt isn't sent again.
4. **If there's no match, or the check fails,** it starts a new chat with the full history pasted in, as before, and records that chat instead.

**Matching ignores formatting.** The hashes ignore whitespace, punctuation, markdown and `<think>` blocks, so a client that stores replies slightly differently, or drops the reasoning, still matches.

| Situation | What happens |
|---|---|
| Normal follow-up | Same chat; only the new message is sent |
| You regenerate or edit an earlier message | No match, or the chat has moved on, so a new chat starts |
| The system prompt changes | No match, so a new chat starts |
| The chat was deleted or won't load | The check fails, so a new chat starts and the next turn resumes that one |
| The chat reaches `COPILOT_MAX_TURNS` (30) | A new chat starts |
| The sign-in has expired | 503 `login_required`, as for any request |

**Settings and privacy:**

| Setting | Default | Purpose |
|---|---|---|
| `COPILOT_RESUME_CONVERSATIONS` | `1` | Set to `0` to start every request in a new chat (the old behaviour) |
| `COPILOT_CONVERSATION_DB` | `./conversations.db` | Where the records are stored |
| `COPILOT_CONVERSATION_TTL_DAYS` | `30` | Records unused this long are deleted at startup |
| `COPILOT_MAX_TURNS` | `30` | Turns per Copilot chat before starting a new one |
| `COPILOT_RESUME_LOAD_TIMEOUT` | `30` | Seconds to wait for a reopened chat to load |
| `COPILOT_LOG_CONVERSATIONS` | `0` | Set to `1` to also append the full prompts and replies to `logs/conversations.jsonl`, for debugging |

- **What's stored.** `conversations.db` holds only hashes, chat URLs and turn counts, no prompt or reply text. It's listed in `.gitignore`. Delete it at any time to forget all chats.
- **The log.** Each request is logged as `resumed` or `new-chat` in `logs/server.log`, along with the reason when a stored chat couldn't be continued.

## Reasoning (`<think>` tags)

When Copilot shows a reasoning ("Thinking…") section before its answer, that text is sent **first**, wrapped in tags, and kept out of the answer:

```
<think>
...Copilot's reasoning...
</think>

...the answer...
```

- **Change the tag** with `THINK_TAG` at the top of `config.py`, or the `COPILOT_THINK_TAG` environment variable. For example, `thinking` gives `<thinking>` … `</thinking>`. The closing tag always matches.
- **Streaming.** Reasoning streams live as plain text. The closing tag is sent as soon as the answer starts, and the answer then streams as described below. Without streaming, the reply contains the same tagged block followed by the answer.
- **Turn it off** with `COPILOT_INCLUDE_REASONING=0`. Reasoning text is then dropped and only the answer is sent.
- **Conversation history.** If a client sends earlier replies back in `messages`, their tagged reasoning blocks are removed before the prompt goes to Copilot.
- **Selector.** The reasoning section is found with `REASONING_SEL`, also at the top of `config.py`. The default is a guess. If replies arrive without a `<think>` block, or with reasoning mixed into the answer, inspect the reasoning section in DevTools and set the right selector.
- **Long reasoning.** The server starts reading when either the reasoning section or the answer appears. `COPILOT_START_TIMEOUT` is 120 s.

## When is a reply finished?

Copilot doesn't signal the end of a reply directly, so the server watches the page. A reply counts as finished when its text hasn't changed for:

| Situation | Wait | Setting |
|---|---|---|
| The Stop button was seen and is now gone | 4 s | `COPILOT_STABLE_SECONDS` |
| No Stop button was ever seen (`COPILOT_STOP_SEL` doesn't match) | 15 s, so pauses while Copilot searches or thinks don't end the reply early | `COPILOT_STABLE_SECONDS_NO_STOP` |
| A Stop button is still showing | 90 s (in case `COPILOT_STOP_SEL` matches something that never goes away) | `COPILOT_STUCK_SECONDS` |

- **Hard limit.** If Copilot is still writing after `COPILOT_REPLY_TIMEOUT` (default 600 s), the server returns what it has with **`finish_reason: "length"`**, so clients can tell the reply is incomplete.
- **Wrong Stop selector.** The server logs a warning once if no Stop button is seen during a reply. Fixing `COPILOT_STOP_SEL` makes replies finish about 11 s sooner.

## Streaming (`stream: true`)

Streaming is **live**: text is sent to the client while Copilot is still writing it. How it is sent depends on `COPILOT_STREAM_MODE`.

### `markdown` (default): finished blocks, as markdown

1. While Copilot writes, the worker reads the reply's HTML every `COPILOT_STREAM_POLL_INTERVAL` seconds (default 0.3) and splits it into top-level blocks: paragraphs, headings, lists, tables and code blocks.
2. Every block except the last is finished, so it is converted to markdown and sent.
3. The block Copilot is still writing is held back until the next block starts or the reply ends.

Copilot's re-rendering (raw `**` turning into bold, tables and code blocks forming) happens inside that unfinished block, so a markdown-rendering client can render the stream as it arrives.

- **Text arrives a block at a time.** A long paragraph or a big table appears all at once when it's finished.
- **Noise is removed first.** Copy buttons, icons and citation superscripts are stripped before conversion (`COPILOT_NOISE_SEL`).
- **Needs `markdownify`.** Without it, the server falls back to `text` mode.

### `text`: plain text as it's typed

Sends plain text word by word, with no formatting. Sending pauses whenever Copilot reformats text that was already sent.

### Nothing is lost at the end

When the reply finishes, the server always sends whatever the client hasn't received yet, before the final chunk:

1. **Normal case.** The final reply continues the streamed text, so the rest is sent as is.
2. **Copilot re-rendered earlier text** (formatting change, a citation inserted, the whole reply wrapped in a new container). The server lines up the end of what was sent with the final reply, ignoring formatting, and sends everything after that point. Earlier text that changed after it was sent can't be unsent, so the client keeps its earlier version of that part.
3. **The two can't be lined up at all** (should be very rare). The server sends the complete final reply after a `---` separator and logs a warning, so the client has some duplication but no missing text.

### Other streaming details

- **End of stream.** The final chunk has `finish_reason` `"stop"`, or `"length"` if the hard limit was hit, followed by `data: [DONE]`. Keep-alive comments go out every 10 s while nothing new arrives.
- **Errors arrive in the stream.** Once streaming has started, an error is sent as an `{"error": {...}}` event followed by `[DONE]`.
- **Same output with or without streaming.** Non-streamed replies use the same markdown conversion.
- **Safety net for missed content.** If the markdown conversion contains noticeably less text than the page shows, the server uses the page's plain text instead and logs a warning. Check `COPILOT_NOISE_SEL` if you see that warning.

## How OpenAI features map onto Copilot

| Feature | Behaviour |
|---|---|
| `messages` | A follow-up to a stored conversation continues that Copilot chat (see *Resuming conversations*). Otherwise a **new Copilot chat** starts: a single user message is sent unchanged, and for longer histories, system/developer messages become an "Instructions:" block and earlier turns become a "Conversation so far:" transcript, followed by the latest user message. |
| `usage` | Always 0, because Copilot doesn't report token counts. |
| `finish_reason` | `"stop"`, or `"length"` if `COPILOT_REPLY_TIMEOUT` cut the reply off. |
| `temperature`, `max_tokens`, `top_p`, `top_k`, … | Accepted and ignored. |
| Images (`image_url` parts) | Ignored; only text parts are sent. |
| `tools` / function calling, `n > 1`, JSON mode, embeddings | Not supported. |

### Errors (OpenAI shape)

| Status | `code` | Meaning |
|---|---|---|
| 400 | `invalid_request` | Bad JSON or missing `messages` |
| 400 | `context_length_exceeded` | Copilot's chat box cut off the prompt; shorten system prompt + history |
| 502 | `automation_failed` | Usually a Copilot UI change; check `logs/` (screenshot saved) |
| 503 | `login_required` | Re-run the sign-in step |
| 504 | `timeout` | No reply at all within the time limits |

## Command line

```powershell
python ask.py "What is a flaky test?"
python ask.py --stream "Explain headway in two paragraphs"
python ask.py -i --stream              # interactive, streamed
```

`ask.py` always starts a new chat for each prompt. Stop the server before using it, because both use the same browser profile.

## Notes

- **One prompt at a time.** Concurrent requests queue up behind each other.
- **Client timeouts.** Set the client timeout above `COPILOT_REPLY_TIMEOUT` (600 s). The OpenAI SDK's default is also 600 s, so raise it or lower the server limit.
- **Logs.** `logs/server.log` records each request, whether it resumed a chat, and how each reply ended (`stable`, `stuck` or `timeout`). For streamed replies it also records how long until the first text arrived and how much was streamed versus the final reply.
- **Re-sign-in.** Stop the server, run `python ask.py --show "Reply with OK"`, then start it again.
- **Settings.** Selectors, timeouts, poll intervals, stream mode, reasoning tags, conversation resuming, port and model name are in `config.py`, and each can be overridden with a `COPILOT_*` environment variable.
- **Tests.** Run `pytest -q`. The browser is mocked; reply reading, streaming and chat resuming are tested against simulated Copilot pages. The browser-side scripts have not been tested against the real page.
