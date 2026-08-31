#!/usr/bin/env python3
"""Deep-dig audit of a single Tableau Server site.

Runs from inside a corporate network against an internal Tableau Server
(often plain http) and inventories one site: projects (as a tree), workbooks,
views with usage statistics, data sources, users, groups, subscriptions,
schedules, extract refresh tasks, flows, and — optionally — per-item
connections, per-workbook permissions, and Metadata-API lineage.

Outputs (written to --out, default ``tableau_dig_output``):
  - ``site_inventory.json``  everything collected, raw
  - ``report.md``            human-readable report

Uses only the Python standard library; runs on Python 3.8+.

Examples:
  python3 tools/tableau_deep_dig.py --server http://tableau.example.com \
      --site Automotive --pat-name my-token --pat-secret abc123
  python3 tools/tableau_deep_dig.py --server http://tableau.example.com \
      --user jane.doe   # prompts for the password

Exit code is 0 even when individual sections fail (failures are recorded in
the report and JSON); nonzero only when sign-in itself fails or no auth
method was provided.
"""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone

TOOL_NAME = "tableau_deep_dig"
NEGOTIATION_API_VERSION = "2.4"  # /serverinfo exists from API 2.4 (Tableau 10.1)
FALLBACK_API_VERSION = "2.3"  # safe floor for pre-10.1 servers
MAX_PAGES = 10000  # hard stop for paging loops
STALE_DAYS = 180
ACTIVE_DAYS = 90
TOP_VIEWS = 25
TABLE_CAP = 500  # report tables are capped; the JSON always has everything

LINEAGE_QUERY = """\
query tableauDeepDigLineage {
  workbooks {
    name
    projectName
    upstreamDatasources {
      name
    }
    upstreamTables {
      name
      schema
      database {
        name
        connectionType
      }
    }
  }
}
"""

_ISO_FALLBACK_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")


class TableauError(Exception):
    """An HTTP or REST-level failure while talking to Tableau Server."""


class AuthError(TableauError):
    """Sign-in failed."""


def _log(message: str) -> None:
    """Progress and diagnostics go to stderr; stdout stays clean."""
    print(message, file=sys.stderr, flush=True)


def parse_iso(value) -> datetime | None:
    """Parse an ISO-8601 timestamp defensively.

    Returns an aware UTC datetime, or None for missing/unparseable input.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        match = _ISO_FALLBACK_RE.match(text)
        if not match:
            return None
        try:
            parsed = datetime(*(int(g) for g in match.groups()), tzinfo=timezone.utc)
        except ValueError:  # out-of-range components, e.g. "0000-00-00"
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _version_tuple(version) -> tuple:
    parts = []
    for chunk in str(version or "").split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts or [0])


def parse_xml(data) -> ET.Element:
    """Parse bytes into an Element with XML namespaces stripped from tags."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        snippet = data[:120] if isinstance(data, bytes) else str(data)[:120].encode()
        shown = snippet.decode("utf-8", "replace")
        raise TableauError(
            f"unparseable XML response: {exc} (response starts: {shown!r})"
        ) from exc
    for element in root.iter():
        if "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    return root


def _format_ts_error(error_el: ET.Element) -> str:
    code = error_el.get("code", "?")
    summary = (error_el.findtext("summary") or "").strip()
    detail = (error_el.findtext("detail") or "").strip()
    message = f"Tableau error code {code}"
    if summary:
        message += f": {summary}"
    if detail:
        message += f" — {detail}"
    return message


def _extract_ts_error(body) -> str | None:
    try:
        root = parse_xml(body)
    except TableauError:
        return None
    error_el = root.find("error")
    if error_el is None:
        return None
    return _format_ts_error(error_el)


def _describe_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read()
    except OSError:
        body = b""
    message = _extract_ts_error(body)
    if message:
        return message
    text = body[:200].decode("utf-8", "replace").strip()
    return text or str(exc.reason)


def item_to_dict(element: ET.Element) -> dict:
    """Flatten one list item: all attributes plus well-known children.

    Children like ``project``, ``owner``, ``usage`` become attribute dicts
    (with one extra level for grandchildren, e.g. task/extractRefresh/schedule);
    ``tags`` becomes a list of tag labels.
    """
    record = dict(element.attrib)
    for child in element:
        tag = child.tag
        if tag == "tags":
            record["tags"] = [t.get("label", "") for t in child if t.tag == "tag"]
            continue
        value = dict(child.attrib)
        for sub in child:
            if sub.tag == "tags":
                value["tags"] = [t.get("label", "") for t in sub if t.tag == "tag"]
            elif sub.attrib and sub.tag not in value:
                value[sub.tag] = dict(sub.attrib)
            elif sub.text and sub.text.strip() and sub.tag not in value:
                value[sub.tag] = sub.text.strip()
        if not value and child.text and child.text.strip():
            record[tag] = child.text.strip()
        else:
            record[tag] = value
    return record


class TableauClient:
    """Minimal Tableau REST API client built on urllib."""

    def __init__(
        self,
        server: str,
        timeout: float = 30.0,
        insecure: bool = False,
        use_env_proxy: bool = False,
    ):
        self.server = server.rstrip("/")
        self.timeout = timeout
        self.api_version = FALLBACK_API_VERSION
        self.token = None
        self.site_id = None
        self.site_content_url = ""
        self.user_id = None
        self.server_info = {}
        handlers = []
        if not use_env_proxy:
            # Corporate proxies usually cannot reach intranet hosts, so the
            # default is to ignore HTTP_PROXY/HTTPS_PROXY entirely.
            handlers.append(urllib.request.ProxyHandler({}))
        if insecure:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    def version_tuple(self) -> tuple:
        return _version_tuple(self.api_version)

    def _request(self, method: str, url: str, body=None, headers=None) -> bytes:
        """One HTTP request; idempotent GETs are retried once on URLError."""
        attempts = 2 if method == "GET" and body is None else 1
        last_error = None
        for attempt in range(attempts):
            request = urllib.request.Request(url, data=body, method=method)
            if self.token:
                request.add_header("X-Tableau-Auth", self.token)
            for key, val in (headers or {}).items():
                request.add_header(key, val)
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                detail = _describe_http_error(exc)
                raise TableauError(
                    f"HTTP {exc.code} for {method} {url}: {detail}"
                ) from exc
            # URLError, timeouts, connection resets, and malformed responses
            # (BadStatusLine, IncompleteRead) — HTTPException is not OSError.
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    _log(f"    retrying {method} {url} after: {exc}")
        raise TableauError(
            f"{method} {url} failed after {attempts} attempt(s): {last_error}"
        ) from last_error

    def get_xml(self, path: str, params=None) -> ET.Element:
        """GET ``/api/{version}/{path}`` and return the parsed tsResponse."""
        url = f"{self.server}/api/{self.api_version}/{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        root = parse_xml(self._request("GET", url))
        error_el = root.find("error")
        if error_el is not None:
            raise TableauError(_format_ts_error(error_el))
        return root

    def get_paged(self, path: str, item_tag: str, params=None, page_size=100):
        """Collect every page of a paged listing endpoint.

        Guards against totalAvailable=0 and against servers that ignore
        paging (stops when a page repeats or comes back empty).
        """
        items = []
        previous_ids = None
        page_number = 1
        while page_number <= MAX_PAGES:
            query = {"pageSize": page_size, "pageNumber": page_number}
            if params:
                query.update(params)
            root = self.get_xml(path, query)
            page_items = [item_to_dict(el) for el in root.iter(item_tag)]
            total = 0
            pagination = root.find("pagination")
            if pagination is not None:
                try:
                    total = int(pagination.get("totalAvailable", "0"))
                except (TypeError, ValueError):
                    total = 0
            if not page_items:
                break
            ids = [entry.get("id") for entry in page_items]
            if previous_ids is not None and ids == previous_ids:
                break  # server ignored pageNumber; avoid double-counting
            previous_ids = ids
            items.extend(page_items)
            if total <= 0 or len(items) >= total:
                break
            page_number += 1
        return items

    def negotiate(self, override=None) -> dict:
        """Discover product/REST versions via /serverinfo; degrade gracefully."""
        info = {}
        try:
            url = f"{self.server}/api/{NEGOTIATION_API_VERSION}/serverinfo"
            root = parse_xml(self._request("GET", url))
            error_el = root.find("error")
            if error_el is not None:
                raise TableauError(_format_ts_error(error_el))
            server_info = root.find("serverInfo")
            if server_info is not None:
                product = server_info.find("productVersion")
                rest = server_info.find("restApiVersion")
                if product is not None:
                    info["product_version"] = (product.text or "").strip()
                    if product.get("build"):
                        info["build"] = product.get("build")
                if rest is not None and (rest.text or "").strip():
                    info["rest_api_version"] = rest.text.strip()
        except TableauError as exc:
            info["negotiation_error"] = str(exc)
        if override:
            self.api_version = str(override)
        elif info.get("rest_api_version"):
            self.api_version = info["rest_api_version"]
        else:
            self.api_version = FALLBACK_API_VERSION
        info["negotiated_api_version"] = self.api_version
        self.server_info = info
        return info

    def sign_in(
        self,
        site_content_url: str,
        user=None,
        password=None,
        pat_name=None,
        pat_secret=None,
    ) -> None:
        credentials = ET.Element("credentials")
        if pat_name and pat_secret:
            credentials.set("personalAccessTokenName", pat_name)
            credentials.set("personalAccessTokenSecret", pat_secret)
        else:
            credentials.set("name", user or "")
            credentials.set("password", password or "")
        ET.SubElement(credentials, "site", {"contentUrl": site_content_url or ""})
        envelope = ET.Element("tsRequest")
        envelope.append(credentials)
        body = ET.tostring(envelope)
        url = f"{self.server}/api/{self.api_version}/auth/signin"
        try:
            data = self._request(
                "POST", url, body=body, headers={"Content-Type": "application/xml"}
            )
        except TableauError as exc:
            raise AuthError(str(exc)) from exc
        root = parse_xml(data)
        error_el = root.find("error")
        if error_el is not None:
            raise AuthError(_format_ts_error(error_el))
        creds = root.find("credentials")
        if creds is None or not creds.get("token"):
            raise AuthError("sign-in response did not include an auth token")
        self.token = creds.get("token")
        site_el = creds.find("site")
        if site_el is not None:
            self.site_id = site_el.get("id")
            self.site_content_url = site_el.get("contentUrl", "")
        user_el = creds.find("user")
        if user_el is not None:
            self.user_id = user_el.get("id")
        if not self.site_id:
            raise AuthError("sign-in response did not include a site id")

    def sign_out(self) -> None:
        """Best-effort sign-out; never raises."""
        if not self.token:
            return
        url = f"{self.server}/api/{self.api_version}/auth/signout"
        try:
            self._request("POST", url, body=b"")
        except TableauError:
            pass
        self.token = None

    def graphql(self, query: str) -> dict:
        """POST a query to the Metadata API (Tableau 2019.3+)."""
        url = f"{self.server}/api/metadata/graphql"
        body = json.dumps({"query": query}).encode()
        data = self._request(
            "POST",
            url,
            body=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            return json.loads(data.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise TableauError(
                f"Metadata API returned a non-JSON response: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _fetch_item_connections(client, items, collection, section, record_error):
    """N+1 fetch of /…/{id}/connections for workbooks or datasources."""
    if items is None:
        record_error(section, f"skipped: {collection} list unavailable")
        return None
    _log(f"[.] fetching {section} for {len(items)} item(s) ...")
    results = {}
    failures = []
    for item in items:
        item_id = item.get("id")
        if not item_id:
            continue
        path = f"sites/{client.site_id}/{collection}/{item_id}/connections"
        try:
            root = client.get_xml(path)
        except TableauError as exc:
            failures.append(f"{item.get('name', item_id)}: {exc}")
            continue
        results[item_id] = [item_to_dict(el) for el in root.iter("connection")]
    if failures:
        preview = "; ".join(failures[:3])
        record_error(
            section,
            f"{len(failures)} of {len(items)} item(s) failed (e.g. {preview})",
        )
    _log(f"[+] {section}: connections for {len(results)} item(s)")
    return results


def _fetch_workbook_permissions(client, workbooks, record_error):
    """N+1 fetch of per-workbook permissions (usually needs admin)."""
    section = "workbook_permissions"
    if workbooks is None:
        record_error(section, "skipped: workbook list unavailable")
        return None
    _log(f"[.] fetching {section} for {len(workbooks)} workbook(s) ...")
    results = {}
    failures = []
    for workbook in workbooks:
        workbook_id = workbook.get("id")
        if not workbook_id:
            continue
        path = f"sites/{client.site_id}/workbooks/{workbook_id}/permissions"
        try:
            root = client.get_xml(path)
        except TableauError as exc:
            failures.append(f"{workbook.get('name', workbook_id)}: {exc}")
            continue
        grants = []
        for grantee_el in root.iter("granteeCapabilities"):
            entry = {}
            group_el = grantee_el.find("group")
            user_el = grantee_el.find("user")
            if group_el is not None:
                entry["grantee"] = {"type": "group", **group_el.attrib}
            elif user_el is not None:
                entry["grantee"] = {"type": "user", **user_el.attrib}
            entry["capabilities"] = [
                dict(cap.attrib) for cap in grantee_el.iter("capability")
            ]
            grants.append(entry)
        results[workbook_id] = grants
    if failures:
        preview = "; ".join(failures[:3])
        record_error(
            section,
            f"{len(failures)} of {len(workbooks)} workbook(s) failed (e.g. {preview})",
        )
    _log(f"[+] {section}: permissions for {len(results)} workbook(s)")
    return results


def collect_inventory(client, args):
    """Fetch every section; each is individually wrapped so one failure
    (403/404/old version) records an error entry and the dig continues."""
    sections = {}
    errors = []

    def record_error(section, message):
        errors.append({"section": section, "error": str(message)})
        _log(f"[!] {section}: {message}")

    def run(section, func, min_version=None):
        if min_version and client.version_tuple() < min_version:
            needed = ".".join(str(part) for part in min_version)
            record_error(
                section,
                f"skipped: requires REST API {needed}+ "
                f"(negotiated {client.api_version})",
            )
            sections[section] = None
            return None
        _log(f"[.] fetching {section} ...")
        try:
            result = func()
        except Exception as exc:
            record_error(section, exc)
            sections[section] = None
            return None
        sections[section] = result
        count = len(result) if hasattr(result, "__len__") else "?"
        _log(f"[+] {section}: {count} item(s)")
        return result

    site = f"sites/{client.site_id}"
    size = args.page_size

    workbooks = None
    datasources = None

    run(
        "projects",
        lambda: client.get_paged(f"{site}/projects", "project", page_size=size),
    )
    workbooks = run(
        "workbooks",
        lambda: client.get_paged(f"{site}/workbooks", "workbook", page_size=size),
    )
    run(
        "views",
        lambda: client.get_paged(
            f"{site}/views",
            "view",
            params={"includeUsageStatistics": "true"},
            page_size=size,
        ),
    )
    datasources = run(
        "datasources",
        lambda: client.get_paged(f"{site}/datasources", "datasource", page_size=size),
    )
    run("users", lambda: client.get_paged(f"{site}/users", "user", page_size=size))
    run("groups", lambda: client.get_paged(f"{site}/groups", "group", page_size=size))
    run(
        "subscriptions",
        lambda: client.get_paged(
            f"{site}/subscriptions", "subscription", page_size=size
        ),
    )
    # Server-level; needs server admin — expect 403 for most analysts.
    run(
        "schedules",
        lambda: client.get_paged("schedules", "schedule", page_size=size),
    )
    # Site-level list of extract refresh tasks; admin only — expect 403.
    run(
        "extract_refresh_tasks",
        lambda: client.get_paged(
            f"{site}/tasks/extractRefreshes", "task", page_size=size
        ),
        min_version=(2, 6),
    )
    run(
        "flows",
        lambda: client.get_paged(f"{site}/flows", "flow", page_size=size),
        min_version=(3, 3),
    )

    if args.connections:
        sections["workbook_connections"] = _fetch_item_connections(
            client, workbooks, "workbooks", "workbook_connections", record_error
        )
        sections["datasource_connections"] = _fetch_item_connections(
            client,
            datasources,
            "datasources",
            "datasource_connections",
            record_error,
        )

    if args.permissions:
        sections["workbook_permissions"] = _fetch_workbook_permissions(
            client, workbooks, record_error
        )

    if args.lineage:
        _log("[.] querying the Metadata API for lineage ...")
        try:
            payload = client.graphql(LINEAGE_QUERY)
        except TableauError as exc:
            record_error("lineage", f"Metadata API unavailable: {exc}")
            sections["lineage"] = None
        else:
            if payload.get("errors"):
                shown = json.dumps(payload["errors"])[:300]
                record_error("lineage", f"GraphQL errors: {shown}")
            sections["lineage"] = payload.get("data")
            nodes = (payload.get("data") or {}).get("workbooks") or []
            _log(f"[+] lineage: {len(nodes)} workbook node(s)")

    return sections, errors


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _md(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_md(cell) for cell in row) + " |")
    return lines


def _fmt_date(value) -> str:
    parsed = parse_iso(value)
    if parsed:
        return parsed.strftime("%Y-%m-%d")
    return str(value) if value else ""


def _sort_key_date(value) -> datetime:
    return parse_iso(value) or datetime(1970, 1, 1, tzinfo=timezone.utc)


def _child_id(record, child) -> str:
    value = record.get(child)
    if isinstance(value, dict):
        return value.get("id") or ""
    return ""


def _child_name(record, child, id_to_name) -> str:
    value = record.get(child)
    if isinstance(value, dict):
        return (
            value.get("name")
            or id_to_name.get(value.get("id") or "", "")
            or (value.get("id") or "")
        )
    return ""


def _usage_count(view) -> int:
    usage = view.get("usage")
    if isinstance(usage, dict):
        try:
            return int(usage.get("totalViewCount") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _project_tree_lines(projects, workbooks, datasources):
    workbook_counts = Counter(_child_id(wb, "project") for wb in workbooks or [])
    datasource_counts = Counter(_child_id(ds, "project") for ds in datasources or [])
    by_id = {p.get("id"): p for p in projects or [] if p.get("id")}
    children = {}
    roots = []
    for project in projects or []:
        parent_id = project.get("parentProjectId")
        if parent_id and parent_id in by_id:
            children.setdefault(parent_id, []).append(project)
        else:
            roots.append(project)
    lines = []
    seen = set()

    def walk(node, depth):
        node_id = node.get("id")
        if node_id in seen:
            return  # cycle guard
        seen.add(node_id)
        indent = "  " * depth
        name = _md(node.get("name") or "(unnamed project)")
        wb_count = workbook_counts.get(node_id, 0)
        ds_count = datasource_counts.get(node_id, 0)
        lines.append(
            f"{indent}- **{name}** (workbooks: {wb_count}, datasources: {ds_count})"
        )
        kids = children.get(node_id, [])
        for child in sorted(kids, key=lambda c: (c.get("name") or "").lower()):
            walk(child, depth + 1)

    for root in sorted(roots, key=lambda c: (c.get("name") or "").lower()):
        walk(root, 0)
    return lines


def build_report(inventory) -> str:
    sections = inventory["sections"]
    errors = inventory["errors"]
    options = inventory["options"]
    server = inventory["server"]
    site = inventory["site"]
    now = parse_iso(inventory["generated_at"]) or datetime.now(timezone.utc)

    projects = sections.get("projects")
    workbooks = sections.get("workbooks")
    views = sections.get("views")
    datasources = sections.get("datasources")
    users = sections.get("users")
    groups = sections.get("groups")
    subscriptions = sections.get("subscriptions")
    schedules = sections.get("schedules")
    refresh_tasks = sections.get("extract_refresh_tasks")
    flows = sections.get("flows")

    user_names = {
        u.get("id"): (u.get("name") or u.get("fullName") or u.get("id") or "")
        for u in users or []
    }
    project_names = {p.get("id"): p.get("name") or "" for p in projects or []}
    workbook_names = {w.get("id"): w.get("name") or "" for w in workbooks or []}
    datasource_names = {d.get("id"): d.get("name") or "" for d in datasources or []}

    lines = []
    out = lines.append

    site_label = site.get("content_url") or "(Default)"
    out("# Tableau Site Deep-Dig Report")
    out("")
    out(
        f"Site **{_md(site_label)}** on `{server.get('url', '')}` — generated "
        f"{now.strftime('%Y-%m-%d %H:%M UTC')} by `{TOOL_NAME}`."
    )
    out("")

    # -- Overview ----------------------------------------------------------
    out("## Overview")
    out("")
    product = server.get("product_version") or "unknown"
    build = server.get("build")
    product_label = f"{product} (build {build})" if build else product
    out(f"- Server product version: {_md(product_label)}")
    out(f"- REST API version used: {server.get('negotiated_api_version', '?')}")
    if server.get("negotiation_error"):
        out(
            "- Note: /serverinfo was unavailable (old server?); "
            f"fell back to REST API {FALLBACK_API_VERSION}"
        )
    out(f"- Site contentUrl: {_md(site_label)} (id `{site.get('id', '')}`)")
    out("")
    count_rows = []
    for key, label in [
        ("projects", "Projects"),
        ("workbooks", "Workbooks"),
        ("views", "Views"),
        ("datasources", "Data sources"),
        ("users", "Users"),
        ("groups", "Groups"),
        ("subscriptions", "Subscriptions"),
        ("schedules", "Schedules (server-wide)"),
        ("extract_refresh_tasks", "Extract refresh tasks"),
        ("flows", "Flows"),
    ]:
        data = sections.get(key)
        count = str(len(data)) if isinstance(data, list) else "unavailable"
        count_rows.append([label, count])
    for key, label, enabled in [
        ("workbook_connections", "Workbooks w/ connection details", "connections"),
        ("datasource_connections", "Datasources w/ connection details", "connections"),
        ("workbook_permissions", "Workbooks w/ permission details", "permissions"),
    ]:
        if options.get(enabled):
            data = sections.get(key)
            count = str(len(data)) if isinstance(data, dict) else "unavailable"
            count_rows.append([label, count])
    lines.extend(_table(["Content type", "Count"], count_rows))
    out("")

    # -- Project tree ------------------------------------------------------
    out("## Project tree")
    out("")
    if projects is None:
        out("_Not available — see the Errors section._")
    elif not projects:
        out("_No projects returned._")
    else:
        lines.extend(_project_tree_lines(projects, workbooks, datasources))
    out("")

    # -- Workbooks ---------------------------------------------------------
    out("## Workbooks")
    out("")
    if workbooks is None:
        out("_Not available — see the Errors section._")
    elif not workbooks:
        out("_No workbooks returned._")
    else:
        ordered = sorted(
            workbooks,
            key=lambda w: _sort_key_date(w.get("updatedAt")),
            reverse=True,
        )
        if len(ordered) > TABLE_CAP:
            out(
                f"Showing the {TABLE_CAP} most recently updated of "
                f"{len(ordered)} workbooks (full list in site_inventory.json)."
            )
            out("")
        rows = [
            [
                wb.get("name") or wb.get("id") or "",
                _child_name(wb, "project", project_names),
                _child_name(wb, "owner", user_names),
                wb.get("size") or "",
                _fmt_date(wb.get("createdAt")),
                _fmt_date(wb.get("updatedAt")),
                ", ".join(wb.get("tags") or []),
            ]
            for wb in ordered[:TABLE_CAP]
        ]
        lines.extend(
            _table(
                [
                    "Workbook",
                    "Project",
                    "Owner",
                    "Size (MB)",
                    "Created",
                    "Updated",
                    "Tags",
                ],
                rows,
            )
        )
    out("")

    # -- Views by usage ----------------------------------------------------
    out(f"## Top {TOP_VIEWS} views by usage")
    out("")
    if views is None:
        out("_Not available — see the Errors section._")
    elif not views:
        out("_No views returned._")
    else:
        have_usage = any(isinstance(v.get("usage"), dict) for v in views)
        if not have_usage:
            out(
                "_This server did not return usage statistics "
                "(includeUsageStatistics unsupported?)._"
            )
            out("")
        top = sorted(views, key=_usage_count, reverse=True)[:TOP_VIEWS]
        rows = [
            [
                v.get("name") or v.get("id") or "",
                _child_name(v, "workbook", workbook_names),
                str(_usage_count(v)),
            ]
            for v in top
        ]
        lines.extend(_table(["View", "Workbook", "Total views"], rows))
        zero = sum(1 for v in views if _usage_count(v) == 0)
        out("")
        out(f"**{zero}** of {len(views)} views have zero recorded usage.")
    out("")

    # -- Data sources ------------------------------------------------------
    out("## Data sources")
    out("")
    if datasources is None:
        out("_Not available — see the Errors section._")
    elif not datasources:
        out("_No data sources returned._")
    else:
        ordered = sorted(
            datasources,
            key=lambda d: _sort_key_date(d.get("updatedAt")),
            reverse=True,
        )
        if len(ordered) > TABLE_CAP:
            out(
                f"Showing the {TABLE_CAP} most recently updated of "
                f"{len(ordered)} data sources (full list in "
                "site_inventory.json)."
            )
            out("")
        rows = [
            [
                ds.get("name") or ds.get("id") or "",
                ds.get("type") or "",
                "yes" if str(ds.get("isCertified")).lower() == "true" else "no",
                _child_name(ds, "owner", user_names),
                _child_name(ds, "project", project_names),
                _fmt_date(ds.get("updatedAt")),
            ]
            for ds in ordered[:TABLE_CAP]
        ]
        lines.extend(
            _table(
                ["Data source", "Type", "Certified", "Owner", "Project", "Updated"],
                rows,
            )
        )
    out("")

    # -- Users -------------------------------------------------------------
    out("## Users")
    out("")
    if users is None:
        out("_Not available — see the Errors section._")
    elif not users:
        out("_No users returned._")
    else:
        role_counts = Counter(u.get("siteRole") or "(unknown)" for u in users)
        rows = [
            [role, str(count)]
            for role, count in sorted(role_counts.items(), key=lambda kv: -kv[1])
        ]
        lines.extend(_table(["Site role", "Users"], rows))
        cutoff = now - timedelta(days=ACTIVE_DAYS)
        active = 0
        never = 0
        for user in users:
            last_login = parse_iso(user.get("lastLogin"))
            if last_login is None:
                never += 1
            elif last_login >= cutoff:
                active += 1
        out("")
        out(
            f"**{active}** of {len(users)} users signed in within the last "
            f"{ACTIVE_DAYS} days; {never} have no recorded last login."
        )
    out("")

    # -- Groups ------------------------------------------------------------
    out("## Groups")
    out("")
    if groups is None:
        out("_Not available — see the Errors section._")
    elif not groups:
        out("_No groups returned._")
    else:
        out(f"{len(groups)} group(s):")
        out("")
        for group in sorted(groups, key=lambda g: (g.get("name") or "").lower())[
            :TABLE_CAP
        ]:
            domain = group.get("domain")
            domain_name = domain.get("name") if isinstance(domain, dict) else ""
            suffix = f" (domain: {_md(domain_name)})" if domain_name else ""
            out(f"- {_md(group.get('name') or group.get('id'))}{suffix}")
        if len(groups) > TABLE_CAP:
            out(f"- … and {len(groups) - TABLE_CAP} more")
    out("")

    # -- Schedules / refresh tasks / subscriptions -------------------------
    out("## Schedules, extract refreshes and subscriptions")
    out("")
    out("### Schedules (server-wide)")
    out("")
    if schedules is None:
        out("_Not available (server-admin only) — see the Errors section._")
    elif not schedules:
        out("_No schedules returned._")
    else:
        rows = [
            [
                s.get("name") or s.get("id") or "",
                s.get("type") or "",
                s.get("frequency") or "",
                s.get("state") or "",
                _fmt_date(s.get("nextRunAt")),
            ]
            for s in schedules[:TABLE_CAP]
        ]
        lines.extend(
            _table(["Schedule", "Type", "Frequency", "State", "Next run"], rows)
        )
    out("")
    out("### Extract refresh tasks")
    out("")
    if refresh_tasks is None:
        out("_Not available (admin only or old server) — see the Errors section._")
    elif not refresh_tasks:
        out("_No extract refresh tasks returned._")
    else:
        rows = []
        for task in refresh_tasks[:TABLE_CAP]:
            refresh = task.get("extractRefresh")
            refresh = refresh if isinstance(refresh, dict) else {}
            target = ""
            wb = refresh.get("workbook")
            ds = refresh.get("datasource")
            if isinstance(wb, dict):
                name = workbook_names.get(wb.get("id") or "", wb.get("id") or "")
                target = f"workbook: {name}"
            elif isinstance(ds, dict):
                name = datasource_names.get(ds.get("id") or "", ds.get("id") or "")
                target = f"datasource: {name}"
            schedule = refresh.get("schedule")
            schedule_name = schedule.get("name") if isinstance(schedule, dict) else ""
            rows.append([refresh.get("type") or "", target, schedule_name or ""])
        lines.extend(_table(["Refresh type", "Target", "Schedule"], rows))
    out("")
    out("### Subscriptions")
    out("")
    if subscriptions is None:
        out("_Not available — see the Errors section._")
    elif not subscriptions:
        out("_No subscriptions returned._")
    else:
        rows = []
        for sub in subscriptions[:TABLE_CAP]:
            content = sub.get("content")
            content_type = content.get("type") if isinstance(content, dict) else ""
            rows.append(
                [
                    sub.get("subject") or "",
                    content_type or "",
                    _child_name(sub, "user", user_names),
                    _child_name(sub, "schedule", {}),
                ]
            )
        lines.extend(
            _table(["Subject", "Content type", "Subscriber", "Schedule"], rows)
        )
    out("")
    if flows is not None:
        out(f"### Flows: {len(flows)} on this site")
        out("")

    # -- Stale content -----------------------------------------------------
    out(f"## Stale content (not updated in {STALE_DAYS}+ days)")
    out("")
    if workbooks is None:
        out("_Not available — see the Errors section._")
    else:
        stale_cutoff = now - timedelta(days=STALE_DAYS)
        stale = []
        for wb in workbooks:
            updated = parse_iso(wb.get("updatedAt"))
            if updated and updated <= stale_cutoff:
                stale.append((updated, wb))
        stale.sort(key=lambda pair: pair[0])
        if not stale:
            out(f"No workbooks are older than {STALE_DAYS} days (by updatedAt).")
        else:
            out(
                f"**{len(stale)}** of {len(workbooks)} workbooks were last "
                f"updated {STALE_DAYS}+ days ago"
                + (
                    f" (oldest {min(TABLE_CAP, len(stale))} shown):"
                    if len(stale) > TABLE_CAP
                    else ":"
                )
            )
            out("")
            rows = [
                [
                    wb.get("name") or wb.get("id") or "",
                    _child_name(wb, "project", project_names),
                    _child_name(wb, "owner", user_names),
                    _fmt_date(wb.get("updatedAt")),
                ]
                for _, wb in stale[:TABLE_CAP]
            ]
            lines.extend(_table(["Workbook", "Project", "Owner", "Last updated"], rows))
    out("")

    # -- Lineage -----------------------------------------------------------
    if options.get("lineage"):
        out("## Lineage summary (Metadata API)")
        out("")
        lineage = sections.get("lineage")
        nodes = (lineage or {}).get("workbooks") if isinstance(lineage, dict) else None
        if not nodes:
            out("_Not available — see the Errors section._")
        else:
            with_tables = sum(1 for n in nodes if n.get("upstreamTables"))
            db_counter = Counter()
            for node in nodes:
                for table in node.get("upstreamTables") or []:
                    database = table.get("database") or {}
                    db_counter[
                        (
                            database.get("name") or "(unknown)",
                            database.get("connectionType") or "",
                        )
                    ] += 1
            out(f"- Workbooks returned by the Metadata API: {len(nodes)}")
            out(f"- Workbooks with resolved upstream tables: {with_tables}")
            out(f"- Distinct upstream databases: {len(db_counter)}")
            out("")
            if db_counter:
                rows = [
                    [name, conn_type, str(count)]
                    for (name, conn_type), count in db_counter.most_common(15)
                ]
                lines.extend(
                    _table(["Database", "Connection type", "Table references"], rows)
                )
        out("")

    # -- Errors ------------------------------------------------------------
    out("## Errors")
    out("")
    if not errors:
        out("All sections completed successfully.")
    else:
        out(
            f"{len(errors)} section(s) failed or were skipped; everything "
            "else completed:"
        )
        out("")
        for entry in errors:
            out(f"- **{_md(entry['section'])}**: {_md(entry['error'])}")
    out("")

    out("---")
    out("")
    out(
        "*Share this `report.md` (and `site_inventory.json` for data-level "
        "questions) back to Claude for follow-up analysis — e.g. cleanup "
        "candidates, ownership gaps, refresh-schedule overlaps, or usage "
        "trends.*"
    )
    out("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tableau_deep_dig.py",
        description=(
            "Deep-dig audit of one Tableau Server site: projects, workbooks, "
            "views (with usage), datasources, users, groups, subscriptions, "
            "schedules, extract refreshes, flows — plus optional connections, "
            "permissions and Metadata-API lineage. Writes "
            "<out>/site_inventory.json and <out>/report.md."
        ),
        epilog=(
            "Exit code: 0 even when individual sections fail (failures are "
            "recorded in the report and JSON); nonzero only when sign-in "
            "itself fails or no auth method was provided."
        ),
    )
    parser.add_argument(
        "--server",
        default=None,
        help=(
            "Tableau Server base URL, e.g. http://tableau.example.com "
            "(required unless env TABLEAU_SERVER is set)"
        ),
    )
    parser.add_argument(
        "--site",
        default=None,
        help=(
            "Site contentUrl, e.g. Automotive. Default: env TABLEAU_SITE, "
            "else empty string = the Default site"
        ),
    )
    parser.add_argument(
        "--user",
        default=None,
        help=(
            "Username (env TABLEAU_USER). If given without --password, "
            "prompts via getpass"
        ),
    )
    parser.add_argument(
        "--password", default=None, help="Password (env TABLEAU_PASSWORD)"
    )
    parser.add_argument(
        "--pat-name",
        default=None,
        help=(
            "Personal access token name (env TABLEAU_PAT_NAME); "
            "requires REST API >= 3.6"
        ),
    )
    parser.add_argument(
        "--pat-secret",
        default=None,
        help="Personal access token secret (env TABLEAU_PAT_SECRET)",
    )
    parser.add_argument(
        "--connections",
        action="store_true",
        help=(
            "Also fetch per-workbook and per-datasource connection details (N+1 calls)"
        ),
    )
    parser.add_argument(
        "--permissions",
        action="store_true",
        help="Also fetch per-workbook permissions (N+1 calls, usually needs admin)",
    )
    parser.add_argument(
        "--lineage",
        action="store_true",
        help=(
            "Also query the Metadata API (GraphQL) for upstream "
            "databases/tables (2019.3+)"
        ),
    )
    parser.add_argument(
        "--out",
        default="tableau_dig_output",
        help="Output directory (default: tableau_dig_output)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-request timeout in seconds (default 30)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Page size for paged endpoints (default 100, max 1000)",
    )
    parser.add_argument(
        "--api-version",
        default=None,
        help="Override the negotiated REST API version, e.g. 3.4",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (self-signed https)",
    )
    parser.add_argument(
        "--use-env-proxy",
        action="store_true",
        help=(
            "Honor HTTP_PROXY/HTTPS_PROXY env vars. DEFAULT is to bypass all "
            "proxies, because corporate proxies usually cannot reach the "
            "internal Tableau host"
        ),
    )
    return parser


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    server = args.server or os.environ.get("TABLEAU_SERVER", "")
    if not server:
        parser.error("--server is required (or set env TABLEAU_SERVER)")
    if "://" not in server:
        server = f"http://{server}"
    site = args.site if args.site is not None else os.environ.get("TABLEAU_SITE", "")
    user = args.user or os.environ.get("TABLEAU_USER", "")
    password = args.password or os.environ.get("TABLEAU_PASSWORD", "")
    pat_name = args.pat_name or os.environ.get("TABLEAU_PAT_NAME", "")
    pat_secret = args.pat_secret or os.environ.get("TABLEAU_PAT_SECRET", "")

    if user and not password and not (pat_name and pat_secret):
        try:
            password = getpass.getpass(f"Tableau password for {user}: ")
        except (EOFError, KeyboardInterrupt):  # non-interactive stdin
            password = ""

    has_password_auth = bool(user and password)
    has_pat_auth = bool(pat_name and pat_secret)
    if not has_password_auth and not has_pat_auth:
        _log(
            "error: no auth method provided — use --user/--password or "
            "--pat-name/--pat-secret (or the TABLEAU_* env vars)"
        )
        return 2

    args.page_size = max(1, min(args.page_size, 1000))

    client = TableauClient(
        server,
        timeout=args.timeout,
        insecure=args.insecure,
        use_env_proxy=args.use_env_proxy,
    )

    _log(f"[.] negotiating API version with {server} ...")
    info = client.negotiate(args.api_version)
    if info.get("negotiation_error"):
        _log(
            f"[!] /serverinfo failed ({info['negotiation_error']}); "
            f"assuming an old server, REST API {client.api_version}"
        )
    else:
        _log(
            f"[+] server {info.get('product_version', '?')} — "
            f"using REST API {client.api_version}"
        )

    use_pat = has_pat_auth
    if use_pat and client.version_tuple() < (3, 6):
        if has_password_auth:
            _log(
                "[!] personal access tokens need REST API 3.6+; "
                "falling back to username/password"
            )
            use_pat = False
        else:
            _log(
                "error: personal access tokens require REST API 3.6+ "
                f"but this server negotiated {client.api_version}; "
                "use --user/--password instead"
            )
            return 2

    _log(f"[.] signing in to site '{site or '(Default)'}' ...")
    try:
        if use_pat:
            client.sign_in(site, pat_name=pat_name, pat_secret=pat_secret)
        else:
            client.sign_in(site, user=user, password=password)
    except TableauError as exc:
        _log(f"error: sign-in failed: {exc}")
        return 2
    _log(f"[+] signed in (site id {client.site_id})")

    try:
        sections, errors = collect_inventory(client, args)
    finally:
        client.sign_out()

    inventory = {
        "tool": TOOL_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server": {"url": server, **client.server_info},
        "site": {"id": client.site_id, "content_url": client.site_content_url or site},
        "options": {
            "connections": args.connections,
            "permissions": args.permissions,
            "lineage": args.lineage,
            "page_size": args.page_size,
            "timeout": args.timeout,
            "insecure": args.insecure,
            "use_env_proxy": args.use_env_proxy,
        },
        "sections": sections,
        "errors": errors,
    }

    os.makedirs(args.out, exist_ok=True)
    json_path = os.path.join(args.out, "site_inventory.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(inventory, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")
    report_path = os.path.join(args.out, "report.md")
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(build_report(inventory))

    _log(f"[+] wrote {json_path}")
    _log(f"[+] wrote {report_path}")
    if errors:
        _log(
            f"[!] {len(errors)} section(s) recorded errors — see the Errors "
            "section of the report"
        )
    _log("[+] done — share report.md (and site_inventory.json) with Claude")
    return 0


if __name__ == "__main__":
    sys.exit(main())
