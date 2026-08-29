# claude-quickstart

A single-file example of the Claude Messages API using the **server-side web search tool**.

`quickstart.py` asks Claude a question that needs current information, lets it search the web
as many times as it needs, and prints the answer with the queries it ran and the sources it
used. It is deliberately small, but it handles the parts of this API that are easy to get
wrong: paused turns, streaming timeouts, and server-tool errors that never raise.

## Requirements

- Python 3.10+ (the `anthropic` 1.x SDK requires it; this repo is developed on 3.14)
- An Anthropic account with API access

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Linux/macOS: .venv/bin/python
```

The only direct dependency is `anthropic` (developed against 1.2.0); everything else in the venv is transitive.

## Authentication

Either of these works; the script constructs a bare `anthropic.Anthropic()` client, which
resolves credentials from the environment automatically.

```bash
ant auth login
```

That stores a profile the SDK picks up with no environment variable set. Run `ant auth status`
to see which credential is active. Alternatively, export a key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

An unset `ANTHROPIC_API_KEY` does **not** mean you have no credentials — check `ant auth status`
before assuming.

## Run

```bash
.venv/Scripts/python quickstart.py
```

Output has three parts: the search queries Claude issued, the prose answer, and a
deduplicated list of sources.

```
Searched for:
  - recent breakthroughs in solar panel efficiency 2026
  - grid-scale battery storage developments 2026

<the answer>

Sources:
  - <title>
    <url>
```

## How it works

**Server-side tool.** `web_search_20260209` runs on Anthropic's infrastructure. There is no
function to implement and no tool-execution loop — you declare the tool, and search results
arrive as content blocks in the same response. This variant does dynamic filtering (Claude
filters results in code before they reach the context window), which is why the script must
*not* also declare `code_execution`: that would create a second execution environment and
confuse the model. The variant requires Opus 5/4.8/4.7/4.6 or Sonnet 5/4.6.

**`pause_turn`.** A long server-side search loop can pause before finishing. The script
resumes by replaying the assistant blocks it has so far, up to `MAX_CONTINUATIONS` times.
Two details matter: do not append a "Continue." message (the API picks up from the trailing
`server_tool_use` block), and echo `thinking` blocks back unchanged — required when resuming
on the same model. Claude Opus 5 thinks by default, so those blocks are present even though
the script never sets the `thinking` parameter.

**Streaming.** The script streams and calls `.get_final_message()`. A non-streaming request
risks hitting the SDK's 10-minute HTTP timeout, which then retries and re-runs every search
before surfacing anything.

**Server-tool errors return HTTP 200.** A failed search is not an exception. It arrives as a
`web_search_tool_result` block whose `content` is a single error object (e.g.
`{"error_code": "max_uses_exceeded"}`) instead of the usual list of results. `collect()`
branches on that before indexing.

**Error handling is ordered most-specific-first.** `AuthenticationError`, `BadRequestError`,
and `RateLimitError` all subclass `APIStatusError`, so a lone `APIStatusError` handler would
erase the difference between retryable and terminal failures.

**Partial answers are labelled.** `stop_reason` of `pause_turn` (still paused after the
continuation cap) or `max_tokens` (hit the cap mid-sentence) both leave output worth printing,
so the script prints it — but says so first, or truncated output reads as a complete answer.
`refusal` is a third HTTP-200 outcome: Claude declined, and `stop_details.category` says why.

## Tuning

Constants at the top of [quickstart.py](quickstart.py):

| Constant | Default | Notes |
| --- | --- | --- |
| `MODEL` | `claude-opus-5` | $5 / $25 per 1M input/output tokens, 1M context |
| `MAX_TOKENS` | `16000` | Safe to raise — up to 128000 on Opus 5, since this streams |
| `MAX_CONTINUATIONS` | `5` | Cap on `pause_turn` resumes before giving up |
| `USER_QUERY` | renewable energy | The question asked |

The `web_search` tool also accepts `max_uses`, `allowed_domains` / `blocked_domains`, and
`user_location` in its `TOOLS` entry — worth adding if you care about search cost or want to
restrict sources. For cost/quality tuning, `output_config={"effort": "low"|"medium"|"high"|
"xhigh"|"max"}` is the first lever to reach for; `high` is the default.

## Known limitation

`merge_blocks()` handles a resumed turn that returns either the whole turn so far (cumulative)
or only the new segment (incremental). It detects which by checking whether the new content
re-sends blocks already held, so it is correct either way — but the actual behaviour has not
been observed against a live paused turn. If you reproduce one, the detection branch is worth
confirming.
