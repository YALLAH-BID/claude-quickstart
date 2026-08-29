"""Tests for collect().

Unlike merge_blocks it is not pure -- the server-tool error path prints -- so
those cases assert on captured output. That path matters more than its size
suggests: search failures arrive as HTTP 200 and this is the only place they
surface.
"""

from types import SimpleNamespace

from conftest import Block

from quickstart import collect

# --- queries --------------------------------------------------------------


def test_no_blocks():
    assert collect([]) == ([], {})


def test_collects_query_from_dict_input():
    blocks = [Block("server_tool_use", input={"query": "solar efficiency"})]
    assert collect(blocks)[0] == ["solar efficiency"]


def test_collects_query_from_object_input():
    """field() has to cope with the SDK returning a model rather than a dict."""
    blocks = [Block("server_tool_use", input=SimpleNamespace(query="grid storage"))]
    assert collect(blocks)[0] == ["grid storage"]


def test_repeated_queries_are_deduplicated_in_order():
    blocks = [
        Block("server_tool_use", input={"query": "first"}),
        Block("server_tool_use", input={"query": "second"}),
        Block("server_tool_use", input={"query": "first"}),
    ]
    assert collect(blocks)[0] == ["first", "second"]


def test_missing_or_empty_query_is_skipped():
    blocks = [
        Block("server_tool_use", input={}),
        Block("server_tool_use", input={"query": ""}),
        Block("server_tool_use", input={"query": "kept"}),
    ]
    assert collect(blocks)[0] == ["kept"]


# --- sources --------------------------------------------------------------


def test_collects_url_and_title():
    result = {"url": "https://example.com/a", "title": "A"}
    blocks = [Block("web_search_tool_result", content=[result])]
    assert collect(blocks)[1] == {"https://example.com/a": "A"}


def test_collects_from_object_results():
    result = SimpleNamespace(url="https://example.com/b", title="B")
    blocks = [Block("web_search_tool_result", content=[result])]
    assert collect(blocks)[1] == {"https://example.com/b": "B"}


def test_first_title_wins_for_a_repeated_url():
    blocks = [
        Block(
            "web_search_tool_result",
            content=[
                {"url": "https://example.com", "title": "first"},
                {"url": "https://example.com", "title": "second"},
            ],
        )
    ]
    assert collect(blocks)[1] == {"https://example.com": "first"}


def test_url_stands_in_for_a_missing_title():
    blocks = [
        Block(
            "web_search_tool_result",
            content=[
                {"url": "https://example.com/no-title"},
                {"url": "https://example.com/blank", "title": ""},
            ],
        )
    ]
    assert collect(blocks)[1] == {
        "https://example.com/no-title": "https://example.com/no-title",
        "https://example.com/blank": "https://example.com/blank",
    }


def test_result_without_a_url_is_skipped():
    blocks = [Block("web_search_tool_result", content=[{"title": "orphan"}])]
    assert collect(blocks)[1] == {}


# --- the HTTP-200 error path ----------------------------------------------


def test_search_error_is_reported_rather_than_raised(capsys):
    blocks = [
        Block("web_search_tool_result", content={"error_code": "max_uses_exceeded"})
    ]

    assert collect(blocks) == ([], {})
    assert "[web_search error: max_uses_exceeded]" in capsys.readouterr().out


def test_unrecognised_error_shape_is_printed_whole(capsys):
    """No error_code -- the payload itself is shown rather than 'None'."""
    blocks = [Block("web_search_tool_result", content={"unexpected": "shape"})]

    collect(blocks)

    assert "'unexpected': 'shape'" in capsys.readouterr().out


def test_an_error_does_not_discard_other_results(capsys):
    blocks = [
        Block("server_tool_use", input={"query": "q"}),
        Block("web_search_tool_result", content={"error_code": "max_uses_exceeded"}),
        Block("web_search_tool_result", content=[{"url": "https://ok", "title": "T"}]),
    ]

    queries, sources = collect(blocks)

    assert queries == ["q"]
    assert sources == {"https://ok": "T"}
    assert "max_uses_exceeded" in capsys.readouterr().out


# --- everything else ------------------------------------------------------


def test_other_block_types_are_ignored():
    blocks = [
        Block("thinking", thinking="..."),
        Block("text", text="the answer"),
    ]
    assert collect(blocks) == ([], {})
