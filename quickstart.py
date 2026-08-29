"""Claude Messages API quickstart: web search with pause_turn handling."""

import sys

import anthropic

MODEL = "claude-opus-5"
MAX_TOKENS = 16000
MAX_CONTINUATIONS = 5

# Server-side tool: runs on Anthropic's infrastructure, so there is no
# function to implement and no tool-execution loop to write.
# The _20260209 version does dynamic filtering (Claude filters results in code
# before they reach the context window). Do not also declare code_execution --
# that creates a second execution environment and confuses the model.
TOOLS = [{"type": "web_search_20260209", "name": "web_search"}]

USER_QUERY = (
    "What are the latest developments in renewable energy? "
    "Search the web and summarize the most significant recent ones."
)


def field(obj, name):
    """Read a field whether the SDK hands back a model or a plain dict."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def block_key(block):
    """Identity for a content block, used to spot re-sent blocks."""
    return (block.type, getattr(block, "id", None), getattr(block, "text", None))


def merge_blocks(accumulated, new_blocks):
    """Fold a resumed turn's content into what we already hold.

    A resumed turn may return the whole turn so far (cumulative) or only the
    new segment (incremental); this has not been observed against a live
    paused turn. Rather than assume, detect: if the new content re-sends the
    blocks we already hold, treat it as cumulative and replace; otherwise
    extend. Correct either way.
    """
    new_blocks = list(new_blocks)
    if not accumulated:
        return new_blocks
    head = [block_key(b) for b in new_blocks[: len(accumulated)]]
    if head == [block_key(b) for b in accumulated]:
        return new_blocks
    return accumulated + new_blocks


def request(client, blocks):
    """One Messages call. `blocks` is the assistant turn to resume, if any."""
    messages = [{"role": "user", "content": USER_QUERY}]
    if blocks:
        # Resuming: the API picks up from the trailing server_tool_use block,
        # so do NOT append a "Continue." message. Thinking blocks are echoed
        # back unchanged, which is required when resuming on the same model.
        messages.append({"role": "assistant", "content": blocks})
    # Streaming: the server-side search loop can run long, and a non-streaming
    # request risks the SDK's HTTP timeout -- which then retries and re-runs
    # the searches before surfacing anything.
    with client.messages.stream(
        model=MODEL, max_tokens=MAX_TOKENS, tools=TOOLS, messages=messages
    ) as stream:
        return stream.get_final_message()


def run(client):
    """Drive the request through any pause_turn continuations."""
    blocks = []
    response = request(client, blocks)
    blocks = merge_blocks(blocks, response.content)

    continuations = 0
    while response.stop_reason == "pause_turn" and continuations < MAX_CONTINUATIONS:
        continuations += 1
        response = request(client, blocks)
        blocks = merge_blocks(blocks, response.content)
    return response, blocks


def collect(blocks):
    """Pull the search queries Claude issued and the sources it got back."""
    queries, sources = [], {}
    for block in blocks:
        if block.type == "server_tool_use":
            query = field(block.input, "query")
            if query and query not in queries:
                queries.append(query)
        elif block.type == "web_search_tool_result":
            results = block.content
            # Success -> list of web_search_result. Error -> a single object
            # with an error_code. Server tool errors return HTTP 200, so this
            # branch is the only place they surface.
            if isinstance(results, list):
                for result in results:
                    url = field(result, "url")
                    if url and url not in sources:
                        sources[url] = field(result, "title") or url
            else:
                print(f"[web_search error: {field(results, 'error_code') or results}]")
    return queries, sources


def main():
    client = anthropic.Anthropic()

    # Most specific first: AuthenticationError, BadRequestError and
    # RateLimitError all subclass APIStatusError, so a lone APIStatusError
    # handler would swallow the distinction between retryable and terminal.
    try:
        response, blocks = run(client)
    except anthropic.AuthenticationError as exc:
        sys.exit(f"Authentication failed -- try `ant auth login`.\n  {exc}")
    except anthropic.BadRequestError as exc:
        sys.exit(f"Request rejected.\n  {exc}")
    except anthropic.RateLimitError as exc:
        sys.exit(f"Rate limited -- retry later.\n  {exc}")
    except anthropic.APIStatusError as exc:
        sys.exit(f"API returned {exc.status_code}.\n  {exc}")
    except anthropic.APIConnectionError as exc:
        sys.exit(f"Could not reach the API.\n  {exc}")

    # Opus 5 can decline via stop_reason "refusal" (HTTP 200, not an exception).
    if response.stop_reason == "refusal":
        sys.exit(f"Model declined ({field(response.stop_details, 'category')}).")

    # Both of these leave a partial answer worth printing -- but say so first,
    # or truncated output reads as a complete one.
    if response.stop_reason == "pause_turn":
        print(
            f"[incomplete: still paused after {MAX_CONTINUATIONS} continuations]\n"
        )
    elif response.stop_reason == "max_tokens":
        print(f"[truncated: hit the {MAX_TOKENS}-token cap mid-answer]\n")

    queries, sources = collect(blocks)

    if queries:
        print("Searched for:")
        for query in queries:
            print(f"  - {query}")
        print()

    for block in blocks:
        if block.type == "text" and block.text:
            print(block.text)

    if sources:
        print("\nSources:")
        for url, title in sources.items():
            print(f"  - {title}\n    {url}")


if __name__ == "__main__":
    main()
