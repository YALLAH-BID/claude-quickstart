"""Shared stand-ins for SDK objects.

The code under test reads `.type` directly and everything else through
`getattr` or `field()`, so a bare attribute holder is the entire contract.
Keeping one definition here rather than one per test module means the stub
cannot drift away from what the real blocks look like.
"""


class Block:
    """A content block with `.type` plus whatever else a test needs."""

    def __init__(self, type, **attrs):  # noqa: A002
        self.type = type
        self.__dict__.update(attrs)


class Response:
    """A Messages response, reduced to what run() and main() actually read."""

    def __init__(self, content=(), stop_reason="end_turn", stop_details=None):
        self.content = list(content)
        self.stop_reason = stop_reason
        # Populated by the API only for a refusal; None otherwise, which main()
        # relies on when reading the category.
        self.stop_details = stop_details


class _Stream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _Messages:
    def __init__(self, responses):
        self._queue = list(responses)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self._queue:
            raise AssertionError(
                f"run() made {len(self.calls)} requests; the test scripted "
                f"{len(self.calls) - 1}"
            )
        return _Stream(self._queue.pop(0))


class FakeClient:
    """A client that hands back a scripted sequence of responses.

    A fake rather than a mock: it records the requests it received so a test can
    assert on the message structure that was actually sent, instead of asserting
    that a mock returned what the mock was told to return.
    """

    def __init__(self, *responses):
        self.messages = _Messages(responses)

    @property
    def calls(self):
        return self.messages.calls
