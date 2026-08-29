# Security Policy

## Scope

This is a single-file example, not a library or a deployed service. Nothing here
runs on anyone's infrastructure and there is no release process, so the realistic
security surface is narrow: what the example *teaches*, and what it does with your
API key.

Vulnerabilities in the `anthropic` SDK itself belong upstream, not here — report
those to [anthropic-sdk-python](https://github.com/anthropics/anthropic-sdk-python/security).

## Supported versions

`main` is always supported, as is the most recent tagged release once one exists
— there are none yet. There are no backports: a fix lands on `main` and ships in
the next release.

## Reporting

Use GitHub's private vulnerability reporting — the **Report a vulnerability**
button under the repository's Security tab. That keeps the report out of public
view until it is resolved.

Please don't open a public issue for anything that would expose a credential or a
working exploit.

## API keys

The one genuinely sensitive thing this example touches is your Anthropic API key.

`quickstart.py` never reads a key from source. It constructs a bare
`anthropic.Anthropic()`, which resolves credentials from the environment or from an
`ant auth login` profile — so a key never has to be pasted into a file that could be
committed.

`.env` is gitignored. No credential belongs in a tracked file.

**If you do leak a key, rotate it** at [console.anthropic.com](https://console.anthropic.com).
Removing the commit is not sufficient: the value survives in the reflog, in any fork
or clone, and in whatever scraped it before you noticed.
