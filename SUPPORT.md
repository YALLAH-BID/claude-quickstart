# Support

One person maintains this in spare time. There is no SLA — worth knowing before
you depend on a reply.

## Where to ask

**Something in this example is wrong or doesn't work**
Open a [bug report](https://github.com/YALLAH-BID/claude-quickstart/issues/new?template=bug_report.yml).
The form asks for the output and both version numbers; including them up front
usually saves a round trip.

**You triggered a real `pause_turn`**
There is a [dedicated form](https://github.com/YALLAH-BID/claude-quickstart/issues/new?template=pause_turn_observation.yml)
for it. This is the one open question in the repo, and reporting what you saw is
the most useful thing anyone can do here.

**The `anthropic` SDK itself misbehaves**
That belongs upstream, at
[anthropic-sdk-python](https://github.com/anthropics/anthropic-sdk-python/issues).
Nothing in this repo can fix an SDK bug.

**A question about the Claude API** — how a parameter behaves, what a model
supports, what something costs
The [documentation](https://platform.claude.com/docs/en/home) first, then
[Anthropic support](https://support.claude.com/en/). Please don't open an issue
here for these: this repo has no knowledge of the API beyond what the docs say,
and an answer given here would just be a worse copy of one.

**Account, billing, or credits**
[Anthropic support](https://support.claude.com/en/). This includes
`Your credit balance is too low to access the Anthropic API` — that is a billing
state on your account, not a fault in the example. The script is working
correctly when it reports it.

## What this repo cannot tell you

CI checks that the code parses, formats, and imports. It never runs
`quickstart.py`. So if your question is "does this example actually work end to
end", the honest answer is that its runtime path is unverified — see the
Unverified section of [CHANGELOG.md](CHANGELOG.md).
