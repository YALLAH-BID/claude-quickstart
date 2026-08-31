(async () => {
  const log = (m) => console.log('[tableau-dig] ' + m);
  const oops = (m) => console.error('[tableau-dig] ' + m);
  try {
    const server = location.origin;

    // Derive the site's contentUrl from the page URL (…/site/<contentUrl>/…).
    let site = '';
    const m = location.hash.match(/\/site\/([^/]+)/) || location.pathname.match(/\/site\/([^/]+)/);
    if (m) site = decodeURIComponent(m[1]);
    if (!site) site = prompt('Tableau site name (leave blank for the Default site)', 'Automotive') || '';

    const patName = prompt('Personal Access Token NAME (e.g. Tableau-REST-API)');
    if (!patName) { oops('cancelled: no token name given'); return; }
    const patSecret = prompt('Personal Access Token SECRET (paste it here)');
    if (!patSecret) { oops('cancelled: no token secret given'); return; }

    const JSON_HEADERS = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
    const call = async (method, path, body, extra) => {
      const res = await fetch(server + path, {
        method,
        headers: Object.assign({}, JSON_HEADERS, extra || {}),
        body: body ? JSON.stringify(body) : undefined,
        credentials: 'omit',
      });
      const text = await res.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) { /* non-JSON */ }
      if (!res.ok) {
        const detail = data && data.error
          ? (data.error.code + ': ' + data.error.summary)
          : ('HTTP ' + res.status);
        const e = new Error(detail); e.status = res.status; throw e;
      }
      return data;
    };

    // 1) Negotiate the REST API version (falls back for old servers).
    let ver = '3.6';
    let productVersion = '?';
    try {
      const si = await call('GET', '/api/2.4/serverinfo');
      ver = (si.serverInfo && si.serverInfo.restApiVersion) || ver;
      productVersion = (si.serverInfo && si.serverInfo.productVersion && si.serverInfo.productVersion.value) || '?';
      log('server ' + productVersion + ' — REST API ' + ver);
    } catch (e) {
      log('serverinfo failed (' + e.message + '); assuming REST API ' + ver);
    }

    // 2) Sign in with the Personal Access Token.
    log('signing in to site "' + (site || '(Default)') + '" …');
    const signin = await call('POST', '/api/' + ver + '/auth/signin', {
      credentials: {
        personalAccessTokenName: patName,
        personalAccessTokenSecret: patSecret,
        site: { contentUrl: site },
      },
    });
    const token = signin.credentials.token;
    const siteId = signin.credentials.site.id;
    const authGet = (path) => call('GET', path, null, { 'X-Tableau-Auth': token });
    log('signed in (site id ' + siteId + ')');

    // Paged fetch: follow pagination.totalAvailable, guard empty/runaway pages.
    const PAGE = 100, MAX_PAGES = 200;
    const collect = async (name, path) => {
      const items = [];
      let page = 1;
      while (page <= MAX_PAGES) {
        const sep = path.includes('?') ? '&' : '?';
        const data = await authGet(path + sep + 'pageSize=' + PAGE + '&pageNumber=' + page);
        const containerKey = Object.keys(data).find((k) => k !== 'pagination');
        const container = (containerKey && data[containerKey]) || {};
        const innerKey = Object.keys(container)[0];
        const raw = innerKey ? container[innerKey] : [];
        const batch = Array.isArray(raw) ? raw : (raw ? [raw] : []);
        items.push(...batch);
        const total = data.pagination ? parseInt(data.pagination.totalAvailable, 10) : items.length;
        if (!batch.length || items.length >= total) break;
        page++;
      }
      log(name + ': ' + items.length);
      return items;
    };

    const inv = { tool: 'tableau_deep_dig (browser)', server, site, productVersion, restApiVersion: ver, sections: {}, errors: [] };
    const section = async (name, fn) => {
      try { inv.sections[name] = await fn(); }
      catch (e) { inv.errors.push({ section: name, error: e.message }); oops(name + ': ' + e.message); }
    };

    const base = '/api/' + ver + '/sites/' + siteId;
    await section('projects', () => collect('projects', base + '/projects'));
    await section('workbooks', () => collect('workbooks', base + '/workbooks'));
    await section('views', () => collect('views', base + '/views?includeUsageStatistics=true'));
    await section('datasources', () => collect('datasources', base + '/datasources'));
    await section('users', () => collect('users', base + '/users'));
    await section('groups', () => collect('groups', base + '/groups'));
    await section('subscriptions', () => collect('subscriptions', base + '/subscriptions'));
    await section('schedules', () => collect('schedules', '/api/' + ver + '/schedules'));
    await section('extract_refresh_tasks', () => collect('extract_refresh_tasks', base + '/tasks/extractRefreshes'));

    // Best-effort sign out.
    try { await call('POST', '/api/' + ver + '/auth/signout', null, { 'X-Tableau-Auth': token }); } catch (e) { /* ignore */ }

    // Build the report.
    const S = inv.sections;
    const n = (x) => (x && x.length) || 0;
    const esc = (s) => String(s == null ? '' : s).replace(/\|/g, '\\|').replace(/\n/g, ' ');
    const days = (iso) => { const t = Date.parse(iso); return isNaN(t) ? null : Math.floor((Date.now() - t) / 86400000); };
    const L = [];
    L.push('# Tableau Deep Dig — ' + (site || 'Default') + ' site');
    L.push('');
    L.push('Server `' + server + '` (' + productVersion + ', REST API ' + ver + '). Generated in-browser.');
    L.push('');
    L.push('## Overview');
    L.push('');
    L.push('| Content | Count |');
    L.push('|---|---|');
    [['Projects', 'projects'], ['Workbooks', 'workbooks'], ['Views', 'views'], ['Data sources', 'datasources'], ['Users', 'users'], ['Groups', 'groups']]
      .forEach(([lbl, k]) => L.push('| ' + lbl + ' | ' + n(S[k]) + ' |'));
    L.push('');

    if (n(S.projects)) {
      L.push('## Projects'); L.push('');
      L.push('| Project | Owner | Description |'); L.push('|---|---|---|');
      S.projects.forEach((p) => L.push('| ' + esc(p.name) + ' | ' + esc(p.owner && p.owner.name) + ' | ' + esc(p.description) + ' |'));
      L.push('');
    }
    if (n(S.workbooks)) {
      L.push('## Workbooks'); L.push('');
      L.push('| Workbook | Project | Owner | Updated | Age (days) |'); L.push('|---|---|---|---|---|');
      S.workbooks.forEach((w) => L.push('| ' + esc(w.name) + ' | ' + esc(w.project && w.project.name) + ' | ' + esc(w.owner && w.owner.name) + ' | ' + esc(w.updatedAt) + ' | ' + (days(w.updatedAt) == null ? '' : days(w.updatedAt)) + ' |'));
      const stale = S.workbooks.filter((w) => { const d = days(w.updatedAt); return d != null && d >= 180; });
      L.push(''); L.push('**' + stale.length + '** workbook(s) not updated in 180+ days.');
      L.push('');
    }
    if (n(S.views)) {
      const views = S.views.map((v) => ({ name: v.name, usage: v.usage ? (parseInt(v.usage.totalViewCount, 10) || 0) : 0 }));
      const zero = views.filter((v) => v.usage === 0).length;
      const top = views.slice().sort((a, b) => b.usage - a.usage).slice(0, 25);
      L.push('## Views — usage'); L.push('');
      L.push('**' + zero + '** of ' + views.length + ' views have zero recorded usage.'); L.push('');
      L.push('Top ' + top.length + ' by view count:'); L.push('');
      L.push('| View | Views |'); L.push('|---|---|');
      top.forEach((v) => L.push('| ' + esc(v.name) + ' | ' + v.usage + ' |'));
      L.push('');
    }
    if (n(S.datasources)) {
      L.push('## Data sources'); L.push('');
      L.push('| Data source | Type | Certified | Updated |'); L.push('|---|---|---|---|');
      S.datasources.forEach((d) => L.push('| ' + esc(d.name) + ' | ' + esc(d.type) + ' | ' + esc(d.isCertified) + ' | ' + esc(d.updatedAt) + ' |'));
      L.push('');
    }
    if (n(S.users)) {
      const byRole = {};
      S.users.forEach((u) => { const r = u.siteRole || 'Unknown'; byRole[r] = (byRole[r] || 0) + 1; });
      L.push('## Users by site role'); L.push('');
      L.push('| Site role | Count |'); L.push('|---|---|');
      Object.keys(byRole).sort().forEach((r) => L.push('| ' + r + ' | ' + byRole[r] + ' |'));
      L.push('');
    }
    if (n(S.groups)) {
      L.push('## Groups'); L.push('');
      S.groups.forEach((g) => L.push('- ' + esc(g.name)));
      L.push('');
    }
    L.push('## Sections not available');
    L.push('');
    if (inv.errors.length) inv.errors.forEach((e) => L.push('- **' + e.section + '**: ' + esc(e.error) + ' (usually means your account is not a site admin — expected for a Viewer)'));
    else L.push('- (none — every section returned)');
    L.push('');
    L.push('_Metadata only — no credentials or token are included. Hand report.md (and site_inventory.json for deeper questions) to Claude for analysis._');

    const report = L.join('\n');

    const dl = (fname, text, type) => {
      const blob = new Blob([text], { type: type || 'text/plain' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      a.remove();
    };
    dl('report.md', report, 'text/markdown');
    dl('site_inventory.json', JSON.stringify(inv, null, 2), 'application/json');
    log('DONE — report.md and site_inventory.json downloaded to your Downloads folder. Sections that failed: ' + inv.errors.length);
  } catch (e) {
    oops('FAILED: ' + (e && e.message ? e.message : e));
    oops('Sign-in failures: re-check the token NAME and SECRET, and that the site name matches exactly what is in the address bar after /site/.');
  }
})();
