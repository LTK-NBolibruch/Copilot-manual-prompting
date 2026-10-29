# Copilot Local: OpenAI-compatible API

`server.py` gives you a **local, OpenAI-compatible Chat Completions endpoint**. Behind it, Playwright drives Microsoft 365 Copilot Chat in Edge, signed in with your saved session.

There's no authentication or token handling: the server listens on `127.0.0.1` only, and any `api_key` a client sends is ignored.

```
OpenAI client ──▶ http://127.0.0.1:5000/v1 ──queue──▶ one Edge session ──▶ m365.cloud.microsoft/chat
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
| GET | `/health` | Worker status and queue depth |

## Streaming (`stream: true`)

Streaming is **live**: text is sent to the client while Copilot is still writing it.

1. While Copilot writes, the worker reads the reply every `COPILOT_STREAM_POLL_INTERVAL` seconds (default 0.3).
2. It sends only text that **continues** what the client already has, each piece as a `chat.completion.chunk`.
3. When Copilot finishes, the server sends a final chunk with `finish_reason: "stop"`, then `data: [DONE]`. Keep-alive comments go out every 10 s while nothing new arrives.

**Limitations to know about:**

- **Plain text only.** Streamed replies are plain text. Only non-streamed replies are converted to markdown, and only if `markdownify` is installed.
- **Rewrites pause the stream.** Copilot sometimes re-renders text it already wrote, for example turning it into a table, a code block or citation markers. When that happens, the server stops sending, so the client never gets duplicated or garbled text. It resumes if the reply goes back to continuing the sent text. In that case the streamed text can differ slightly from Copilot's final version. `ask.py --stream` prints a note when this happens.
- **Whole-reply fallback.** If nothing could be streamed at all, the whole reply is sent as one chunk at the end.
- **Errors arrive in the stream.** Once streaming has started, an error is sent as an `{"error": {...}}` event followed by `[DONE]`, because the HTTP status is already 200.
- **Disconnects.** If the client disconnects, Copilot still finishes in the background, and the next request waits for it.

## How OpenAI features map onto Copilot

| Feature | Behaviour |
|---|---|
| `messages` | Each request starts a **new Copilot chat**. A single user message is sent unchanged. When there's more, system/developer messages become an "Instructions:" block and earlier turns become a "Conversation so far:" transcript, followed by the latest user message. |
| `usage` | Always 0, because Copilot doesn't report token counts. |
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

## Command line

```powershell
python ask.py "What is a flaky test?"
python ask.py --stream "Explain headway in two paragraphs"
python ask.py -i --stream              # interactive, streamed
```

Stop the server before using `ask.py`, because both use the same browser profile.

## Notes

- **One prompt at a time.** Concurrent requests queue up behind each other.
- **Logs.** `logs/server.log` records each request. For streamed replies it also records how long until the first text arrived and how much was streamed versus the final reply length.
- **Re-sign-in.** Stop the server, run `python ask.py --show "Reply with OK"`, then start it again.
- **Settings.** Selectors, timeouts, poll intervals, port and model name are in `config.py`, and each can be overridden with a `COPILOT_*` environment variable.
- **Tests.** Run `pytest -q`. The browser is mocked, and the streaming logic is tested against a simulated Copilot page.
