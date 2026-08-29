## What this changes

<!-- One or two sentences. -->

## Why

<!-- What was wrong, unclear, or missing. -->

## Verification

CI checks that the code parses, formats, and imports. It never runs
`quickstart.py` — that needs an API key and would spend money on every push. So
anything touching runtime behaviour has to be checked by hand.

- [ ] `ruff check .` and `ruff format --check .` pass locally
- [ ] This change cannot affect runtime behaviour (docs, config, comments), **or**
- [ ] I ran `quickstart.py` against a real key and it behaved as expected

<!-- If you ran it, say what you saw — the stop_reason especially, and whether a
     pause_turn occurred. Whether a resumed turn comes back cumulative or
     incremental is this repo's open question, and a real run is the only way to
     answer it. -->

## Scope

This is a quickstart: one file, one dependency, short enough to read in a sitting.
[CONTRIBUTING.md](https://github.com/YALLAH-BID/claude-quickstart/blob/main/CONTRIBUTING.md)
covers what that rules out — features, config layers, module splits, extra
dependencies.
