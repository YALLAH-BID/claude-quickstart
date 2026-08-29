"""Tests for main(): error mapping, stop_reason interpretation, and output.

main() takes no arguments and builds its own client, so these patch
`quickstart.run` and the client constructor. That keeps each test about main's
own responsibility -- which message comes out for a given input -- rather than
re-testing the loop, which test_run.py already covers.
"""

from types import SimpleNamespace

import anthropic
import pytest
from conftest import Block, Response

import quickstart
from quickstart import MAX_CONTINUATIONS, MAX_TOKENS, main


@pytest.fixture(autouse=True)
def never_a_real_client(monkeypatch):
    """main() constructs a client first thing; no test should build a real one."""
    monkeypatch.setattr(quickstart.anthropic, "Anthropic", lambda *a, **k: object())


def returning(response, blocks=()):
    return lambda _client: (response, list(blocks))


def raising(exc):
    def _run(_client):
        raise exc

    return _run


def api_error(cls, status=400, message="boom"):
    """Build an SDK error without needing a real HTTP response."""
    response = SimpleNamespace(status_code=status, headers={}, request=None)
    return cls(message, response=response, body=None)


# --- error handling -------------------------------------------------------


def test_the_ordering_these_handlers_depend_on():
    """Documents why the except chain is ordered, not merely that it is.

    Three of the four subclass APIStatusError, so a lone APIStatusError handler
    placed first would swallow all of them.
    """
    for cls in (
        anthropic.AuthenticationError,
        anthropic.BadRequestError,
        anthropic.RateLimitError,
    ):
        assert issubclass(cls, anthropic.APIStatusError)
    assert not issubclass(anthropic.APIConnectionError, anthropic.APIStatusError)


@pytest.mark.parametrize(
    "exc, expected",
    [
        (api_error(anthropic.AuthenticationError, 401), "Authentication failed"),
        (api_error(anthropic.BadRequestError, 400), "Request rejected."),
        (api_error(anthropic.RateLimitError, 429), "Rate limited"),
        (api_error(anthropic.APIStatusError, 500), "API returned 500"),
    ],
    ids=["401", "400", "429", "500"],
)
def test_each_error_gets_its_own_message(monkeypatch, exc, expected):
    monkeypatch.setattr(quickstart, "run", raising(exc))

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert expected in str(excinfo.value.code)


def test_a_bad_request_is_not_reported_as_a_generic_status_error(monkeypatch):
    """The specific handler must win -- this is what the ordering buys."""
    monkeypatch.setattr(
        quickstart, "run", raising(api_error(anthropic.BadRequestError, 400))
    )

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert "Request rejected." in str(excinfo.value.code)
    assert "API returned" not in str(excinfo.value.code)


def test_connection_errors_are_reported(monkeypatch):
    exc = anthropic.APIConnectionError(request=SimpleNamespace())
    monkeypatch.setattr(quickstart, "run", raising(exc))

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert "Could not reach the API." in str(excinfo.value.code)


# --- refusal, which is HTTP 200 and raises nothing -------------------------


def test_a_refusal_exits_with_its_category(monkeypatch):
    response = Response(stop_reason="refusal", stop_details={"category": "cyber"})
    monkeypatch.setattr(quickstart, "run", returning(response))

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert "Model declined (cyber)" in str(excinfo.value.code)


def test_a_refusal_without_details_still_exits(monkeypatch):
    monkeypatch.setattr(quickstart, "run", returning(Response(stop_reason="refusal")))

    with pytest.raises(SystemExit):
        main()


# --- partial answers are labelled before they are printed -----------------


def test_hitting_the_pause_cap_is_labelled_and_still_prints(monkeypatch, capsys):
    answer = Block("text", text="partial answer")
    response = Response([answer], stop_reason="pause_turn")
    monkeypatch.setattr(quickstart, "run", returning(response, [answer]))

    main()

    out = capsys.readouterr().out
    assert f"still paused after {MAX_CONTINUATIONS} continuations" in out
    assert "partial answer" in out
    assert out.index("still paused") < out.index("partial answer")


def test_truncation_is_labelled_and_still_prints(monkeypatch, capsys):
    answer = Block("text", text="cut off here")
    response = Response([answer], stop_reason="max_tokens")
    monkeypatch.setattr(quickstart, "run", returning(response, [answer]))

    main()

    out = capsys.readouterr().out
    assert f"{MAX_TOKENS}-token cap" in out
    assert "cut off here" in out


def test_a_complete_answer_carries_no_marker(monkeypatch, capsys):
    answer = Block("text", text="the answer")
    monkeypatch.setattr(quickstart, "run", returning(Response([answer]), [answer]))

    main()

    out = capsys.readouterr().out
    assert "[incomplete" not in out
    assert "[truncated" not in out
    assert "the answer" in out


# --- output ---------------------------------------------------------------


def test_queries_and_sources_are_reported_around_the_answer(monkeypatch, capsys):
    blocks = [
        Block("server_tool_use", input={"query": "solar output"}),
        Block(
            "web_search_tool_result",
            content=[{"url": "https://example.com", "title": "Example"}],
        ),
        Block("text", text="the answer"),
    ]
    monkeypatch.setattr(quickstart, "run", returning(Response(blocks), blocks))

    main()

    out = capsys.readouterr().out
    assert out.index("Searched for:") < out.index("the answer")
    assert out.index("the answer") < out.index("Sources:")
    assert "solar output" in out
    assert "https://example.com" in out


def test_sections_are_omitted_when_empty(monkeypatch, capsys):
    answer = Block("text", text="just prose")
    monkeypatch.setattr(quickstart, "run", returning(Response([answer]), [answer]))

    main()

    out = capsys.readouterr().out
    assert "Searched for:" not in out
    assert "Sources:" not in out


def test_only_text_blocks_with_text_are_printed(monkeypatch, capsys):
    blocks = [
        Block("thinking", text="internal reasoning"),
        Block("text", text=""),
        Block("text", text="visible"),
    ]
    monkeypatch.setattr(quickstart, "run", returning(Response(blocks), blocks))

    main()

    out = capsys.readouterr().out
    assert "visible" in out
    assert "internal reasoning" not in out
