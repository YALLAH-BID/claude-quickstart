# Claude Code on a corporate Windows laptop

A step-by-step setup for installing Claude Code on a company-managed
Windows 11 laptop (no admin rights, behind a corporate proxy). Written for
someone who uses Excel, SAP and Outlook daily and codes occasionally.

Sources: the official docs at <https://code.claude.com/docs/en/installation>,
`/authentication`, `/network-config`, `/third-party-integrations` and
`/data-usage`. Anything marked *unverified* could not be confirmed there.

---

## 0. Read this first: where your data goes

Claude Code sends the text you type **and the contents of the files it reads**
to the Claude API so the model can answer. That traffic leaves the laptop.

| Account type | Trained on your data? | Data retention | Stays inside company cloud? |
|---|---|---|---|
| claude.ai Pro / Max (personal) | Only if you opt in | 30 days by default | No |
| Claude for Teams | No | 30 days | No |
| Claude for Enterprise | No | 30 days, zero-retention available | No |
| Amazon Bedrock / Google Vertex AI / Microsoft Foundry | No | Your cloud's own policy | **Yes** |

If the rule is *company data must never leave company systems*, then before
step 1:

1. Ask IT / Information Security which of the above is approved.
2. Until that is confirmed, use Claude Code only on **non-sensitive** material:
   this repository, your own scripts, sample data with no customer, VIN, price
   or plate information.
3. Never paste Autorola, MOI, SAP or appraisal exports into it.

The rest of this guide works the same whichever option IT picks; only step 3
(authentication) changes.

---

## 1. Install

Open **PowerShell** (not Command Prompt). Search the Start menu for
"PowerShell", open it, and run:

```powershell
irm https://claude.ai/install.ps1 | iex
```

- No admin rights needed. It installs into your user profile only.
- No Node.js needed. It is a single native program.
- It updates itself in the background.

If you see `'irm' is not recognized`, you are in Command Prompt. Close it and
open PowerShell instead.

Close PowerShell, open it again, then check:

```powershell
claude --version
claude doctor
```

`claude doctor` prints a health report without starting a session. Green
lines are fine; any red line tells you what to fix.

### Optional but recommended: Git for Windows

Download from <https://git-scm.com/downloads/win> and install with the default
options (user-level install works without admin rights). With it, Claude Code
gets a proper Bash shell. Without it, Claude Code falls back to PowerShell for
shell commands, which still works.

If Git is installed somewhere unusual, tell Claude Code where Bash is in
`%USERPROFILE%\.claude\settings.json`:

```json
{
  "env": {
    "CLAUDE_CODE_GIT_BASH_PATH": "C:\\Program Files\\Git\\bin\\bash.exe"
  }
}
```

### Alternatives to the terminal

- **Desktop app**: <https://claude.com/download> (Windows x64 or ARM64). Same
  engine, with a file browser, diff view and parallel sessions. Good if you
  prefer clicking to typing.
- **VS Code extension**: install "Claude Code" from the VS Code Marketplace.
  It needs the CLI or desktop app installed first.

---

## 2. Corporate proxy and certificate

Most corporate laptops route internet traffic through a proxy that inspects
HTTPS. Two symptoms mean this applies to you:

- `claude` hangs or fails with a network / TLS / certificate error.
- `claude doctor` reports it cannot reach `api.anthropic.com`.

Ask IT for two things: the **proxy address** and the **company root
certificate** as a `.pem` or `.crt` file. Then put both into
`%USERPROFILE%\.claude\settings.json` so they apply every time:

```json
{
  "env": {
    "HTTPS_PROXY": "http://proxy.company.com:8080",
    "HTTP_PROXY": "http://proxy.company.com:8080",
    "NO_PROXY": "localhost,127.0.0.1,.company.com",
    "NODE_EXTRA_CA_CERTS": "C:\\Users\\<you>\\certs\\company-root.pem"
  }
}
```

Notes:

- Use double backslashes in Windows paths inside JSON.
- SOCKS proxies are not supported. Proxies that need NTLM or Kerberos
  sign-in are not supported directly; IT would need to provide an LLM gateway
  instead (*unverified* for your specific proxy vendor).
- If IT maintains a firewall allowlist, these hosts must be open outbound:
  `api.anthropic.com`, `claude.ai`, `platform.claude.com`,
  `downloads.claude.ai`.

To confirm the settings were picked up: run `claude --debug` once and look in
`%USERPROFILE%\.claude\debug\`.

---

## 3. Sign in

Pick the row IT approved in step 0.

### A. claude.ai subscription (Pro, Max, Team, Enterprise)

```powershell
cd C:\path\to\a\project
claude
```

A browser window opens. Sign in. If the browser cannot redirect back (common
on VPN), copy the code it shows and paste it into the terminal. The free
claude.ai plan does **not** include Claude Code.

### B. API key from the Claude Console

Set the key once for your user account (PowerShell):

```powershell
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
```

Restart PowerShell, run `claude`, and approve the key when asked. The key is
a password: do not put it in any file that goes into git.

### C. Company cloud (Bedrock / Vertex / Foundry)

IT gives you the cloud login and region. Add one switch to `settings.json`:

```json
{
  "env": {
    "CLAUDE_CODE_USE_FOUNDRY": "1"
  }
}
```

Replace `FOUNDRY` with `BEDROCK` or `VERTEX` as appropriate, and follow the
provider-specific variables at
<https://code.claude.com/docs/en/third-party-integrations>. Anthropic does not
see the traffic in this mode; your cloud provider's retention rules apply.

Precedence if more than one is configured: cloud provider > `ANTHROPIC_API_KEY`
> claude.ai login.

---

## 4. First session

```powershell
cd C:\path\to\claude-quickstart
claude
```

1. **Trust dialog**: Claude Code asks whether to trust this folder. Say yes
   for folders you own.
2. `/init` writes a `CLAUDE.md` describing the project so future sessions
   start with context. This repository already has one, so skip it here.
3. `/config` opens the settings panel: model, permission mode, auto-update
   channel.
4. **Permission mode** decides how much Claude Code asks before acting:
   - *Manual*: asks before every edit and command. Start here.
   - *Accept edits*: edits files freely, still asks before commands.
   - *Auto*: a safety classifier approves routine actions on its own.
5. Type a request in plain language, for example
   "explain what stock_report/pipeline.py does in simple terms".

Useful commands inside a session:

| Command | What it does |
|---|---|
| `/help` | list all commands |
| `/clear` | start a fresh conversation |
| `/cost` | tokens and cost so far |
| `/doctor` | health check |
| `Esc` | stop the current action |

---

## 5. Where things live on Windows

| Path | Purpose |
|---|---|
| `%USERPROFILE%\.claude\settings.json` | your personal settings, proxy, env vars |
| `%USERPROFILE%\.claude\.credentials.json` | encrypted login token (do not share) |
| `<project>\.claude\settings.json` | project settings, committed to git |
| `<project>\.claude\settings.local.json` | project settings just for you, ignored by git |
| `<project>\CLAUDE.md` | project notes Claude Code reads every session |

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `irm` not recognised | Command Prompt, not PowerShell | open PowerShell |
| Install blocked by policy | app-control software on the laptop | ask IT to allow `claude.exe`, or use the desktop app / WSL |
| Certificate or TLS error | HTTPS-inspecting proxy | set `NODE_EXTRA_CA_CERTS` (step 2) |
| Browser login never returns | VPN or locked-down browser | paste the login code into the terminal |
| "Claude Code not available on this plan" | free claude.ai account | Pro/Max/Team/Enterprise, or an API key |
| Shell commands behave oddly | no Git Bash | install Git for Windows |

If all else fails, WSL 2 (Windows Subsystem for Linux) runs the Linux
installer inside Windows and also gains sandboxing, which the native Windows
build lacks. It needs IT to enable the WSL feature (*unverified* whether
your image allows it).
