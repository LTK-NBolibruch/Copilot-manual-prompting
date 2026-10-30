# Copilot Local: OpenAI-compatible API

`server.py` gives you a **local, OpenAI-compatible Chat Completions endpoint**. Behind it, Playwright drives Microsoft 365 Copilot Chat in Edge, signed in with your saved session. `manager.py` runs **several endpoints at once**, one browser tab each, from a small window.

There's no authentication or token handling: endpoints listen on `127.0.0.1` only, and any `api_key` a client sends is ignored.

```
OpenAI client ──▶ http://127.0.0.1:5000/v1 ──▶ Edge tab 1 ─┐
OpenAI client ──▶ http://127.0.0.1:5001/v1 ──▶ Edge tab 2 ─┼─▶ Copilot chats (new or resumed)
                                          ...              ┘   one browser, one profile, one sign-in
```

## Setup

```powershell
pip install -r requirements.txt        # includes markdownify, needed for formatted replies
python ask.py --show "Reply with OK"   # one-time sign-in (saved in .\playwright_profile)

python server.py                       # one endpoint:   http://127.0.0.1:5000/v1
python manager.py                      # or: a window for several endpoints (5000, 5001, ...)
```

Only one of `server.py`, `manager.py` and `ask.py` can run at a time, because they share the browser profile.

## Several sessions at once (`manager.py`)

```powershell
python manager.py            # browser kept off-screen ("Show browser" brings it back)
python manager.py --show     # browser visible: one window, one tab per session
start_manager.cmd            # the same, without a console window
```

**The manager uses a normal Edge.** It starts Edge itself, as an ordinary process with remote debugging on a local port, using the saved profile (`playwright_profile`). Each session then attaches to that Edge in its own tab. In an Edge launched *by Playwright*, Copilot only loads one chat at a time, so extra tabs never got past loading. A normal Edge doesn't have that limit. `server.py` and `ask.py` still use a Playwright-launched Edge, which is fine for a single session.

The window lists one row per session: name, port, endpoint URL (selectable, so you can copy it), an on/off switch, a status and a **Remove** button.

| Control | What it does |
|---|---|
| **Add** | Adds a session on the next port (5000, 5001, 5002, …) and switches it on |
| **Switch on** | Opens a new tab in the browser and starts answering on that port |
| **Switch off** | Stops taking requests on that port, lets a reply in progress finish (up to `COPILOT_STOP_TIMEOUT`, 60 s, then aborts it) and closes the tab |
| **Switch, while "Starting…"** | Cancels the start and frees the port |
| **Remove** | Stops the session **right away**, aborting a reply in progress or a start, and removes the row |
| **Show browser / Hide browser** | Moves the Edge window on- or off-screen, for example to sign in. Not available with `COPILOT_EDGE_HIDDEN=headless` |
| **Processes…** | Lists the browser processes using the profile, and lets you end leftovers (see below) |
| **Retry** | Appears when the browser couldn't start. Starts it again once you've fixed the problem |
| **Closing the window** | Switches every session off (replies in progress finish first), closes Edge, and waits until its processes are gone |
| **Ctrl+C in the console** | Stops every session immediately, then closes Edge |

### Leftover browser processes

Stale Edge processes, left behind by a crash, Ctrl+C or a killed Python, keep using the profile. A new Edge on that profile then can't start properly, and tabs fail to load. The manager deals with them in three ways.

**1. They're found before Edge starts.** Every Edge process names its profile on its command line (`--user-data-dir=…`), so the manager can find each one using `playwright_profile`, whoever started it. If there are any, Edge isn't started yet. A yellow bar explains what was found, with an **End them and start** button, and sessions show `Waiting for the browser`. If you end them another way, for example in Task Manager, the manager notices within a couple of seconds and starts by itself.

**2. They're watched while it runs.** The manager re-checks every `COPILOT_PROCESS_SCAN_SECONDS` (10 s).
- **The top line** shows this manager's Edge, for example `Browser: ready (off-screen) · Edge PID 4120, 17 processes`.
- **The yellow bar** reappears with an **End them** button if leftovers or orphaned Playwright drivers turn up.
- **Processes…** lists everything it found:

| What | Meaning | Can be ended here |
|---|---|---|
| Edge (this manager) | The Edge this manager started, with its process count | No: use **Retry** to restart it |
| Edge left over from an earlier run | The program that started it is gone | Yes |
| Edge used by another program | The program that started it is still running, usually `server.py` or `ask.py` (the *Started by* column names it) | Yes, which closes that program's browser |
| Orphaned Playwright driver | A `node.exe` Playwright driver whose Python is gone (the `EPIPE` crash after Ctrl+C leaves these) | Yes |

Only processes using **this app's profile** are ever listed or ended, never your everyday Edge. The list is re-checked just before ending anything, so a process ID that Windows has reused since is never ended.

**3. Windows ends them automatically.** At startup, `manager.py`, `server.py` and `ask.py` put themselves in a Windows *job object*. Windows then ends everything they started (Edge and the Playwright drivers) when the Python process exits, even if it crashes or is ended from Task Manager. Turn this off with `COPILOT_KILL_ON_EXIT=0`. If Windows refuses, a warning is logged and the leftovers are caught at the next start instead.

**Per-tab process IDs aren't shown.** Edge can share one renderer process between tabs on the same site, so a session's tab doesn't reliably map to its own process.

| Setting | Default | Purpose |
|---|---|---|
| `COPILOT_STALE_EDGE` | `ask` | Leftovers found before starting: `ask` (yellow bar, wait for you), `end` (end them automatically), `ignore` (start anyway) |
| `COPILOT_PROCESS_SCAN_SECONDS` | `10` | How often the window re-checks the processes |
| `COPILOT_KILL_ON_EXIT` | `1` | Windows: end Edge and the drivers when Python exits |

Install **psutil** (it's in `requirements.txt`) for fast process listing. Without it, the manager asks PowerShell, which takes about a second per check.

### Time limits

Nothing waits forever:

| Step | Limit | Setting |
|---|---|---|
| Edge opening its debugging port | 30 s | `COPILOT_BROWSER_START_TIMEOUT` |
| A session's tab opening | 60 s | `COPILOT_SESSION_START_TIMEOUT` |
| Switching off with a reply in progress | 60 s, then the reply is aborted | `COPILOT_STOP_TIMEOUT` |

A session that hits a limit shows an error in its row and can be switched on again. Waiting for you to end leftover processes has no limit.

### Signing in

- **The top line says when you need to.** If the browser isn't signed in, it shows `Browser: not signed in - click Show browser and sign in`, and session rows show `On - not signed in`.
- **How to sign in.** Click **Show browser**, then sign in in the Copilot tab. The sign-in is saved to the profile, and you can click **Hide browser** afterwards.
- **Requests during sign-in.** While the window is shown, a request that hits the sign-in page waits up to `COPILOT_LOGIN_TIMEOUT` (5 min) for you to finish, instead of failing straight away.

### Other details

- **Concurrency.** Each session handles one request at a time, and sessions run in parallel. Point different clients, or different conversations, at different ports. Two requests sent to the same port queue up one after the other.
- **Conversations are shared.** All sessions use one `conversations.db`, so a conversation started on 5000 can continue on 5001. Two sessions never continue the **same** Copilot chat at the same time: if a second request arrives for a chat that's busy, it starts a new chat instead.
- **Status messages.**
  - `Starting… 12s`: the session is opening its tab. Click the switch to cancel.
  - `On (2 queued)`: requests waiting for that session.
  - `Error: …`: for example `port 5001 unavailable`, if another program has the port. Switch it on again once it's fixed.
- **The first port** comes from `COPILOT_PORT` (default 5000). Sessions aren't remembered between runs; the window always starts with one session.
- **Finding Edge.** The manager looks for `msedge.exe` in the usual install folders. Set `COPILOT_EDGE_PATH` if it's somewhere else.
- **Off-screen window.** The browser is a normal window placed off-screen (`COPILOT_EDGE_HIDDEN=offscreen`, the default). `headless` hides it completely, but then you can't sign in from the manager.
- **Logs.** All sessions write to `logs/server.log`. Each line shows its session, for example `[copilot-5001]` or `:5001`. Ending processes is logged too, with each PID.

**Unknowns to watch:**
- **Copilot throttling.** All tabs use your one Microsoft 365 account. I don't know whether Copilot throttles several chats at once for one user, so start with two sessions and watch for errors or slow replies.
- **Model selection across tabs.** If Copilot saves the model picker's choice per user, a session switching models could affect what another tab shows as selected. Each request selects its model just before sending, which should cover this, but it's untested.

## Use from any OpenAI client

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:5000/v1` (**http**, not https; another port for another session) |
| API key | anything (e.g. `local`); some clients refuse an empty key |
| Model | `m365-copilot` (current selection) or one from `/v1/models` |

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:5000/v1", api_key="local")

print([m.id for m in client.models.list()])

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
| GET | `/v1/models` | The models in Copilot's model picker (see *Models*) |
| GET | `/v1/models/{id}` | One model |
| GET | `/health` | Port, worker status, queue depth, models, current model and number of stored conversations |

## Models

`/v1/models` lists the models in Copilot's **model picker**, read from the web UI.

1. **Reading the list.** At startup, the server opens the picker (`#gptModeSwitcher`) and reads the option names. **Sub-menus are read too:** for an entry that opens a sub-menu (such as `GPT-5.6 Sol`), the server opens it and adds its models. It then closes the menus. The list is re-read every `COPILOT_MODELS_REFRESH_MINUTES` (60), in the background.
2. **The IDs.** Each model gets an ID made from its name. `m365-copilot` on its own means "whatever is currently selected":

   ```
   m365-copilot                              current selection (no switching)
   m365-copilot-auto                         Auto
   m365-copilot-think-deeper                 Think deeper
   m365-copilot-gpt-5-6-sol-quick-response   GPT-5.6 Sol Quick response
   m365-copilot-gpt-5-6-sol-think-deeper     GPT-5.6 Sol Think deeper
   ```

   Each entry also has a `name` field with the name exactly as the picker shows it.
3. **Selecting a model.** When a request's `model` names one of them, the server selects it in the picker before sending the prompt, opening its sub-menu first if needed. It skips that if the model is already selected. These all work, and case doesn't matter:
   - the full ID
   - the picker name (`"GPT-5.6 Sol Think deeper"`)
   - the short form (`gpt-5-6-sol-think-deeper`)
   - Copilot's internal key from the option's checkmark icon (`Gpt_5_6_Reasoning`)
4. **Ambiguous button labels.** If the picker button shows a label that could mean more than one model, for example `Think deeper` for both the top-level model and the sub-menu one, the server checks which option has the checkmark in the menu.
5. **Unknown model names** (for example `gpt-4o`, or `test`) use the current selection and log a warning, so clients with hard-coded names keep working. Set `COPILOT_STRICT_MODELS=1` to reject them with a 400 error that lists the valid IDs instead.
6. **Resumed chats.** A follow-up continues its Copilot chat when the request asks for the same model the chat used, or for `m365-copilot`. Asking for a different model starts a new chat.

| Setting | Default | Purpose |
|---|---|---|
| `COPILOT_MODELS` | – | Comma-separated names to list **instead of** reading the picker, e.g. `Auto,Think deeper`. Use it to hide models, or if reading the picker fails. |
| `COPILOT_MODEL_ID` | `m365-copilot` | ID meaning "current selection"; also the prefix of the other IDs |
| `COPILOT_MODELS_REFRESH_MINUTES` | `60` | How often to re-read the picker |
| `COPILOT_STRICT_MODELS` | `0` | `1` = reject unknown model names with HTTP 400 |
| `COPILOT_MODEL_PICKER_SEL` | `#gptModeSwitcher` | The picker button |
| `COPILOT_MODEL_MENU_SEL` / `COPILOT_MODEL_OPTION_SEL` | `[role="menu"]` … / `[role="menuitemradio"]` … | The open menu and its options |
| `COPILOT_MODEL_SUBMENU_SEL` | `[aria-haspopup="menu"]` … | Options that open a sub-menu of more models |

- **Checking it worked.** The startup log shows each model with its menu path, for example `Models in the picker: Auto, ..., GPT-5.6 Sol > GPT-5.6 Sol Think deeper (selected: Auto)`. If it shows `Could not read the model list`, only `m365-copilot` is listed. Check the menu selectors in DevTools, or set `COPILOT_MODELS`.
- **Sub-menus that won't open.** These are logged as `Could not read these model sub-menus`, and the main-menu models are still listed. The server tries clicking the entry, then hovering over it, then pressing the right arrow key. If none of those opens it, check `COPILOT_MODEL_SUBMENU_SEL`.
- **From the command line.** `python ask.py --models` lists the models, and `python ask.py --model "Think deeper" "..."` uses one.

## Formatting (markdown)

Replies are returned as markdown: headings, bold and italics, lists, tables, links and fenced code blocks with their language.

**How it works:**

1. **Clean up the reply in the browser.** Copy buttons, icons, citation markers (`COPILOT_NOISE_SEL`) and the reasoning section are removed.
2. **Convert Copilot's own markup.** Tables, lists and headings that Copilot builds from `<div role="table">`, `role="row"`, `role="heading"` and similar elements are turned into real HTML tables, lists and headings. Text made bold or italic with inline styles is turned into `<strong>` and `<em>`.
3. **Convert to markdown.** `markdownify` converts the result. Underscores aren't escaped, so identifiers like `run_sdr` and `trip_id` come through unchanged.

**Plain-text safety net.** If the markdown has much less text than the cleaned reply (less than 80%), the server assumes the conversion lost content. It sends the plain page text instead and logs `Markdown conversion has N of M letters/digits`.

**If replies are unformatted:**

- **Check the startup log.** It should say `markdown=on`. If you see `markdownify is not installed`, run `pip install markdownify`.
- **Look for the safety-net warning.** If `logs/server.log` shows `Markdown conversion has N of M`, the conversion is losing text.
- **Capture a reply for diagnosis.** Set `COPILOT_DEBUG_REPLIES=1`, send one prompt, and look in `logs/replies/`. Each reply is saved as `.html` (the cleaned HTML that was converted), `.md` (the markdown produced from it) and `.txt` (the page text). They contain the reply text, so delete them when you're done.

## Finding the new reply

To read Copilot's answer, the server has to pick the right element on the page. A chat page holds your own messages, earlier replies, and, in a resumed chat, history that may still be loading.

1. **Mark what's already there.** Just before sending a prompt, every reply and reasoning element already on the page is marked as old.
2. **Find your own message.** After sending, your own message bubble is recognised by its text, or by `COPILOT_USER_MSG_SEL`. It's never treated as the reply.
3. **Pick the reply.** Only an unmarked element that comes **after** the old replies and after your message counts as the new reply.
4. **Wait for it.** The server waits until that element, or Copilot's reasoning, appears before it starts reading. If it never appears, the request fails after `COPILOT_START_TIMEOUT` with a message pointing at `COPILOT_REPLY_SEL`.

**Streams always end properly.** A streamed response always finishes with a final chunk (or an error event) and `data: [DONE]`, even if recording the conversation fails, the browser worker stops, or no reply arrives within the time limits.

## Resuming conversations

OpenAI clients send the whole conversation with every request. Instead of starting a new Copilot chat each time and pasting that history in, the server **continues the Copilot chat that already holds the conversation** and sends only the new user message.

**How it works:**

1. After each reply, the server records which Copilot chat (`.../chat/conversation/<id>`) now holds the conversation, and which model answered. The record goes in a local SQLite file, `conversations.db`. It's keyed on a hash of every message, including the reply just sent.
2. On the next request, it hashes the earlier messages, everything except the new user message, and looks them up.
3. **If there's a match,** the server reopens that chat and checks that its last reply is the one it recorded. If it is, it sends only the new message, so the system prompt isn't sent again.
4. **If there's no match, or the check fails,** it starts a new chat with the full history pasted in, and records that chat instead.

**Matching ignores formatting.** The hashes ignore whitespace, punctuation, markdown and `<think>` blocks, so a client that stores replies slightly differently, or drops the reasoning, still matches.

| Situation | What happens |
|---|---|
| Normal follow-up | Same chat; only the new message is sent |
| You regenerate or edit an earlier message | No match, or the chat has moved on, so a new chat starts |
| The system prompt changes | No match, so a new chat starts |
| The request asks for a different model than the chat used | A new chat starts |
| Another session is continuing the same chat right now | A new chat starts |
| The chat was deleted or won't load | The check fails, so a new chat starts and the next turn resumes that one |
| The chat reaches `COPILOT_MAX_TURNS` (30) | A new chat starts |
| The sign-in has expired | 503 `login_required`, as for any request |

**Settings and privacy:**

| Setting | Default | Purpose |
|---|---|---|
| `COPILOT_RESUME_CONVERSATIONS` | `1` | Set to `0` to start every request in a new chat |
| `COPILOT_CONVERSATION_DB` | `./conversations.db` | Where the records are stored |
| `COPILOT_CONVERSATION_TTL_DAYS` | `30` | Records unused this long are deleted at startup |
| `COPILOT_MAX_TURNS` | `30` | Turns per Copilot chat before starting a new one |
| `COPILOT_RESUME_LOAD_TIMEOUT` | `30` | Seconds to wait for a reopened chat to load |
| `COPILOT_LOG_CONVERSATIONS` | `0` | Set to `1` to also append the full prompts and replies to `logs/conversations.jsonl`, for debugging |

- **What's stored.** `conversations.db` holds only hashes, chat URLs, turn counts and model names, no prompt or reply text. It's listed in `.gitignore`. Delete it at any time to forget all chats. A database from an earlier version is upgraded automatically.
- **The log.** Each request is logged as `resumed` or `new-chat`, with the model that answered, in `logs/server.log`, along with the reason when a stored chat couldn't be continued.

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

1. While Copilot writes, the worker reads the reply every `COPILOT_STREAM_POLL_INTERVAL` seconds (default 0.3) and splits it into top-level blocks: paragraphs, headings, lists, tables and code blocks.
2. Every block except the last is finished, so it is converted to markdown and sent.
3. The block Copilot is still writing is held back until the next block starts or the reply ends.

Copilot's re-rendering (raw `**` turning into bold, tables and code blocks forming) happens inside that unfinished block, so a markdown-rendering client can render the stream as it arrives. Text arrives a block at a time, so a long paragraph or a big table appears all at once when it's finished.

### `text`: plain text as it's typed

Sends plain text word by word, with **no formatting**. Sending pauses whenever Copilot reformats text that was already sent.

### Nothing is lost at the end

When the reply finishes, the server always sends whatever the client hasn't received yet, before the final chunk:

1. **Normal case.** The final reply continues the streamed text, so the rest is sent as is.
2. **Copilot re-rendered earlier text** (formatting change, a citation inserted, the whole reply wrapped in a new container). The server lines up the end of what was sent with the final reply, ignoring formatting, and sends everything after that point. Earlier text that changed after it was sent can't be unsent, so the client keeps its earlier version of that part.
3. **The two can't be lined up at all** (should be very rare). The server sends the complete final reply after a `---` separator and logs a warning, so the client has some duplication but no missing text.

### Other streaming details

- **End of stream.** The final chunk has `finish_reason` `"stop"`, or `"length"` if the hard limit was hit, followed by `data: [DONE]`. Keep-alive comments go out every 10 s while nothing new arrives.
- **Errors arrive in the stream.** Once streaming has started, an error is sent as an `{"error": {...}}` event followed by `[DONE]`.
- **Same output with or without streaming.** Non-streamed replies use the same markdown conversion.

## How OpenAI features map onto Copilot

| Feature | Behaviour |
|---|---|
| `model` | Selects that model in Copilot's picker (see *Models*). `m365-copilot` keeps the current selection. The response echoes the `model` you sent. |
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
| 400 | `model_not_found` | Unknown model, with `COPILOT_STRICT_MODELS=1` |
| 400 | `context_length_exceeded` | Copilot's chat box cut off the prompt; shorten system prompt + history |
| 502 | `automation_failed` | Usually a Copilot UI change, or a model that couldn't be selected; check `logs/` (screenshot saved) |
| 503 | `login_required` | Re-run the sign-in step |
| 504 | `timeout` | No reply at all within the time limits |

## Command line

```powershell
python ask.py "What is a flaky test?"
python ask.py --stream "Explain headway in two paragraphs"
python ask.py -i --stream              # interactive, streamed
python ask.py --models                 # list the models in the picker
python ask.py --model "Think deeper" "Compare T0 and T1 batch mode"
```

`ask.py` always starts a new chat for each prompt. Stop `server.py` / `manager.py` before using it, because they share the browser profile.

## Notes

- **One prompt at a time per session.** Within a session, concurrent requests queue up behind each other, and so does re-reading the model list. Use `manager.py` for parallel requests.
- **Client timeouts.** Set the client timeout above `COPILOT_REPLY_TIMEOUT` (600 s). The OpenAI SDK's default is also 600 s, so raise it or lower the server limit.
- **Logs.** `logs/server.log` records each request, whether it resumed a chat, which model answered, and how each reply ended (`stable`, `stuck` or `timeout`). For streamed replies it also records how long until the first text arrived and how much was streamed versus the final reply.
- **Re-sign-in.** Stop the server or manager, run `python ask.py --show "Reply with OK"`, then start it again.
- **Settings.** Selectors, timeouts, poll intervals, stream mode, reasoning tags, models, conversation resuming, debugging, port and model ID are in `config.py`, and each can be overridden with a `COPILOT_*` environment variable.
- **Tests.** Run `pytest -q`. The browser is mocked; reply reading, formatting, streaming, model selection, chat resuming, running several sessions, cancelling and removing sessions, starting Edge, and finding and ending leftover processes are tested against simulated pages and processes. The reply-picking logic runs in Node, which is skipped if `node` isn't installed. The process tests start stand-in processes and are skipped on Windows. The browser-side scripts, the real Edge, the Windows-only process code (PowerShell listing, `taskkill`, the job object) and the window have not been tested on a real machine.
