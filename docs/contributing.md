# Working on the code

[CONTRIBUTING.md](../CONTRIBUTING.md) is the policy — what belongs in a
quickstart, what doesn't, and how to set up. This page is the practice: how the
157 lines are arranged, and how to establish that a change actually works when
CI cannot tell you.

## How the file is arranged

Configuration first ([quickstart.py:7-21](../quickstart.py)) — `MODEL`,
`MAX_TOKENS`, `MAX_CONTINUATIONS`, `TOOLS`, `USER_QUERY`. Everything a reader is
likely to change sits at the top, before any logic.

Then, in dependency order:

| Function | Does |
|---|---|
| `field` | Reads an attribute whether the SDK returned a model or a plain dict |
| `block_key` | Identity for a content block, used to spot re-sent ones |
| `merge_blocks` | Folds a resumed turn into what is already held — [the open question](pause-turn.md) |
| `request` | One Messages call, optionally resuming a paused turn |
| `run` | Drives `request` through `pause_turn` continuations |
| `collect` | Pulls the search queries issued and the sources returned |
| `main` | Error handling, `stop_reason` interpretation, output |

`field` and `block_key` exist to keep the interesting functions readable. If a
change makes either of them more complicated, that is usually a sign the change
belongs somewhere else.

### The side-effect boundary

`client` appears in exactly three places: `main` constructs one, `run` passes it
along, `request` calls it. **There is one network seam in the whole program.**
Nothing below `request` — `field`, `block_key`, `merge_blocks`, `collect` — knows
a client exists.

A consequence worth preserving: `merge_blocks`, the most intricate logic here and
the subject of the repo's [open question](pause-turn.md), is on the pure side. It
takes two lists and returns one. Folded into `run`, every test of it would have
needed a client.

`main` owns the other boundary — all six `sys.exit` calls, the client
construction, and all output but one line. `run` returns `(response, blocks)` and
lets `main` decide what a `stop_reason` means.

The one leak is [quickstart.py:104](../quickstart.py): `collect` prints the
`[web_search error: ...]` line rather than returning it, making it the only
function below the boundary with an effect. The reason is that search failures
arrive inline among successful results and `collect` is the only place walking
them; the alternative widens its return signature for a case that ideally never
fires. Arguable, but deliberate — and it is why testing that one branch needs
stdout capture when nothing else does.

### `blocks` is the only state

One list, created in `run` and threaded as a plain argument. `response` is
transient, rebound each iteration. Nothing is stored on a module, a class, or the
client, so tests need no setup or teardown and cannot contaminate each other. The
module-level names are constants; nothing writes to them.

Note `blocks` is walked twice for two different purposes: `collect` extracts
queries and sources, then `main` walks it again for the answer text. `collect`
does not return the answer.

## What CI actually proves

The lint job runs `ruff`. The test job runs `pytest --cov` on three Python
versions, failing below 100% coverage. Every function in the table above is
covered; [testing.md](testing.md) says how.

So a green tick means the logic behaves, on three Python versions. What it still
cannot tell you is whether any of it works against the real API — every test runs
against a fake client, and none of them spend money.

## Verifying by hand

```bash
.venv/Scripts/python.exe quickstart.py   # Linux/macOS: .venv/bin/python
```

This spends real money: Opus 5 tokens plus server-side searches, once per run.
There is no dry-run mode and no recorded fixture to replay — adding one would
mean a mocking layer, which the scope rules rule out.

Report what you observed rather than that it "worked". The useful facts are the
final `stop_reason`, whether a `pause_turn` occurred, and how many searches ran.
The PR template asks for exactly this.

## What is hard to verify

Three paths are difficult or impossible to reach deliberately:

- **Which resume shape the API sends** — the unit tests cover both, but
  establishing which one actually occurs needs a genuine pause, and there is no
  supported way to force one; see [pause-turn.md](pause-turn.md).
- **The `MAX_CONTINUATIONS` cap against the real API** — the loop is tested with
  a fake client, but observing it genuinely needs five consecutive pauses.
- **A real refusal** — the handling is tested with a fake response, but
  provoking one needs a query the model declines, which is not something to go
  fishing for.

A change that alters any of these cannot be honestly described as tested. Say so
in the PR rather than implying otherwise; an accurate "unverified" is worth more
than a confident guess.
