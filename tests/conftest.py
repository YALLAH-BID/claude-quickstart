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
