# FAQ

Questions that are not answered elsewhere. If you hit a specific message, start
with [troubleshooting.md](troubleshooting.md); for what the API does, see
[api-reference.md](api-reference.md).

## Can I use this in production?

No — not as-is. The error handling is deliberately explicit so you can see the
failure modes, not because it is a template for a service. And the resume path
has never run against a live paused turn ([pause-turn.md](pause-turn.md)), so
part of it is unproven against the real API.

There is nothing here to *deploy* — no package, no service, no container. The
useful question is what you would have to add before code shaped like this could
be. Concretely:

**Credentials.** The script relies on the SDK resolving them from the environment
or an `ant auth login` profile. Neither belongs in a deployed image: inject a key
from a secret manager at runtime. Note profile tokens expire, and the failure
looks identical to never having authenticated at all
([troubleshooting.md](troubleshooting.md)).

**Cost bounds.** `MAX_TOKENS` and `MAX_CONTINUATIONS` are the only ceilings in the
file, and they bound one run at six API calls — the initial request plus five
continuations. The search tool's `max_uses` is *not* set, so how many searches a
single turn performs is bounded by Claude, not by you. Add it before running this
anywhere unattended.

**Observability.** There is no logging at all. Nothing records token usage, which
searches ran, or how many continuations a request consumed — so a run that costs
more than expected leaves no evidence of why.

**Retry policy.** Entirely the SDK's: two retries and a 600-second read timeout,
both defaults. There is no circuit breaker, no budget, and no dead-letter path for
a request that exhausts them.

**The query is not a parameter.** `USER_QUERY` is a module constant read inside
`request()`, so asking a different question means changing function signatures,
not editing a value. The "one question, asked once" assumption is structural
rather than incidental.

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
