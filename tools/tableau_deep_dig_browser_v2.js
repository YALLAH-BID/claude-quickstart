(async () => {
  const log = (m) => console.log('[tableau-dig-v2] ' + m);
  const oops = (m) => console.error('[tableau-dig-v2] ' + m);
  try {
    const server = location.origin;
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
        method, headers: Object.assign({}, JSON_HEADERS, extra || {}),
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

    let ver = '3.6'; let productVersion = '?';
    try {
      const si = await call('GET', '/api/2.4/serverinfo');
      ver = (si.serverInfo && si.serverInfo.restApiVersion) || ver;
      productVersion = (si.serverInfo && si.serverInfo.productVersion && si.serverInfo.productVersion.value) || '?';
      log('server ' + productVersion + ' - REST API ' + ver);
    } catch (e) { log('serverinfo failed (' + e.message + '); assuming REST API ' + ver); }

    log('signing in to site "' + (site || '(Default)') + '" ...');
    const signin = await call('POST', '/api/' + ver + '/auth/signin', {
      credentials: { personalAccessTokenName: patName, personalAccessTokenSecret: patSecret, site: { contentUrl: site } },
    });
    const token = signin.credentials.token;
    const siteId = signin.credentials.site.id;
    const AUTH = { 'X-Tableau-Auth': token };
    const authGet = (path) => call('GET', path, null, AUTH);
    const authBytes = async (path) => {
      const res = await fetch(server + path, { headers: AUTH, credentials: 'omit' });
      if (!res.ok) { const e = new Error('HTTP ' + res.status); e.status = res.status; throw e; }
      return await res.arrayBuffer();
    };
    log('signed in (site id ' + siteId + ')');

    const PAGE = 100, MAX_PAGES = 200;
    const collect = async (name, path) => {
      const items = []; let page = 1;
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

    const inv = { tool: 'tableau_deep_dig (browser v2)', server, site, productVersion, restApiVersion: ver, sections: {}, errors: [] };
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

    const wbs = inv.sections.workbooks || [];
    const dss = inv.sections.datasources || [];
    const unwrap = (data, key, inner) => {
      const c = (data && data[key]) || {};
      const raw = c[inner] || [];
      return Array.isArray(raw) ? raw : (raw ? [raw] : []);
    };

    // --- v2: per-workbook and per-datasource connections ---
    await section('workbook_connections', async () => {
      const out = [];
      for (let i = 0; i < wbs.length; i++) {
        const w = wbs[i];
        const rec = { workbookId: w.id, workbook: w.name, project: w.project && w.project.name };
        try {
          const data = await authGet(base + '/workbooks/' + w.id + '/connections');
          rec.connections = unwrap(data, 'connections', 'connection');
        } catch (e) { rec.error = e.message; }
        out.push(rec);
      }
      log('workbook connections: ' + out.filter((r) => r.connections).length + '/' + wbs.length + ' readable');
      return out;
    });
    await section('datasource_connections', async () => {
      const out = [];
      for (const d of dss) {
        const rec = { datasourceId: d.id, datasource: d.name, project: d.project && d.project.name };
        try {
          const data = await authGet(base + '/datasources/' + d.id + '/connections');
          rec.connections = unwrap(data, 'connections', 'connection');
        } catch (e) { rec.error = e.message; }
        out.push(rec);
      }
      log('datasource connections: ' + out.filter((r) => r.connections).length + '/' + dss.length + ' readable');
      return out;
    });

    // --- v2: workbook revision history ---
    await section('workbook_revisions', async () => {
      const out = [];
      for (const w of wbs) {
        const rec = { workbookId: w.id, workbook: w.name };
        try {
          const data = await authGet(base + '/workbooks/' + w.id + '/revisions');
          rec.revisions = unwrap(data, 'revisions', 'revision').map((r) => ({
            revisionNumber: r.revisionNumber, publishedAt: r.publishedAt, current: r.current,
            publisher: r.publisher && r.publisher.name,
          }));
        } catch (e) { rec.error = e.message; }
        out.push(rec);
      }
      log('revisions: ' + out.filter((r) => r.revisions).length + '/' + wbs.length + ' readable');
      return out;
    });

    // --- v2: Metadata API lineage (read-only GraphQL query) ---
    await section('lineage', async () => {
      const q = 'query tableauDeepDigV2 { workbooks { name projectName embeddedDatasources { name upstreamTables { name schema database { name connectionType } } } upstreamDatasources { name } } }';
      const res = await fetch(server + '/api/metadata/graphql', {
        method: 'POST', headers: Object.assign({}, JSON_HEADERS, AUTH),
        body: JSON.stringify({ query: q }), credentials: 'omit',
      });
      const text = await res.text();
      let data = null;
      try { data = JSON.parse(text); } catch (e) { throw new Error('Metadata API unavailable (non-JSON response, HTTP ' + res.status + ')'); }
      if (!res.ok || (data.errors && data.errors.length)) {
        throw new Error('Metadata API: ' + (data.errors && data.errors[0] && data.errors[0].message || ('HTTP ' + res.status)));
      }
      const rows = (data.data && data.data.workbooks) || [];
      log('lineage: ' + rows.length + ' workbook node(s)');
      return rows;
    });

    // --- v2: workbook internals (.twb / .twbx definition parsing) ---
    const extractTwbFromZip = async (buf) => {
      const b = new Uint8Array(buf); const dv = new DataView(buf); const td = new TextDecoder();
      let eocd = -1;
      for (let i = b.length - 22; i >= Math.max(0, b.length - 22 - 65536); i--) {
        if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
      }
      if (eocd < 0) throw new Error('zip: end record not found');
      const count = dv.getUint16(eocd + 10, true);
      let off = dv.getUint32(eocd + 16, true);
      for (let i = 0; i < count; i++) {
        if (dv.getUint32(off, true) !== 0x02014b50) throw new Error('zip: bad central header');
        const method = dv.getUint16(off + 10, true);
        const csize = dv.getUint32(off + 20, true);
        const nlen = dv.getUint16(off + 28, true);
        const elen = dv.getUint16(off + 30, true);
        const clen = dv.getUint16(off + 32, true);
        const lho = dv.getUint32(off + 42, true);
        const name = td.decode(b.subarray(off + 46, off + 46 + nlen));
        if (name.toLowerCase().endsWith('.twb')) {
          const lnlen = dv.getUint16(lho + 26, true);
          const lelen = dv.getUint16(lho + 28, true);
          const start = lho + 30 + lnlen + lelen;
          const comp = b.subarray(start, start + csize);
          if (method === 0) return td.decode(comp);
          if (method === 8) {
            const stream = new Blob([comp]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
            return await new Response(stream).text();
          }
          throw new Error('zip: unsupported compression method ' + method);
        }
        off += 46 + nlen + elen + clen;
      }
      throw new Error('zip: no .twb entry found');
    };
    const parseTwb = (xml) => {
      const doc = new DOMParser().parseFromString(xml, 'text/xml');
      if (doc.querySelector('parsererror')) throw new Error('workbook XML did not parse');
      const out = { customSql: [], calcFields: [], connections: [], counts: {} };
      doc.querySelectorAll('relation[type="text"]').forEach((r) => {
        const t = (r.textContent || '').trim();
        if (t) out.customSql.push(t.length > 1200 ? t.slice(0, 1200) + ' ...[truncated]' : t);
      });
      doc.querySelectorAll('column > calculation[formula]').forEach((c) => {
        const col = c.parentElement;
        const f = c.getAttribute('formula') || '';
        out.calcFields.push({ name: (col && (col.getAttribute('caption') || col.getAttribute('name'))) || '?',
          length: f.length, formula: f.length > 500 ? f.slice(0, 500) + ' ...[truncated]' : f });
      });
      const seen = {};
      doc.querySelectorAll('connection[class]').forEach((c) => {
        const rec = { class: c.getAttribute('class') || '', server: c.getAttribute('server') || '',
          dbname: c.getAttribute('dbname') || '', username: c.getAttribute('username') || '' };
        const k = rec.class + '|' + rec.server + '|' + rec.dbname;
        if (!seen[k]) { seen[k] = 1; out.connections.push(rec); }
      });
      out.counts = { worksheets: doc.querySelectorAll('worksheet').length,
        dashboards: doc.querySelectorAll('dashboard').length,
        parameters: doc.querySelectorAll('datasource[name="Parameters"] column').length };
      return out;
    };
    await section('workbook_internals', async () => {
      const out = [];
      for (let i = 0; i < wbs.length; i++) {
        const w = wbs[i];
        const rec = { workbookId: w.id, workbook: w.name, project: w.project && w.project.name };
        try {
          const buf = await authBytes(base + '/workbooks/' + w.id + '/content?includeExtract=false');
          rec.bytes = buf.byteLength;
          if (buf.byteLength > 60 * 1024 * 1024) { rec.skipped = 'too large to parse in browser'; out.push(rec); continue; }
          const head = new Uint8Array(buf.slice(0, 2));
          let xml = null;
          if (head[0] === 0x50 && head[1] === 0x4b) { rec.kind = 'twbx'; xml = await extractTwbFromZip(buf); }
          else { rec.kind = 'twb'; xml = new TextDecoder().decode(buf); }
          const parsed = parseTwb(xml);
          rec.customSqlCount = parsed.customSql.length;
          rec.customSql = parsed.customSql;
          rec.calcFieldCount = parsed.calcFields.length;
          rec.calcFields = parsed.calcFields;
          rec.connections = parsed.connections;
          rec.counts = parsed.counts;
        } catch (e) { rec.error = (e.status === 403 ? 'no download permission (403)' : e.message); }
        out.push(rec);
        log('internals ' + (i + 1) + '/' + wbs.length + ': ' + w.name + (rec.kind ? ' (' + rec.kind + ')' : ' - ' + rec.error));
      }
      return out;
    });

    try { await call('POST', '/api/' + ver + '/auth/signout', null, AUTH); } catch (e) {}

    // ------- report -------
    const S = inv.sections;
    const n = (x) => (x && x.length) || 0;
    const esc = (s) => String(s == null ? '' : s).replace(/\|/g, '\\|').replace(/\n/g, ' ');
    const L = [];
    L.push('# Tableau Deep Dig v2 - ' + (site || 'Default') + ' site'); L.push('');
    L.push('Server `' + server + '` (' + productVersion + ', REST API ' + ver + '). Connections, lineage, revisions and workbook internals.'); L.push('');

    const sysUse = {};
    const noteSys = (srv, type, who) => {
      if (!srv) return;
      const k = srv + '|' + (type || '');
      (sysUse[k] = sysUse[k] || { server: srv, type: type || '', users: new Set() }).users.add(who);
    };
    (S.workbook_connections || []).forEach((r) => (r.connections || []).forEach((c) => noteSys(c.serverAddress, c.type, 'wb:' + r.workbook)));
    (S.datasource_connections || []).forEach((r) => (r.connections || []).forEach((c) => noteSys(c.serverAddress, c.type, 'ds:' + r.datasource)));
    (S.workbook_internals || []).forEach((r) => (r.connections || []).forEach((c) => noteSys(c.server, c.class, 'wb:' + r.workbook)));
    const sysRows = Object.values(sysUse).sort((a, b) => b.users.size - a.users.size);
    L.push('## Upstream systems'); L.push('');
    if (sysRows.length) {
      L.push('| Server | Type | Used by |'); L.push('|---|---|---|');
      sysRows.forEach((s) => L.push('| ' + esc(s.server) + ' | ' + esc(s.type) + ' | ' + s.users.size + ' item(s) |'));
    } else L.push('- (no connection details readable with this account)');
    L.push('');

    const embedded = [];
    (S.workbook_connections || []).forEach((r) => (r.connections || []).forEach((c) => {
      if (String(c.embedPassword) === 'true') embedded.push('workbook "' + r.workbook + '" -> ' + (c.type || '?') + ' ' + (c.serverAddress || '') + ' as ' + (c.userName || '?'));
    }));
    (S.datasource_connections || []).forEach((r) => (r.connections || []).forEach((c) => {
      if (String(c.embedPassword) === 'true') embedded.push('data source "' + r.datasource + '" -> ' + (c.type || '?') + ' ' + (c.serverAddress || '') + ' as ' + (c.userName || '?'));
    }));
    L.push('## Connections with embedded credentials'); L.push('');
    if (embedded.length) embedded.forEach((x) => L.push('- ' + esc(x)));
    else L.push('- (none readable, or none embed passwords)');
    L.push('');

    const wi = S.workbook_internals || [];
    const parsedWi = wi.filter((r) => r.kind);
    L.push('## Workbook internals'); L.push('');
    L.push('Parsed **' + parsedWi.length + '** of ' + wi.length + ' workbook definitions (' +
      wi.filter((r) => r.kind === 'twb').length + ' twb, ' + wi.filter((r) => r.kind === 'twbx').length + ' twbx; ' +
      wi.filter((r) => r.error).length + ' unreadable).'); L.push('');
    const sqlWbs = parsedWi.filter((r) => r.customSqlCount);
    L.push('### Custom SQL'); L.push('');
    if (sqlWbs.length) {
      L.push('| Workbook | Custom SQL blocks |'); L.push('|---|---|');
      sqlWbs.sort((a, b) => b.customSqlCount - a.customSqlCount).forEach((r) => L.push('| ' + esc(r.workbook) + ' | ' + r.customSqlCount + ' |'));
      L.push(''); L.push('Full SQL text is in site_inventory_v2.json under workbook_internals.customSql.');
    } else L.push('- none found in the parsed workbooks');
    L.push('');
    const allCalc = [];
    parsedWi.forEach((r) => (r.calcFields || []).forEach((c) => allCalc.push({ wb: r.workbook, name: c.name, length: c.length })));
    L.push('### Calculated fields'); L.push('');
    if (allCalc.length) {
      L.push(allCalc.length + ' calculated fields across ' + parsedWi.filter((r) => r.calcFieldCount).length + ' workbook(s). Longest:'); L.push('');
      L.push('| Field | Workbook | Formula length |'); L.push('|---|---|---|');
      allCalc.sort((a, b) => b.length - a.length).slice(0, 10).forEach((c) => L.push('| ' + esc(c.name) + ' | ' + esc(c.wb) + ' | ' + c.length + ' |'));
    } else L.push('- none found');
    L.push('');

    L.push('## Revision history'); L.push('');
    const revs = (S.workbook_revisions || []).filter((r) => r.revisions && r.revisions.length);
    if (revs.length) {
      L.push('| Workbook | Revisions | Last publisher |'); L.push('|---|---|---|');
      revs.sort((a, b) => b.revisions.length - a.revisions.length).forEach((r) => {
        const last = r.revisions[r.revisions.length - 1] || {};
        L.push('| ' + esc(r.workbook) + ' | ' + r.revisions.length + ' | ' + esc(last.publisher || '?') + ' |');
      });
    } else L.push('- (revision history not readable with this account)');
    L.push('');

    L.push('## Lineage (Metadata API)'); L.push('');
    const lin = S.lineage || [];
    if (lin.length) {
      lin.forEach((w) => {
        const tables = [];
        (w.embeddedDatasources || []).forEach((d) => (d.upstreamTables || []).forEach((t) =>
          tables.push((t.database && t.database.name ? t.database.name + '.' : '') + (t.schema ? t.schema + '.' : '') + t.name)));
        const ups = (w.upstreamDatasources || []).map((d) => d.name);
        L.push('- **' + esc(w.name) + '** [' + esc(w.projectName) + ']' +
          (ups.length ? ' <- published: ' + esc(ups.join(', ')) : '') +
          (tables.length ? ' <- tables: ' + esc(tables.slice(0, 8).join(', ')) + (tables.length > 8 ? ' (+' + (tables.length - 8) + ' more)' : '') : ''));
      });
    } else L.push('- (Metadata API unavailable or empty)');
    L.push('');
    L.push('## Sections not available'); L.push('');
    if (inv.errors.length) inv.errors.forEach((e) => L.push('- **' + e.section + '**: ' + esc(e.error)));
    else L.push('- (none - every section returned)');
    L.push('');
    L.push('_Read-only snapshot; no credentials or tokens are included. Pair with site_inventory_v2.json for full detail._');

    const report = L.join('\n');
    const dl = (fname, text, type) => {
      const blob = new Blob([text], { type: type || 'text/plain' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = fname;
      document.body.appendChild(a); a.click(); a.remove();
    };
    dl('report_v2.md', report, 'text/markdown');
    dl('site_inventory_v2.json', JSON.stringify(inv, null, 2), 'application/json');
    log('DONE - report_v2.md and site_inventory_v2.json downloaded. Sections that failed: ' + inv.errors.length);
  } catch (e) {
    oops('FAILED: ' + (e && e.message ? e.message : e));
    oops('Sign-in failures: re-check the token NAME and SECRET, and that the site name matches what is after /site/ in the address bar.');
  }
})();
