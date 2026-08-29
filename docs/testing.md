# Testing

52 tests across four files. Every function in
[`quickstart.py`](../quickstart.py) is covered, and coverage is enforced at 100%
in CI.

| File | Tests | Covers |
|---|---|---|
| [test_merge_blocks.py](../tests/test_merge_blocks.py) | 11 | both resume shapes, and how blocks are told apart |
| [test_collect.py](../tests/test_collect.py) | 14 | queries, sources, and the HTTP-200 search-error path |
| [test_run.py](../tests/test_run.py) | 12 | the continuation loop, the cap, what a resume actually sends |
| [test_main.py](../tests/test_main.py) | 15 | error-handler ordering, `stop_reason` handling, output |

`field`, `block_key` and `request` have no file of their own; they are exercised
through the four above.

## Running them

```bash
pytest                 # the suite
pytest --cov           # with the coverage gate CI applies
pytest tests/test_run.py -v
```

## Fakes, not mocks

[`tests/conftest.py`](../tests/conftest.py) holds three stand-ins:

- **`Block`** — a content block: `.type` plus whatever a test needs. The code
  reads `.type` directly and everything else through `getattr` or `field()`, so
  that is the entire contract.
- **`Response`** — `.content`, `.stop_reason`, `.stop_details`.
- **`FakeClient`** — hands back a scripted sequence of responses **and records
  the requests it received**.

That last part is what makes them fakes rather than mocks. A mock lets you assert
that a function was called; `FakeClient.calls` lets a test assert on the message
list actually sent — that a resume replays the accumulated assistant turn, and
that no `"Continue."` message follows it. Those are claims about behaviour, not
about test setup.

The fake also raises if `run()` asks for more responses than were scripted, so a
runaway loop fails a test instead of hanging CI.

The reason a fake this small is enough: the program has exactly one network seam.
`request` is the only function that touches the wire, so substituting a client at
that single point puts the entire continuation loop under test — which is why
there is no HTTP mocking library here and no recorded fixtures. See
[contributing.md](contributing.md#the-side-effect-boundary).

## Mutation testing

Coverage says a line executed. It does not say a test would notice if that line
were wrong — a suite that called every function and asserted nothing would still
report 100%.

So every test file here was checked by deliberately breaking the code and
confirming a failure. Fifteen mutations, fifteen caught:

| Broken behaviour | Tests that failed |
|---|---|
| Never detect a cumulative resume | 2 |
| Always replace instead of appending | 5 |
| Drop query deduplication | 1 |
| Drop the title→url fallback | 1 |
| Drop the empty-url guard | 1 |
| Remove the continuation cap | 2 |
| Never resume a paused turn | 9 |
| Append a `"Continue."` message on resume | 1 |
| Clobber blocks instead of merging | 2 |
| Send an assistant turn on the first request | 1 |
| Drop the specific `BadRequestError` handler | 2 |
| Stop checking for `refusal` | 2 |
| Print every block rather than text ones | 2 |
| Always print the "Searched for" header | 1 |
| Drop the `max_tokens` label | 1 |

Worth singling out two. Dropping the `BadRequestError` handler makes the code
report `API returned 400` instead of `Request rejected.` — which is exactly the
claim the comment above that `except` chain makes, now enforced rather than
merely asserted. And appending a `"Continue."` message is caught by a single
test, protecting an invariant that three documents describe and nothing else
guards.

**Do this for any test you add.** Break the thing you think you are covering. If
nothing fails, the test is decoration:

```bash
# edit quickstart.py to break the behaviour
pytest -q tests/test_thing.py     # expect a failure
git checkout -- quickstart.py
```

## Coverage

100%, enforced by `fail_under` in `pyproject.toml`, so a drop fails the build.

The `if __name__ == "__main__":` guard is excluded. It cannot run under test —
executing it means running the program — so counting it would make 100% a target
nobody could reach.

The README badge is a static image rather than a live measurement. That is
normally a bad idea, and it is acceptable here only because the gate makes an
overstatement impossible: coverage cannot fall below the number without CI going
red first.

## What the suite cannot tell you

Every test runs against a fake client. None spend money, and none touch the API.

So the suite establishes that both resume shapes are handled correctly. It cannot
establish **which** shape the API actually sends — that is a fact about
Anthropic's service, and no fake can reach it. See
[pause-turn.md](pause-turn.md).
