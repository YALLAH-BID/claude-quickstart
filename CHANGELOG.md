# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Releases are tagged `vX.Y.Z` and take a version heading here. Anything not yet
released sits under **Unreleased** — which is currently all of it, since no tag
has been cut.

**What gets logged here:** changes that alter what someone running the example
sees or has to understand — the model or tool version it targets, how it handles
a response, a corrected explanation of API behaviour.

**What doesn't:** repository scaffolding — CI, linters, templates, dependency
pins. `git log` already covers those, and duplicating them here would bury the
entries that matter.

## Unreleased

### Added

- Initial example. Calls the Messages API with the server-side
  `web_search_20260209` tool on `claude-opus-5`, resumes `pause_turn`
  continuations, streams to avoid the SDK's HTTP timeout re-running searches,
  and handles refusals and server-tool errors — both of which arrive as HTTP 200
  rather than exceptions.

### Unverified

- `merge_blocks()` copes with a resumed turn that is either cumulative (the whole
  turn so far, re-sent) or incremental (only the new segment) by detecting which
  it received, so it is correct either way. Which shape the API actually returns
  has never been observed against a live paused turn.
