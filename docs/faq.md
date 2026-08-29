# FAQ

Questions that are not answered elsewhere. If you hit a specific message, start
with [troubleshooting.md](troubleshooting.md); for what the API does, see
[api-reference.md](api-reference.md).

## Can I use this in production?

No — not as-is. It is an example: one query, no retry policy of its own beyond
the SDK's, no logging, no persistence, no concurrency. The error handling is
deliberately explicit so you can see the failure modes, not because it is a
template for a service.

Worth being blunt about a second reason: the resume path has never run against a
live paused turn. See [pause-turn.md](pause-turn.md).

## Why isn't it a package? Why not on PyPI?

Because it is one file you are meant to read, and packaging invites you not to.
`pyproject.toml` deliberately has no `[project]` table — it carries tool
configuration only. Copy the file into your own project rather than depending on
it.

## What is tested?

Every function — 52 tests, coverage enforced at 100% in CI, and every test file
checked by deliberately breaking the code to confirm the tests notice.

[testing.md](testing.md) has the detail: what each file covers, why the stand-ins
are fakes rather than mocks, and the table of fifteen mutations.

What the suite cannot settle is **which** resume shape the API actually sends.
Every test runs against a fake client, so that remains
[the open question](pause-turn.md).

## What does one run cost?

Small, but not zero. Claude Opus 5 is $5 per million input tokens and $25 per
million output. A single run of this example is a few thousand tokens each way,
so the model cost is cents.

The server-side web search is billed separately from tokens. This page will not
quote a figure that could go stale — see
[Anthropic's pricing](https://platform.claude.com/docs/en/home) for the current
rate.

## Can I use a cheaper model?

Yes, with one constraint: `web_search_20260209` requires Opus 5/4.8/4.7/4.6 or
Sonnet 5/4.6. `claude-sonnet-5` is the obvious swap and costs less. Older models
need the `web_search_20250305` tool type instead — changing `MODEL` alone is not
enough.

## Can I remove the web search?

Then there is not much left. Drop `TOOLS` and the example becomes a plain
Messages call — which is a fine thing to want, but it is a different, simpler
example. Most of this code exists to handle what the search tool implies:
`pause_turn`, HTTP-200 tool errors, source collection.

## Why synchronous rather than async?

Nothing here benefits. One request at a time, and the second only happens if the
first pauses. `AsyncAnthropic` exists and works the same way, but using it would
add an event loop to a script whose whole point is being readable top to bottom.

## Why is the repository so much larger than the script?

157 lines of Python, thirty-odd files around it. That is deliberate: the
repository doubles as a worked example of the scaffolding a small public project
normally carries — CI, linting, dependency automation, governance, docs.

If you are here for the API usage, [`quickstart.py`](../quickstart.py) and
[api-reference.md](api-reference.md) are the whole story. Everything else is
optional reading.
