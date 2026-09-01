# Tableau Deep Dig — Operator Runbook

## 1. What this is

`tools/tableau_deep_dig.py` inventories an entire Tableau Server site from inside your corporate
network: projects, workbooks, views, data sources, users, groups, subscriptions, schedules,
extract refreshes, flows, and (optionally) connections, permissions, and lineage. Claude runs in a cloud sandbox that cannot reach
intranet hosts, so you run the script (or Claude Code itself) on a machine inside the network, then
bring the outputs — `report.md` and `site_inventory.json` — back to Claude for analysis.

## 2. Option A — run the audit script

**Prerequisites**

- Python 3.8+ on any machine that can open the Tableau URL in a browser (your laptop usually works).
- A Tableau account on the site you want to audit.
- Newer servers (2019.4+): a Personal Access Token — in Tableau, go to
  **My Account Settings → Personal Access Tokens**, create one, and copy the name and secret.
- Older servers: your username and password.

**With a Personal Access Token (preferred):**

```sh
python3 tools/tableau_deep_dig.py \
  --server http://your-tableau-server \
  --site Automotive \
  --pat-name my-token-name \
  --pat-secret my-token-secret
```

**With username/password** (omit `--password` to be prompted securely):

```sh
python3 tools/tableau_deep_dig.py \
  --server http://your-tableau-server \
  --site Automotive \
  --user your.username
```

All values can also come from env vars: `TABLEAU_SERVER`, `TABLEAU_SITE`, `TABLEAU_USER`,
`TABLEAU_PASSWORD`, `TABLEAU_PAT_NAME`, `TABLEAU_PAT_SECRET`. Leaving `--site` empty targets the
Default site.

**Optional deep-dive flags** (each adds calls, so runs take longer):

| Flag | Adds |
|---|---|
| `--connections` | Per-workbook and per-datasource connection details (N+1 calls) |
| `--permissions` | Per-workbook permissions (N+1 calls; usually needs site admin) |
| `--lineage` | Metadata API (GraphQL) upstream databases/tables (Tableau 2019.3+) |

**Other flags:**

| Flag | Meaning |
|---|---|
| `--out DIR` | Output directory (default `tableau_dig_output`) |
| `--timeout SECONDS` | Per-request timeout (default 30) |
| `--page-size N` | Page size for paged endpoints (default 100, max 1000) |
| `--api-version X.Y` | Override the negotiated REST API version |
| `--insecure` | Skip TLS certificate verification (self-signed https) |
| `--use-env-proxy` | Honor `HTTP_PROXY`/`HTTPS_PROXY` (default: bypass all proxies) |

**Output** lands in the `--out` directory:

- `site_inventory.json` — raw data, for deeper follow-up questions
- `report.md` — human-readable report, the main thing to hand to Claude

The output contains metadata only (names, owners, dates, counts — never credentials or extract
data), but review it before sharing outside your team: project and datasource names can themselves
be sensitive. With `--connections`, the JSON additionally includes each connection's database
server address and connection username (never passwords) — those runs deserve extra review.

On Windows, substitute `python` (or `py`) for `python3` in the commands above.

The script exits 0 even when individual sections fail (failures are recorded in the report and
JSON); it exits nonzero only when sign-in itself fails or no auth method was provided.

## 2b. Option A (Windows, zero installs) — PowerShell

If the machine inside the network is a corporate Windows box without Python, use the PowerShell
port `tools/tableau_deep_dig.ps1` instead — it needs **nothing installed**: it runs on the stock
Windows PowerShell 5.1 that ships with every Windows 10/11 machine (and on PowerShell 7), using
only built-in .NET features.

```bat
powershell -ExecutionPolicy Bypass -File tableau_deep_dig.ps1 -Server http://your-tableau-server -Site Automotive -PatName my-token-name -PatSecret my-token-secret
```

Flags mirror the Python version one-for-one (`-User`/`-Password` — omit `-Password` to be
prompted securely — `-Connections`, `-Permissions`, `-Lineage`, `-Out`, `-TimeoutSec`,
`-PageSize`, `-ApiVersion`, `-Insecure`, `-UseEnvProxy`), the same `TABLEAU_*` environment
variables are honored, and it writes the same `site_inventory.json` and `report.md` with the same
exit-code behavior.

## 2c. Option A (locked-down Windows) — browser console

If the machine blocks PowerShell as well (group policy, AppLocker), use
`tools/tableau_deep_dig_browser.js`. It runs in the browser you already sign in to
Tableau with, so there is no program to install or launch — and because it runs on the
Tableau page itself, its REST calls are same-origin and need no proxy or CORS setup.

1. Open any page of the site in the browser, e.g. `http://your-tableau-server/#/site/Automotive/home`.
2. Press **F12**, open the **Console** tab. If it refuses a paste, type `allow pasting`
   and press Enter first (a browser anti-self-XSS guard).
3. Paste the whole script, press Enter.
4. It asks for the Personal Access Token **name**, then its **secret** — credentials are
   never embedded in the pasted text.
5. It downloads `report.md` and `site_inventory.json`, the same outputs as the other two
   versions.

It reads the same endpoints with the same graceful degradation (sections the account
cannot see are recorded as errors and the dig continues), and the site is taken from the
`/site/<name>/` segment of the page URL.

## 2d. Option A (deeper dig) — browser console v2

`tools/tableau_deep_dig_browser_v2.js` is a second-pass, still read-only browser script
that answers "what feeds all this?" It runs the same way (paste into the console, enter the
token name and secret) and writes `report_v2.md` and `site_inventory_v2.json`. On top of the
base inventory it adds, for each item the account may read:

- **Connections** — per workbook and data source: upstream server address, connection type
  and connection user (never passwords), plus a flag for connections that embed credentials.
- **Revision history** — who published each version of each workbook and when.
- **Lineage** — a read-only Metadata API (GraphQL) query mapping workbooks to their upstream
  databases and tables (needs the Metadata API enabled; degrades to an error line if not).
- **Workbook internals** — downloads each workbook definition and parses it in the browser
  for custom SQL, calculated-field formulas, embedded connections and sheet/dashboard counts.
  The `.twbx`/`.twb` unzip and XML parse happen entirely client-side; anything the account
  cannot download is recorded as a 403 and skipped.

Everything is same-origin and read-only, credentials/token never reach the output files, and
the GraphQL call is a query (no mutation). Sections needing site-admin rights (users, groups,
schedules, subscriptions) still record as 403 under a Viewer token and the run continues.

## 3. Option B — run Claude Code inside the network

For a live, interactive dig, install Claude Code on a machine that can reach the server — use the
native installer, or:

```sh
npm i -g @anthropic-ai/claude-code
```

Then start it in any folder and paste the server URL plus what you want to know:

```sh
claude
# > Audit http://your-tableau-server site Automotive — list workbooks not
#   viewed in 90 days and extracts that failed their last refresh.
```

Claude can then call the Tableau REST API directly with your credentials and iterate on follow-up
questions in real time.

## 4. Feeding results back to Claude

Start a Claude session and attach or paste `report.md` (add `site_inventory.json` when you want
deeper, data-level answers). Then ask for the analysis you need, e.g.:

- "Which workbooks look unused or abandoned? Rank by last-viewed and owner status."
- "Find stale extracts and refresh schedules that overlap or fail."
- "Where are the ownership gaps — content owned by deactivated or departed users?"
- "Suggest consolidation candidates: near-duplicate workbooks and datasources."

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| **401 on sign-in** | Wrong credentials or wrong site contentUrl. The site name in your browser URL after `/site/` is the contentUrl — e.g. `http://your-tableau-server/#/site/Automotive/home` → `--site Automotive`. |
| **403 on some sections** | You are not a site admin. The dig continues and records the skipped sections in the report — no action needed unless you need those sections. |
| **Connection errors / timeouts** | Usually a corporate proxy intercepting the request. The script bypasses proxies by default; add `--use-env-proxy` only if your network requires the proxy even for intranet hosts. |
| **TLS errors on https** | Self-signed or internal CA certificate. Add `--insecure`. |
| **Very old server** | The script falls back to REST API 2.3 and skips newer features automatically. Note: PATs need Tableau 2019.4+ (REST API 3.6) — use `--user`/`--password` on anything older. |
