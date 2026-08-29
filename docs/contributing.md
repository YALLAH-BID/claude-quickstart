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

## What CI actually proves

The lint job runs `ruff`. The test job installs dependencies and runs `pytest` on
three Python versions.

`merge_blocks`, `collect` and `run` are covered — both resume shapes, the HTTP-200
search-error path, and the continuation cap. `field`, `block_key` and `request`
are exercised through them.

**`main` is the only function CI never executes**, since it sits behind
`if __name__ == "__main__"` and importing the module runs the constants and stops.

So a green tick now means rather more than it did: the logic behaves, on three
Python versions. What it still cannot tell you is whether any of it works against
the real API — every test runs on a fake client, and none of them spend money.

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
- **`stop_reason == "refusal"`** — needs a query the model declines, which is
  not something to go fishing for.

A change that alters any of these cannot be honestly described as tested. Say so
in the PR rather than implying otherwise; an accurate "unverified" is worth more
than a confident guess.
