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
