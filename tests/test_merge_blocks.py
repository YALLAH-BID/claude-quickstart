"""Tests for merge_blocks, the one function here worth unit-testing.

It is pure -- two sequences in, a list out, no client and no I/O -- and it
carries the branch that has never executed against a live paused turn. Which
shape the API actually sends is still unknown (docs/pause-turn.md); these tests
establish that both are handled, which is a different and answerable question.
"""

import pytest

from quickstart import merge_blocks


class Block:
    """Minimal stand-in for an SDK content block.

    block_key() reads .type directly and .id / .text via getattr with a default,
    so those three attributes are the whole contract.
    """

    def __init__(self, type, id=None, text=None):  # noqa: A002
        self.type = type
        self.id = id
        self.text = text


def keys(blocks):
    return [(b.type, b.id, b.text) for b in blocks]


# --- the empty case -------------------------------------------------------


def test_first_response_returns_new_blocks():
    new = [Block("text", text="hello")]
    assert merge_blocks([], new) == new


def test_empty_accumulated_and_empty_new():
    assert merge_blocks([], []) == []


# --- cumulative: the resumed turn re-sends what we already hold ------------


def test_cumulative_replaces_rather_than_duplicating():
    a, b = Block("thinking", text="t"), Block("server_tool_use", id="s1")
    c = Block("text", text="answer")

    result = merge_blocks([a, b], [a, b, c])

    assert result == [a, b, c]  # not [a, b, a, b, c]


def test_cumulative_detected_by_value_not_identity():
    """Re-sent blocks are new objects, so detection must compare keys."""
    accumulated = [Block("thinking", text="t")]
    resent = [Block("thinking", text="t"), Block("text", text="answer")]

    assert keys(merge_blocks(accumulated, resent)) == [
        ("thinking", None, "t"),
        ("text", None, "answer"),
    ]


# --- incremental: the resumed turn continues where it stopped --------------


def test_incremental_appends():
    a = Block("thinking", text="first")
    b = Block("text", text="second")

    assert merge_blocks([a], [b]) == [a, b]


def test_shorter_new_segment_appends():
    """new shorter than accumulated cannot be cumulative -- the head cannot match."""
    a, b, c = Block("a"), Block("b"), Block("c")
    d = Block("d")

    assert merge_blocks([a, b, c], [d]) == [a, b, c, d]


# --- what makes two blocks distinct ---------------------------------------


@pytest.mark.parametrize(
    "first, second",
    [
        (Block("server_tool_use", id="s1"), Block("server_tool_use", id="s2")),
        (Block("text", text="one"), Block("text", text="two")),
        (Block("text"), Block("thinking")),
    ],
    ids=["differing id", "differing text", "differing type"],
)
def test_blocks_differing_in_any_key_field_are_not_a_match(first, second):
    """A near-miss must be treated as incremental, never silently replaced."""
    assert merge_blocks([first], [second]) == [first, second]


# --- contract details -----------------------------------------------------


def test_accepts_any_iterable():
    a = Block("text", text="x")
    assert merge_blocks([], (b for b in [a])) == [a]


def test_does_not_mutate_its_arguments():
    accumulated = [Block("thinking", text="t")]
    new = [Block("text", text="answer")]
    before = (list(accumulated), list(new))

    merge_blocks(accumulated, new)

    assert (accumulated, new) == before
