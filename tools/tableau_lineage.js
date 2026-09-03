(async () => {
  const log = (m) => console.log('[tableau-lineage] ' + m);
  const oops = (m) => console.error('[tableau-lineage] ' + m);
  try {
    const server = location.origin;
    let site = '';
    const m = location.hash.match(/\/site\/([^/]+)/) || location.pathname.match(/\/site\/([^/]+)/);
    if (m) site = decodeURIComponent(m[1]);
    if (!site) site = prompt('Tableau site name (leave blank for the Default site)', 'Automotive') || '';
    const patName = prompt('Personal Access Token NAME');
    if (!patName) { oops('cancelled: no token name given'); return; }
    const patSecret = prompt('Personal Access Token SECRET (paste it here)');
    if (!patSecret) { oops('cancelled: no token secret given'); return; }

    const J = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
    const call = async (method, path, body, extra) => {
      const res = await fetch(server + path, {
        method, headers: Object.assign({}, J, extra || {}),
        body: body ? JSON.stringify(body) : undefined, credentials: 'omit',
      });
      const text = await res.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) {}
      if (!res.ok) {
        const detail = data && data.error ? (data.error.code + ': ' + data.error.summary) : ('HTTP ' + res.status);
        const e = new Error(detail); e.status = res.status; throw e;
      }
      return data;
    };

    let ver = '3.6';
    try {
      const si = await call('GET', '/api/2.4/serverinfo');
      ver = (si.serverInfo && si.serverInfo.restApiVersion) || ver;
      log('REST API ' + ver);
    } catch (e) { log('serverinfo failed; assuming ' + ver); }

    log('signing in to site "' + (site || '(Default)') + '" ...');
    const signin = await call('POST', '/api/' + ver + '/auth/signin', {
      credentials: { personalAccessTokenName: patName, personalAccessTokenSecret: patSecret, site: { contentUrl: site } },
    });
    const token = signin.credentials.token;
    log('signed in');

    // Each query runs independently so one unsupported field cannot sink the rest.
    const gql = async (label, query) => {
      const res = await fetch(server + '/api/metadata/graphql', {
        method: 'POST',
        headers: Object.assign({}, J, { 'X-Tableau-Auth': token }),
        body: JSON.stringify({ query }), credentials: 'omit',
      });
      const text = await res.text();
      let data = null;
      try { data = JSON.parse(text); } catch (e) {
        throw new Error('non-JSON response (HTTP ' + res.status + '): ' + text.slice(0, 200));
      }
      if (data.errors && data.errors.length) {
        throw new Error(data.errors.map((e) => e.message).join(' | '));
      }
      if (!res.ok) throw new Error('HTTP ' + res.status);
      log(label + ': ok');
      return data.data || {};
    };

    const out = { tool: 'tableau_lineage', server, site, restApiVersion: ver, queries: {}, errors: [] };
    const run = async (label, query) => {
      try { out.queries[label] = await gql(label, query); }
      catch (e) { out.errors.push({ query: label, error: e.message }); oops(label + ': ' + e.message); }
    };

    await run('workbooks', `query { workbooks {
      name projectName
      embeddedDatasources { name
        upstreamTables { name schema database { name connectionType } }
        upstreamDatabases { name connectionType } }
      upstreamDatasources { name projectName hasExtracts }
    } }`);

    await run('publishedDatasources', `query { publishedDatasources {
      name projectName hasExtracts
      upstreamTables { name schema database { name connectionType } }
      upstreamDatabases { name connectionType }
    } }`);

    await run('databases', `query { databases { name connectionType } }`);

    try { await call('POST', '/api/' + ver + '/auth/signout', null, { 'X-Tableau-Auth': token }); } catch (e) {}

    // ---- report ----
    const esc = (s) => String(s == null ? '' : s).replace(/\|/g, '\\|').replace(/\n/g, ' ');
    const L = [];
    L.push('# Tableau Lineage - ' + (site || 'Default') + ' site'); L.push('');
    L.push('Server `' + server + '`. Metadata API (read-only GraphQL).'); L.push('');

    const dbs = (out.queries.databases && out.queries.databases.databases) || [];
    L.push('## Upstream databases seen by the Metadata API'); L.push('');
    if (dbs.length) {
      L.push('| Database | Connection type |'); L.push('|---|---|');
      dbs.forEach((d) => L.push('| ' + esc(d.name) + ' | ' + esc(d.connectionType) + ' |'));
    } else L.push('- (none returned)');
    L.push('');

    const pds = (out.queries.publishedDatasources && out.queries.publishedDatasources.publishedDatasources) || [];
    L.push('## Published data sources -> upstream'); L.push('');
    if (pds.length) {
      L.push('| Data source | Project | Extract | Upstream tables | Upstream databases |');
      L.push('|---|---|---|---|---|');
      pds.forEach((d) => {
        const t = (d.upstreamTables || []).map((x) => (x.database && x.database.name ? x.database.name + '.' : '') + (x.schema ? x.schema + '.' : '') + x.name);
        const db = (d.upstreamDatabases || []).map((x) => x.name + ' (' + x.connectionType + ')');
        L.push('| ' + esc(d.name) + ' | ' + esc(d.projectName) + ' | ' + (d.hasExtracts ? 'yes' : 'no') + ' | ' +
          esc(t.slice(0, 6).join(', ') + (t.length > 6 ? ' (+' + (t.length - 6) + ')' : '')) + ' | ' + esc(db.join(', ')) + ' |');
      });
    } else L.push('- (none returned)');
    L.push('');

    const wbs = (out.queries.workbooks && out.queries.workbooks.workbooks) || [];
    L.push('## Workbooks -> upstream'); L.push('');
    if (wbs.length) {
      wbs.forEach((w) => {
        const pub = (w.upstreamDatasources || []).map((d) => d.name + (d.projectName ? ' [' + d.projectName + ']' : ''));
        const tabs = [];
        (w.embeddedDatasources || []).forEach((d) => (d.upstreamTables || []).forEach((t) =>
          tabs.push((t.database && t.database.name ? t.database.name + '.' : '') + (t.schema ? t.schema + '.' : '') + t.name)));
        L.push('- **' + esc(w.name) + '** [' + esc(w.projectName) + ']' +
          (pub.length ? '  \n    published sources: ' + esc(pub.join(', ')) : '') +
          (tabs.length ? '  \n    direct tables: ' + esc([...new Set(tabs)].slice(0, 10).join(', ')) : ''));
      });
    } else L.push('- (none returned)');
    L.push('');

    L.push('## Queries that failed'); L.push('');
    if (out.errors.length) {
      out.errors.forEach((e) => L.push('- **' + e.query + '**: ' + esc(e.error)));
      L.push('');
      L.push('_If the message mentions the Metadata API store still being created, wait and re-run - it is a temporary indexing state, not a permission problem._');
    } else L.push('- (none)');
    L.push('');
    L.push('_Read-only. No credentials or tokens are included in this file._');

    const dl = (fname, text, type) => {
      const blob = new Blob([text], { type: type || 'text/plain' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = fname;
      document.body.appendChild(a); a.click(); a.remove();
    };
    dl('lineage.md', L.join('\n'), 'text/markdown');
    dl('lineage.json', JSON.stringify(out, null, 2), 'application/json');
    log('DONE - lineage.md and lineage.json downloaded. Failed queries: ' + out.errors.length);
  } catch (e) {
    oops('FAILED: ' + (e && e.message ? e.message : e));
    oops('Sign-in failures: re-check the token NAME and SECRET and the site name after /site/ in the URL.');
  }
})();
