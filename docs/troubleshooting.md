# Troubleshooting

Indexed by what you actually saw. Every message below is produced by
[`quickstart.py`](../quickstart.py) itself, so it is greppable against the
source.

## The script exits immediately

### `Request rejected.` … `credit balance is too low`

The most common first failure, and the most misleading. This is a `400`, so it
looks like a malformed request — but the request was fine and **authentication
succeeded**. It is a billing state on the account.

API credits are separate from a Claude subscription: having Claude Code working
says nothing about whether `/v1/messages` calls will be paid for. Add credits at
[console.anthropic.com](https://console.anthropic.com). Nothing in this
repository can work around it.

### `Authentication failed -- try `ant auth login`.`

Credentials were not resolved, or were rejected. Check what is actually active:

```bash
ant auth status
```

Two things catch people out. First, **an unset `ANTHROPIC_API_KEY` does not mean
you have no credentials** — the SDK also reads `ANTHROPIC_AUTH_TOKEN` and an
`ant auth login` profile, and a bare `anthropic.Anthropic()` picks those up.
Second, **profile tokens expire.** `ant auth status` prints the expiry; a
profile that worked this morning can be dead this afternoon. `ant auth login`
refreshes it.

If `ANTHROPIC_API_KEY` *is* set, note it takes precedence over any profile — a
stale exported key shadows a perfectly good login.

### `Rate limited -- retry later.`

A `429`. The SDK already retried twice with backoff before this surfaced, so
retrying immediately by hand will not help. Wait, then rerun.

### `Could not reach the API.`

Network-level: DNS, proxy, or firewall. Nothing to do with credentials.

### `ModuleNotFoundError: No module named 'anthropic'`

The virtual environment is not being used. Invoke its interpreter by path rather
than relying on `python` resolving correctly:

```bash
.venv/Scripts/python.exe quickstart.py   # Linux/macOS: .venv/bin/python
```

If the venv itself is missing or empty, see the setup steps in
[CONTRIBUTING.md](../CONTRIBUTING.md).

## The script runs but the output looks wrong

### `[incomplete: still paused after 5 continuations]`

Claude was still mid-turn after `MAX_CONTINUATIONS` resumes. The answer printed
below the marker is real but partial. Raising `MAX_CONTINUATIONS` lets it run
longer.

Worth knowing: if you hit this, you have reproduced a live paused turn — which
is [the one thing this repository wants observed](pause-turn.md).

### `[truncated: hit the 16000-token cap mid-answer]`

`max_tokens` was reached mid-sentence. Raise `MAX_TOKENS`; Opus 5 permits up to
`128000`, and because the script streams, large values do not risk an HTTP
timeout.

### `[web_search error: max_uses_exceeded]`

A server-side search failed. This arrives as **HTTP 200**, not an exception —
see [api-reference.md](api-reference.md) for why the result block has two
different shapes. The answer that follows was written with fewer sources than
Claude wanted.

### `Model declined (…)`

`stop_reason` was `refusal` — a safety classifier, returned as HTTP 200 rather
than an error. The bracketed category says which. Rephrasing usually resolves
it; the example's default query does not trigger it.

### It just sits there

Expected, within reason. Server-side search runs before anything streams back,
and several searches take a while. The script has no progress output. If it has
been minutes with nothing, suspect the network rather than a hang.

## Nothing here matches

[SUPPORT.md](../SUPPORT.md) covers where to ask, including which questions
belong with Anthropic rather than with this repository.
