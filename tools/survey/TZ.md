# Survey bot — TZ

Written 14.09.2026 after the first version got stuck on the owner's first inline click. Grounded in
the Bot API changelog as of **Bot API 10.3, 24.08.2026** (`https://core.telegram.org/bots/api-changelog`),
not on memory.

## What it is for

Walk the owner through annotation sets one question at a time, on a phone, with buttons. It replaces
text files he will not mark up. Sets today: the reading review (51) and the «хто водить» answer key
(40). More will come; adding one must be a single command and no restart.

## What went wrong in v1, and the lesson

The flow SENT a new message per question. That is why "stuck" was indistinguishable from "working":

* a click produced no visible change in the message he had just tapped — the spinner cleared and the
  old message stayed exactly as it was, with its buttons still live;
* the new question arrived as yet another message below, so the chat became a wall and the tapped
  message never showed what had been recorded;
* every send was fire-and-forget: `sendMessage`'s answer was ignored, so a refusal looked identical
  to a success, and the log said nothing either way (fixed mid-session: `say()` now complains).

**Rule taken from this: an inline flow must EDIT the message it is driving.** The message the user
tapped has to change under their finger, or there is no feedback loop at all.

## Behaviour

1. `/start` → the list of sets with progress (`назва (12/51)`), one button per set.
2. Pick a set → the FIRST unanswered question is shown **in the same message** (`editMessageText`).
3. Each question carries the set's own answer buttons plus `⬅ до списку`.
4. Tapping an answer:
   - records it,
   - **edits the same message** to show the question with the chosen answer marked and the buttons
     disabled, then advances the message to the next question. The user sees one live message that
     moves forward, not a growing pile.
   - if the option is marked `comment: true`, the bot asks for a comment and waits for ONE message —
     text or a voice note (see below); `/skip` records the verdict without one. The next question
     then comes as a NEW message at the bottom, because after a comment exchange the old one is no
     longer the last thing in the chat and editing it would leave the live question above the
     history.
5. End of set → a tally per verdict and back to the list.
6. `⬅ до списку` → the current question is frozen ("Зупинено"), state cleared, list shown. There is
   no separate "pause": progress is derived from the answers file after every single answer, so
   simply closing Telegram loses nothing. The button was renamed from `⏸ пауза` because it does not
   pause anything — it goes back to the list.

## Answer buttons belong to the SET, not the bot

A set that asks «хто водить» cannot be answered with «вірно / не так» — the verdict vocabulary has
to match the question or the data is worthless (this was a real defect: the answer key shipped with
review buttons). `options` in the set's JSON; the default is
`вірно / не так (with comment) / не видно`. Every set carries a neutral third option on purpose —
forcing a binary answer on a question the owner cannot judge poisons exactly the scale being built.

## API facts that constrain the design (verified against the changelog, not assumed)

* `callback_data` is **1–64 bytes**. Ours is `a:<set>:<id>:<key>` — measured 19–24 bytes for the
  current sets. A longer set name would silently break this, so the builder must check it.
* `answerCallbackQuery` must be called promptly or the client spins. It is the FIRST thing the
  handler does, before any file I/O.
* **Only one `getUpdates` may run per token.** A second poller gets HTTP 409 and updates are split
  between them at random — the exact shape of "sometimes nothing happens". The service must be the
  only poller, and a 409 must be logged loudly rather than swallowed.
* A long poll expiring is NORMAL, not an error: it must not log or sleep.
* Bot API 10.3 added `disabled` to `InlineKeyboardButton` — used here to freeze the answered
  question instead of removing its keyboard, so the chosen answer stays visible.
* `editMessageText` with unchanged text returns error 400 "message is not modified" — the text must
  actually differ, so the recorded answer is written into the message body.

## Data

    knowledge/upstream/surveys/<name>.json          {"title", "options"?, "items":[{"id","text"}]}
    knowledge/upstream/surveys/answers/<name>.json  [{"id","verdict","comment","ts"}]
    knowledge/upstream/surveys/state.json           {"<uid>": {"survey", "msg_id", "awaiting_comment", "verdict"}}

Answers are written atomically (temp + replace) and a re-answer overwrites its row, never appends.
Progress = count of answered rows. The two cannot disagree because there is only one source.

## Deliberately NOT built

No web page, no subdomain, no database, no multi-user, no undo stack, no in-flow statistics, no
export formats. One owner, one token, plain JSON I read directly. The bot is a keyboard on top of
files, and it should stay that size.


## Voice comments (added 14.09.2026)

Where a comment is expected, a **voice note is accepted instead of typing** — the owner's own
request, and the right one: a spoken explanation costs him seconds and a typed one costs minutes, so
the corrections get richer rather than shorter.

* `voice`, `audio` and `video_note` are all accepted while `awaiting_comment` is set.
* The file is downloaded and handed to `tools/survey/transcribe.py`, run as a SUBPROCESS in the main
  bot's venv. The questionnaire stays stdlib-only, and a transcription that hangs or dies cannot
  take it down.
* Same engine, config and truncation guard as the main bot's voice path (`bot/krevetka.py`) — one
  behaviour for voice in this project rather than two that drift apart.
* **The transcript is echoed back.** A misheard word in a comment that becomes a glossary line is
  worse than a slow one; he has to see what was heard.
* Failure is explicit ("не вийшло розшифрувати") and the bot keeps waiting, so a bad recording
  never silently swallows an answer.
