# Contributing

This is a **quickstart**: one file that shows how to call the Claude Messages API
with the server-side web search tool. The small scope is deliberate, and it is the
main thing to keep in mind before opening a pull request.

This page is the policy. [docs/contributing.md](docs/contributing.md) is the
practice — how the code is arranged, and how to verify a change when CI cannot.

## What belongs here

Changes that make the example more correct, clearer, or more current:

- Fixes to how the API is used — a wrong parameter, a stale tool version, a failure
  mode that isn't handled
- Comments explaining *why*, where the reason isn't obvious from the code
- README corrections

## What doesn't

- New features, CLI flags, or configuration layers. The file earns its keep by being
  short enough to read start to finish; every addition costs the reader something.
- Splitting it into modules.
- Dependencies beyond `anthropic`.

If you want a larger, multi-example project, Anthropic's
[claude-quickstarts](https://github.com/anthropics/claude-quickstarts) is the
better home for it.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pre_commit install   # optional, but runs the checks below on every commit
```

`requirements-dev.txt` holds the tooling (`ruff` and `pre-commit`, both pinned). CI installs that same file, so
the version you run locally is the one that gates your PR. It is kept out of
`requirements.txt`, which lists only what the script needs at runtime — people running the
example shouldn't have to install a linter.

## Before you push

If your change alters what someone running the example sees, add a CHANGELOG.md entry
under Unreleased. Scaffolding changes do not need one.

CI runs exactly these, so running them locally first saves a round trip:

```bash
ruff check .
ruff format --check .
pytest --cov
```

The tests cover `merge_blocks` only, and make no API call — `main()` is guarded by
`__name__ == "__main__"`, so importing the module runs nothing.

CI runs the lint on 3.12 and the import on Python 3.10, 3.12, and 3.14.

## What CI does not do

CI never runs `quickstart.py`. Doing so needs API credentials and spends money on
every push. **So any change to runtime behaviour has to be verified by hand** —
run the script against a real key and say in the PR what you saw. A green check
means the code parses and imports, nothing more.

## A known open question

If you trigger a genuine `pause_turn` and can say which shape the API returns, that is
the single most useful contribution to this repo right now.

[docs/pause-turn.md](docs/pause-turn.md) explains what to look for and what to record.
