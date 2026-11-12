// A local stand for upstream's web build, so the battalion map can be clicked through BEFORE a deploy.
//
//   node tools/upstream/atlas/stand.mjs --dist <upstream worktree>/web/dist [--port 5179]
//
// Serves the given build like nginx does for upstreamlion.com: static files + index.html fallback,
// /api → the live API on 127.0.0.1:8201 (so login and data are real), /tiles/upstream.pmtiles and
// /atlas/atlas.json from disk with range support. No auth gate in front of the two files (nginx adds
// it in production); bound to 127.0.0.1 only. The web build in the live checkout is never touched.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

const argv = process.argv.slice(2);
const opt = (n, d) => { const i = argv.indexOf(`--${n}`); return i >= 0 ? argv[i + 1] : d; };
const DIST = path.resolve(opt('dist', ''));
const PORT = Number(opt('port', 5179));
if (!fs.existsSync(path.join(DIST, 'index.html'))) { console.error(`no build at ${DIST}`); process.exit(2); }
const FILES = { '/tiles/upstream.pmtiles': '/var/lib/upstream-tiles/www/upstream.pmtiles', '/atlas/atlas.json': '/var/lib/upstream-atlas/atlas.json' };
const TYPES = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html; charset=utf-8', '.json': 'application/json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon', '.webmanifest': 'application/manifest+json', '.woff2': 'font/woff2' };

function sendFile(req, res, file) {
  let st;
  try { st = fs.statSync(file); } catch { res.writeHead(404); return res.end(); }
  const type = TYPES[path.extname(file)] || 'application/octet-stream';
  const m = /bytes=(\d*)-(\d*)/.exec(req.headers.range || '');
  if (m) {
    const start = m[1] ? Number(m[1]) : st.size - Number(m[2]);
    const end = m[1] && m[2] ? Number(m[2]) : st.size - 1;
    res.writeHead(206, { 'Content-Type': type, 'Content-Range': `bytes ${start}-${end}/${st.size}`, 'Content-Length': end - start + 1, 'Accept-Ranges': 'bytes' });
    return fs.createReadStream(file, { start, end }).pipe(res);
  }
  res.writeHead(200, { 'Content-Type': type, 'Content-Length': st.size, 'Accept-Ranges': 'bytes' });
  fs.createReadStream(file).pipe(res);
}

http.createServer((req, res) => {
  const url = new URL(req.url, 'http://x');
  if (url.pathname.startsWith('/api/')) {
    const up = http.request({ host: '127.0.0.1', port: 8201, path: req.url, method: req.method,
      headers: { ...req.headers, host: 'upstreamlion.com', 'x-forwarded-proto': 'https', 'x-real-ip': '127.0.0.1' } }, (r) => {
      res.writeHead(r.statusCode, r.headers);
      r.pipe(res);
    });
    up.on('error', (e) => { res.writeHead(502); res.end(String(e)); });
    return req.pipe(up);
  }
  if (FILES[url.pathname]) return sendFile(req, res, FILES[url.pathname]);
  const f = path.join(DIST, path.normalize(url.pathname));
  if (f.startsWith(DIST) && fs.existsSync(f) && fs.statSync(f).isFile()) return sendFile(req, res, f);
  sendFile(req, res, path.join(DIST, 'index.html'));
}).listen(PORT, '127.0.0.1', () => console.log(`stand: http://127.0.0.1:${PORT}  (dist ${DIST})`));
