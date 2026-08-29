# Paused turns and the resume shape

The single unresolved question in this repository. Everything else here is
either verified or trivially checkable; this is not.

## What a paused turn is

`stop_reason: "pause_turn"` means the model stopped mid-turn and can be resumed.
It is not an error and not a refusal — the turn is incomplete, not finished.

This example is unusually likely to hit one. `web_search_20260209` runs on
Anthropic's infrastructure, and Claude may issue several searches in a single
turn. A long enough server-side loop pauses rather than running indefinitely.

## How resuming works

Send the assistant content back as the last message and make the request again.
Two details matter, both easy to get wrong:

**Do not append a "Continue." message.** The API resumes from the trailing
`server_tool_use` block. An extra user turn changes what it is resuming from.

**Echo `thinking` blocks back unchanged.** Required when continuing on the same
model. This applies even though the script never sets the `thinking` parameter —
Claude Opus 5 thinks by default, so the blocks are present regardless.

[`quickstart.py`](../quickstart.py) caps this at `MAX_CONTINUATIONS` (5). Hitting
the cap is reported as `[incomplete: ...]` rather than silently returning a
partial answer as though it were whole.

## The open question

A resumed turn could plausibly return either:

- **Cumulative** — the whole turn so far, re-sending blocks the client already
  holds. The client should *replace* what it has.
- **Incremental** — only the new segment. The client should *append*.

Guessing wrong is not a subtle failure. Assume incremental when the API is
cumulative and every block is duplicated; assume cumulative when it is
incremental and everything before the last segment is lost.

**Which one the API actually does has never been observed here.**

## How the code sidesteps it

`merge_blocks()` does not assume. It compares the head of the new content
against everything already accumulated:

- head matches what we hold → cumulative → replace
- head does not match → incremental → extend

This is correct under either behaviour, which is why the uncertainty has not
been resolved by necessity. It is still worth resolving: a reader cannot tell
from the code which branch actually runs, and dead code that has never executed
is code nobody has tested.

## What would settle it

The `.type` of each content block across both responses — the pre-pause one and
the resumed one. That alone distinguishes the two cases. For example:

```
response 1:  thinking, server_tool_use, web_search_tool_result   (stop: pause_turn)
response 2:  thinking, server_tool_use, web_search_tool_result, text
```

If response 2 opens by repeating response 1's blocks, it is cumulative. If it
begins where response 1 stopped, it is incremental.

## Triggering one

There is no supported way to force a pause. It depends on how long the
server-side search loop runs, so a broad query needing many distinct searches is
more likely to produce one than a narrow factual question. Editing `USER_QUERY`
toward something requiring wide research is the only lever the example offers.

Note this cannot be reproduced without API credits, which is the practical
reason the question is still open.

## Reporting what you saw

Open a [pause_turn observation](https://github.com/YALLAH-BID/claude-quickstart/issues/new?template=pause_turn_observation.yml).
The block sequence is the whole payload; the form asks for nothing else of
substance.
