# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What lives here

Three unrelated things share the repo. Work out which one you are touching before anything else — the rules differ.

| Path | What it is |
|---|---|
| `quickstart.py`, `tests/`, `docs/` | The actual project: a ~157-line Claude Messages API example using the server-side `web_search_20260209` tool. Scope is tightly policed (see below). |
| `stock_report/pipeline.py` | Standalone daily used-vehicle stock pipeline (two Excel exports in, two workbooks out). `openpyxl` only, no tests. |
| `tools/tableau_deep_dig*` | Four ports of one Tableau Server site-audit tool: Python, PowerShell, and two browser-console variants. Run inside a corporate network; only the generated `report.md` / `site_inventory.json` come back out. |

`ruff` lints the whole repo. `pytest --cov` only covers `quickstart.py` — `[tool.coverage.run] source = ["quickstart"]`.

## Commands

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt   # Windows: .venv/Scripts/
.venv/bin/pre-commit install     # optional; mirrors the CI lint job
```

```bash
ruff check .                     # CI lint job
ruff format --check .            # CI lint job
pytest -q --cov                  # CI test job; fails below 100% coverage
pytest tests/test_run.py -v      # one file
pytest tests/test_run.py::test_name   # one test
```

CI runs lint on 3.12 and the tests on 3.10, 3.12 and 3.14.

The example itself — **spends real money on every run (Opus 5 tokens plus server-side searches), so only run it when asked to:**

```bash
.venv/bin/python quickstart.py
```

Stock report pipeline (needs `pip install openpyxl`, and `libreoffice-calc` for the recalc step):

```bash
python3 stock_report/pipeline.py --stocklist "<stocklist>.xlsx" --rawdata "<raw data>.xlsx" --outdir out
```

## quickstart.py — the shape to preserve

**Scope is a hard constraint, not a preference.** `CONTRIBUTING.md` rules out new features, CLI flags, configuration layers, splitting the file into modules, and any dependency beyond `anthropic`. The file earns its keep by being readable start to finish. Fixes to how the API is used, comments explaining *why*, and doc corrections are what belong.

**One network seam.** `main` builds the client, `run` passes it, `request` calls it — that is every mention of `client` in the program. `field`, `block_key`, `merge_blocks` and `collect` are pure and know nothing about a client, which is why the tests need one small fake instead of an HTTP mocking layer. Keep new logic on the pure side.

**`main` owns the effects** — all six `sys.exit` calls and all output. The one deliberate exception is `collect` printing `[web_search error: ...]` inline (`quickstart.py:104`), because search failures arrive mixed in with successful results.

**`blocks` is the only state**: one list, created in `run`, threaded as a plain argument. Nothing is stored on a module, class or client.

**Three failure modes arrive as HTTP 200, not exceptions** — a `web_search_tool_result` whose `content` is a single error object instead of a list, `stop_reason == "refusal"`, and `stop_reason` of `pause_turn`/`max_tokens` leaving a partial answer. Each is handled explicitly; do not collapse them into the exception path.

**Error handlers are ordered most-specific-first.** `AuthenticationError`, `BadRequestError` and `RateLimitError` all subclass `APIStatusError`, so a lone `APIStatusError` handler erases the retryable/terminal distinction. A test catches this exact regression.

**Resuming a paused turn:** replay the accumulated assistant blocks, do *not* append a `"Continue."` message (the API picks up from the trailing `server_tool_use` block), and echo `thinking` blocks back unchanged.

**The open question.** `merge_blocks` handles a resumed turn that is either cumulative or incremental by detecting which arrived. Which one the API actually sends has never been observed against a live paused turn — see `docs/pause-turn.md`. Never write anything that implies this is settled.

## Testing conventions

- `tests/conftest.py` holds fakes, not mocks: `FakeClient` records the requests it received, so tests assert on the message list actually sent. It raises if `run()` asks for more responses than were scripted, so a runaway loop fails instead of hanging CI.
- **Mutation-check every test you add.** Break the behaviour in `quickstart.py`, confirm the test fails, then `git checkout -- quickstart.py`. `docs/testing.md` carries the table of fifteen mutations already verified. A test nothing can break is decoration.
- Coverage is enforced at 100% with only the `if __name__ == "__main__":` guard excluded, so a new uncovered branch turns CI red.

## Repo conventions that are easy to get wrong

- **CI never runs `quickstart.py`.** A green tick means it parses, lints and imports — nothing about the real API. Any change to runtime behaviour has to be verified by hand against a real key, and the PR must say what was observed (final `stop_reason`, whether a `pause_turn` occurred, how many searches ran). An honest "unverified" beats a confident guess.
- **`CHANGELOG.md` is only for changes someone running the example would notice** — model or tool version, response handling, a corrected explanation of API behaviour. Repository scaffolding (CI, linters, templates, pins) deliberately does not go in.
- **`pyproject.toml` has no `[project]` table on purpose** — this is not a package. Dependencies live in `requirements.txt` (runtime, `anthropic` only) and `requirements-dev.txt` (pinned tooling); CI and Dependabot read those files.
- **The ruff pin exists twice** — `requirements-dev.txt` and the `rev` in `.pre-commit-config.yaml`. They are separate on purpose and must be bumped together; Dependabot cannot move the pre-commit one.
- **`stock_report/pipeline.py` is the only file with `E501` ignored**, because it embeds Excel formula strings that cannot be wrapped. Every other rule still applies to it.
- `docs/README.md` is a map, not a copy: where a summary disagrees with the file it points at, the file wins. Prefer fixing the source over syncing the summary.

## stock_report specifics

- `pipeline.py` fails fast and exits non-zero if its built-in verification fails. **Do not deliver the outputs when it does.**
- Recalculate only `Automall_Stock_Analysis_<date>.xlsx` with LibreOffice. Never run LibreOffice over `Stock_Raw_data_<date>_REFRESHED.xlsx` — it carries external links to network files and is written with `fullCalcOnLoad` so Excel recalculates it on open.
- The refresh edits sheet XML directly so pivot tables, external links and every untouched sheet stay byte-for-byte identical. Column positions live in `RECON_FIELDS` and `REFRESH_TARGETS` at the top of the script; if an export layout moves, fix it there.
- Deliberately left alone by the refresh: availability wording, vehicle-usage codes (`LV`/`LCV`), and all non-Automall rows. Those surface as `Convention` rows on the RECONCILIATION sheet instead of being overwritten.
