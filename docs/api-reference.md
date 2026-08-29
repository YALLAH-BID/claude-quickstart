# The API surface this example uses

Scoped deliberately. This covers only what
[`quickstart.py`](../quickstart.py) touches and the behaviours it depends on —
the facts you need to read the code without a second tab open.

It is **not** general Claude API documentation. For anything beyond this
example's surface, [the official docs](https://platform.claude.com/docs/en/home)
are authoritative and this page is not; duplicating them here would only produce
a copy that goes stale.

## The request

One endpoint, `POST /v1/messages`, reached through `client.messages.stream()`.

| Parameter | Value here | Note |
|---|---|---|
| `model` | `claude-opus-5` | $5 / $25 per 1M tokens in/out |
| `max_tokens` | `16000` | Opus 5 allows up to 128000, but large values need streaming |
| `tools` | one server tool | see below |
| `messages` | the query, plus the assistant turn when resuming | |

**Streaming is not a style choice.** A server-side search loop can outlast the
SDK's default 10-minute HTTP timeout. A timed-out non-streaming request is
retried by the SDK, which re-runs every search before anything surfaces.
`.get_final_message()` gives back the assembled message, so the code reads like
a non-streaming call.

### Not passed, deliberately

`thinking` is never set. On Claude Opus 5 thinking is **on by default** —
omitting the parameter runs adaptive thinking. This matters when resuming: the
response carries `thinking` blocks that must be echoed back unchanged, even
though the code never asked for them.

`budget_tokens` is removed on Opus 5 and returns 400. Assistant prefill is also
rejected. Depth is controlled by `output_config.effort` (`low` through `max`,
default `high`), which this example leaves at the default.

## The tool

```python
TOOLS = [{"type": "web_search_20260209", "name": "web_search"}]
```

Server-side: it runs on Anthropic's infrastructure, so there is no function to
implement and no execute-and-loop cycle to write. Results arrive as content
blocks in the same response.

The `_20260209` variant does dynamic filtering — Claude filters results in code
before they reach the context window. **Do not also declare `code_execution`**:
that creates a second execution environment and confuses the model. It requires
Opus 5/4.8/4.7/4.6 or Sonnet 5/4.6; older models take `web_search_20250305`.

Optional keys this example does not set: `max_uses`, `allowed_domains` /
`blocked_domains` (never both), `user_location`.

## Response blocks

`response.content` is a list. The types this code handles:

| Type | Carries |
|---|---|
| `thinking` | reasoning; text is empty unless `display: "summarized"` |
| `server_tool_use` | the search Claude issued — `.input["query"]` |
| `web_search_tool_result` | `.content` — see the branch below |
| `text` | the answer |

**`web_search_tool_result.content` is not one shape.** On success it is a *list*
of `web_search_result`. On failure it is a single *object* with an `error_code`
such as `max_uses_exceeded`. Branch on that before indexing — `collect()` does.

## Stop reasons

Three of the possible values arrive as **HTTP 200 with no exception raised**, so
nothing surfaces them unless the code checks:

| `stop_reason` | Means | Handled by |
|---|---|---|
| `end_turn` | finished normally | — |
| `pause_turn` | incomplete, resumable | the continuation loop; [details](pause-turn.md) |
| `max_tokens` | truncated mid-answer | labelled before printing the partial |
| `refusal` | the model declined | reported with `stop_details.category` |

`stop_details` is populated **only** when `stop_reason` is `refusal`. It is
`None` for every other value, so guard before reading it.

## Errors that do raise

Ordered most-specific-first in `main()`, because `AuthenticationError`,
`BadRequestError` and `RateLimitError` all subclass `APIStatusError` — a lone
`APIStatusError` handler would erase the difference between retryable and
terminal failures.

One worth recognising: `Your credit balance is too low to access the Anthropic
API` arrives as a `BadRequestError` (400). It looks like an auth failure and is
not one — the request authenticated successfully. It is a billing state.
