"""Tests for run(), the pause_turn continuation loop.

These need a fake client, so they are a step less direct than the merge_blocks
and collect tests. What they exercise is still real: the loop, the cap, and the
message structure sent on a resume -- including the documented invariant that no
"Continue." message is appended. See docs/pause-turn.md.
"""

import pytest
from conftest import Block, FakeClient, Response

from quickstart import MAX_CONTINUATIONS, MAX_TOKENS, MODEL, TOOLS, USER_QUERY, run


def paused(*content):
    return Response(content, stop_reason="pause_turn")


def done(*content):
    return Response(content, stop_reason="end_turn")


# --- the ordinary path ----------------------------------------------------


def test_a_completed_turn_makes_one_request():
    client = FakeClient(done(Block("text", text="answer")))

    response, blocks = run(client)

    assert len(client.calls) == 1
    assert response.stop_reason == "end_turn"
    assert [b.text for b in blocks] == ["answer"]


def test_the_first_request_carries_only_the_user_query():
    client = FakeClient(done(Block("text", text="x")))

    run(client)

    assert client.calls[0]["messages"] == [{"role": "user", "content": USER_QUERY}]


def test_request_parameters():
    client = FakeClient(done())

    run(client)

    call = client.calls[0]
    assert call["model"] == MODEL
    assert call["max_tokens"] == MAX_TOKENS
    assert call["tools"] == TOOLS


# --- resuming -------------------------------------------------------------


def test_a_pause_is_resumed():
    client = FakeClient(
        paused(Block("server_tool_use", id="s1")),
        done(Block("text", text="answer")),
    )

    response, _ = run(client)

    assert len(client.calls) == 2
    assert response.stop_reason == "end_turn"


def test_the_resume_replays_the_accumulated_turn():
    first = Block("server_tool_use", id="s1")
    client = FakeClient(paused(first), done(Block("text", text="answer")))

    run(client)

    resumed = client.calls[1]["messages"]
    assert resumed[0] == {"role": "user", "content": USER_QUERY}
    assert resumed[1]["role"] == "assistant"
    assert resumed[1]["content"] == [first]


def test_the_resume_appends_no_continue_message():
    """The API resumes from the trailing server_tool_use block.

    An extra user turn would change what it resumes from, so the message list
    must end with the assistant turn.
    """
    client = FakeClient(
        paused(Block("server_tool_use", id="s1")),
        done(Block("text", text="answer")),
    )

    run(client)

    roles = [m["role"] for m in client.calls[1]["messages"]]
    assert roles == ["user", "assistant"]


def test_thinking_blocks_are_replayed_unchanged():
    """Required when resuming on the same model; Opus 5 thinks by default."""
    thinking = Block("thinking", text="reasoning")
    client = FakeClient(
        paused(thinking, Block("server_tool_use", id="s1")),
        done(Block("text", text="answer")),
    )

    run(client)

    assert client.calls[1]["messages"][1]["content"][0] is thinking


# --- how the two resume shapes come out -----------------------------------


def test_a_cumulative_resume_does_not_duplicate_blocks():
    a = Block("server_tool_use", id="s1")
    b = Block("text", text="answer")
    client = FakeClient(paused(a), done(a, b))  # second response re-sends `a`

    _, blocks = run(client)

    assert blocks == [a, b]


def test_an_incremental_resume_appends():
    a = Block("server_tool_use", id="s1")
    b = Block("text", text="answer")
    client = FakeClient(paused(a), done(b))  # second response carries only `b`

    _, blocks = run(client)

    assert blocks == [a, b]


# --- the cap --------------------------------------------------------------


def test_it_gives_up_at_the_continuation_cap():
    always_paused = [paused(Block("server_tool_use", id=f"s{i}")) for i in range(20)]
    client = FakeClient(*always_paused)

    response, _ = run(client)

    assert len(client.calls) == MAX_CONTINUATIONS + 1
    assert response.stop_reason == "pause_turn"


def test_the_cap_still_returns_what_was_gathered():
    responses = [paused(Block("text", text=f"part{i}")) for i in range(20)]
    client = FakeClient(*responses)

    _, blocks = run(client)

    assert [b.text for b in blocks] == [
        f"part{i}" for i in range(MAX_CONTINUATIONS + 1)
    ]


def test_the_fake_rejects_unscripted_requests():
    """Guards the tests above: an unbounded loop would surface here, not hang."""
    client = FakeClient(paused(Block("text", text="x")))

    with pytest.raises(AssertionError, match="scripted"):
        run(client)
