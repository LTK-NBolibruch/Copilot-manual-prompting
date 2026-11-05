# Copilot website interface notes

This file is the handoff record for future coding agents. Update the dated change log whenever a
selector or page-flow assumption changes. Do not silently replace old observations: keep them so a
future repair can compare past and present DOM structure.

## Structural contract observed 2026-11-03

### URLs
- Chat home configured by `COPILOT_CHAT_URL`, default `https://m365.cloud.microsoft/chat/`.
- Persistent chats match `/chat/conversation/<UUID>`; canonicalization is in
  `conversations.CONVERSATION_URL_RE`.
- Microsoft login redirects are detected by `copilot._is_login_url`.

### Editor and reply automation
- Editor selector: `config.INPUT_SEL`; sending, Stop-button handling, reply snapshots and model-picker
  selection live in `copilot.py`.
- Model-picker success is authoritative proof of an authenticated session.
- A working `/chat` page takes precedence over a stale login tab.

### Conversation-history import
- History navigation links are discovered with:
  `a[href*="/chat/conversation/"]`.
- On each conversation page, turn candidates are the union of:
  - `[data-testid*="message"]`
  - `[data-testid*="turn"]`
  - `article`
  - `[role="article"]`
- Role classification inspects `data-testid`, `aria-label`, and class names:
  - user: `user|prompt|request`
  - assistant: `assistant|copilot|response|answer`
- Empty imports are errors, not successful empty conversations. Failures are written to
  `logs/server.log` by `manager.py`.

### Repair procedure after a site change
1. Run `python manager.py --show` and reproduce the failure on the Copilot Sessions desktop.
2. Save `logs/fail_<port>_<timestamp>.png` and inspect the affected page in Edge DevTools.
3. Compare current URL, editor, history links, turn containers, role attributes, and model picker to
   the dated contract above.
4. Add the new selectors as a fallback before deleting an older selector.
5. Add a dated entry below, including selectors changed and the real symptom.
6. Add/update a unit fixture in `tests/` and run `pytest -q`.

## Change log
- **2026-11-03, v27**: Added website-history import, layered turn selectors, replay markers, durable
  structured histories, starting-point selection, and one-shot endpoint replay. Initial structural
  contract recorded.
- **2026-11-03, v26**: Authentication became operationally latched after model-picker/editor success.
- **2026-11-02, v21-v25**: Isolated Edge processes, Windows 11 Copilot Sessions desktop, graceful Edge
  shutdown, stale-process UI, and shared browser/session authentication state.

- **2026-11-03, v28**: Repaired imported presentation wrappers (`You said:`, `Copilot said:`),
  migrated existing v27 library rows on startup, added search and bounded 100-item loading, made replay
  previews resizable/scrollable/copyable, canonicalized returned replay messages, and decoupled SSE
  transport from persistence. SSE headers now flush before browser submission and logs include accepted,
  replay prefix, Copilot submission, first delta, final chunk, `[DONE]`, and persistence checkpoints.

### v28 replay and synchronization invariants
- Synthetic `[COPILOT_REPLAY ...]` assistant content is client-facing only. It is never sent to the
  Copilot website and never stored as an assistant answer.
- A returned marker resolves the canonical structured history by token and chat URL; only messages
  after the marker are appended before the new Copilot answer.
- `history_library.chat_url` is unique, so local capture and Copilot import merge into one record.
- Streaming transport does not depend on SQLite writes. A disconnected client does not cancel the
  browser worker; the completed answer is persisted by a Future callback.
- Imported message text must be normalized with `replay.strip_wrapper` before titles and starting
  points are generated.

- **2026-11-04, v29**: Normalized Copilot div-based code widgets into semantic `<pre><code>`
  before markdown conversion, preserving fenced blocks, indentation, and language classes. Expanded
  reasoning discovery to `thought`, `aria-label*=thinking/reasoning`, and existing reasoning selectors.
  Collapsed reasoning uses `textContent` fallback and is streamed before the answer inside configurable
  `<think>...</think>` tags (`COPILOT_THINK_TAG`, default `think`).

- **2026-11-04, v30**: Added Copilot error-card handling for exact `Try Again` buttons. Manual mode
  returns a client-facing instruction and intercepts `TRY AGAIN` / `ALWAYS TRY AGAIN` without sending
  those control messages to Copilot. Automatic mode waits `COPILOT_TRY_AGAIN_DELAY` (default 2 seconds),
  clicks the website button, and retries up to `COPILOT_TRY_AGAIN_MAX` (default 3). Retry controls and
  synthetic guidance are excluded from durable conversation history. `DISABLE ALWAYS TRY AGAIN`
  turns automatic retry off for that session.

- **2026-11-04, v31**: Recorded the actual virtualized code widget structure: complete host
  `[aria-label="Code Preview"]` / `id^="code-block-host-"`; language label under
  `[data-testid="one-copilot-code-identity"] span`; source rows under
  `[role="textbox"][aria-label="Code editor"] [data-line-index]`; blank rows use
  `data-line-empty`. The widget has no `<code>` element. Snapshot normalization now reconstructs an
  ordered `<pre><code class="language-*">` and replaces the whole host, excluding toolbar labels,
  SVG text, line-number nodes, and Copy/More-options controls. `C++` maps to `cpp`; `Plain Text`
  produces an unlabelled fenced block.
