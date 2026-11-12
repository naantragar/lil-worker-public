// Self-test of the battalion map: click every control like a person would, and after each click
// compare what is ACTUALLY on the map with what the data says should be there.
//
//   node tools/upstream/atlas/selftest.mjs [/root/upstream-maps/MAP_DD.MM.html] [--shots DIR]
//
// Owner, 12.11.2026: «то якась галочка не працювала, то щось ще» — so the map is not shipped on a
// guess. Expected counts are computed HERE from DATA, independently of the page's own logic; the
// page only exposes what is on the map (window.__atlas.shown()). Runs on a desktop and a phone
// viewport. Prints a JSON report; exit code 1 if any check failed.
import { chromium } from '~/.krevetka-browser/node_modules/playwright/index.mjs';
import fs from 'node:fs';

const argv = process.argv.slice(2);
const opt = (n, d) => { const i = argv.indexOf(`--${n}`); return i >= 0 ? argv[i + 1] : d; };
const file = argv.find(a => !a.startsWith('--') && a !== opt('shots')) ||
  fs.readdirSync('/root/upstream-maps').filter(f => /^MAP_.*\.html$/.test(f)).map(f => '/root/upstream-maps/' + f)
    .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs)[0];
const SHOTS = opt('shots', '/tmp/atlas_selftest');
fs.mkdirSync(SHOTS, { recursive: true });

const VIEWPORTS = { desktop: { width: 1440, height: 900 }, phone: { width: 390, height: 844, isMobile: true, hasTouch: true } };
const report = { file, checks: [], shots: [], errors: [] };
const check = (vp, name, ok, detail) => report.checks.push({ vp, name, ok: !!ok, ...(ok ? {} : { detail }) });

// what SHOULD be shown, from the raw data and the state — the page's logic is not trusted here
function expected(D, st) {
  const today = D.window[1];
  const from = st.period >= 9999 ? '0000' : (() => { const d = new Date(today + 'T12:00:00'); d.setDate(d.getDate() - (st.period - 1)); return d.toISOString().slice(0, 10); })();
  const r = {};
  for (const b of D.battalions) {
    const places = b.places.filter(p => Object.entries(p.days).some(([d, v]) => d >= from && v > 0)).length;
    const points = D.points.filter(p => p.b === b.key && p.d >= from).length;
    r[b.key + ':places'] = st.on[b.key] && st.kinds.places ? places : 0;
    r[b.key + ':zones'] = st.on[b.key] && st.kinds.zones && places ? 1 : 0;
    r[b.key + ':points'] = st.on[b.key] && st.kinds.points ? points : 0;
  }
  const keys = new Set(D.battalions.map(b => b.key));
  r['__pow:points'] = st.kinds.pow ? D.pow.length : 0;
  r['__other:points'] = st.kinds.other ? D.points.filter(p => !(p.b && keys.has(p.b)) && p.d >= from).length : 0;
  return r;
}

async function run(name, vp) {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height }, isMobile: !!vp.isMobile, hasTouch: !!vp.hasTouch });
  page.on('pageerror', e => report.errors.push(`${name}: ${e.message}`));
  page.on('console', m => { if (m.type() === 'error') report.errors.push(`${name}: console ${m.text()}`); });
  await page.goto('file://' + file, { timeout: 60000 });
  await page.waitForFunction(() => window.__atlas, null, { timeout: 60000 });
  const D = await page.evaluate(() => DATA);
  const shot = async tag => { const p = `${SHOTS}/${name}_${tag}.png`; await page.screenshot({ path: p }); report.shots.push(p); };

  const verify = async (label) => {
    const st = await page.evaluate(() => window.__atlas.state());
    const got = await page.evaluate(() => window.__atlas.shown());
    const exp = expected(D, st);
    const bad = Object.keys(exp).filter(k => (got[k] || 0) !== exp[k]).map(k => `${k}: на мапі ${got[k] || 0}, має бути ${exp[k]}`);
    check(name, `${label}: мапа = дані`, !bad.length, bad.slice(0, 6));
    // every box must say what the state says
    const boxes = await page.evaluate(() => [...document.querySelectorAll('input[data-bat]')].map(x => [x.dataset.bat, x.checked]));
    const wrong = boxes.filter(([k, c]) => c !== st.on[k]).map(([k]) => k);
    check(name, `${label}: галочки батальйонів = стан`, !wrong.length, wrong);
    const brig = await page.evaluate(() => [...document.querySelectorAll('input[data-brig]')].map(x => [x.dataset.brig, x.checked, x.indeterminate]));
    const brigBad = brig.filter(([f, c, ind]) => {
      const ks = D.battalions.filter(b => b.formation === f).map(b => b.key), n = ks.filter(k => st.on[k]).length;
      return c !== (n === ks.length) || ind !== (n > 0 && n < ks.length);
    }).map(([f]) => f);
    check(name, `${label}: галочки бригад = стан`, !brigBad.length, brigBad);
    return st;
  };
  const openPanel = async () => {
    if (await page.evaluate(() => document.getElementById('panel').classList.contains('closed'))) await page.click('#toggle');
  };
  const click = async sel => { await openPanel(); await page.locator(sel).first().scrollIntoViewIfNeeded(); await page.click(sel); };

  // 1. first view
  if (vp.isMobile) {
    check(name, 'на телефоні панель спершу згорнута (видно мапу)', await page.evaluate(() => document.getElementById('panel').classList.contains('closed')));
    await shot('0_start_closed');
  }
  {
    const all = D.battalions.flatMap(b => b.places.map(p => [p.lat, p.lon]));
    const seen = await page.evaluate(p => window.__atlas.inView(p), all);
    check(name, `старт: видно всі села, жодне не під панеллю (${seen} з ${all.length})`, seen === all.length);
    const rings = Object.values(await page.evaluate(() => window.__atlas.zones())).flat();
    const seenR = await page.evaluate(p => window.__atlas.inView(p), rings);
    if (!vp.isMobile) check(name, `старт: зони не обрізані краєм екрана (${seenR} з ${rings.length} точок контуру)`, seenR === rings.length);
  }
  await openPanel();
  let st = await verify('старт');
  check(name, 'старт: усі батальйони увімкнені', Object.values(st.on).every(Boolean));
  await shot('1_start');

  // 2. all off / all on
  await click('#allOff');
  st = await verify('«Усе викл»');
  const total = o => Object.values(o).reduce((a, b) => a + b, 0);
  check(name, '«Усе викл»: на мапі нічого', total(await page.evaluate(() => window.__atlas.shown())) === 0);
  await shot('2_all_off');
  await click('#allOn');
  st = await verify('«Усе вкл»');

  // 3. each brigade alone, then each battalion alone
  const brigs = [...new Set(D.battalions.map(b => b.formation))];
  for (const f of brigs) {
    await click('#allOff');
    await click(`input[data-brig="${f}"]`);
    st = await verify(`лише бригада ${f}`);
    const onKeys = Object.keys(st.on).filter(k => st.on[k]);
    check(name, `лише бригада ${f}: увімкнено рівно її батальйони`,
      onKeys.length && onKeys.every(k => k.startsWith(f + ' /')) && D.battalions.filter(b => b.formation === f).every(b => st.on[b.key]), onKeys);
    if (f === brigs[1]) await shot('3_one_brigade');
  }
  for (const b of D.battalions) {
    await click('#allOff');
    await click(`input[data-bat="${b.key}"]`);
    st = await verify(`лише ${b.key}`);
    check(name, `лише ${b.key}: увімкнено один`, Object.values(st.on).filter(Boolean).length === 1 && st.on[b.key]);
  }
  // 4. a brigade box with part of it on: «частково», a click turns the whole brigade on, the next one off
  const f38 = brigs.find(f => D.battalions.filter(b => b.formation === f).length > 1);
  if (f38) {
    await click('#allOff');
    await click(`input[data-bat="${D.battalions.find(b => b.formation === f38).key}"]`);
    await verify(`частково ${f38}`);
    await click(`input[data-brig="${f38}"]`);
    st = await verify(`бригада ${f38} з «частково» → уся`);
    check(name, `бригада ${f38}: з «частково» вмикається вся`, D.battalions.filter(b => b.formation === f38).every(b => st.on[b.key]));
    await click(`input[data-brig="${f38}"]`);
    st = await verify(`бригада ${f38} → вимк`);
    check(name, `бригада ${f38}: другий клік вимикає`, D.battalions.filter(b => b.formation === f38).every(b => !st.on[b.key]));
  }
  // 5. what to show: places / points / other stations
  await click('#allOn');
  for (const k of ['zones', 'places', 'points', 'other', 'pow']) {
    await click(`input[data-kind="${k}"]`);
    await verify(`перемкнув «${k}»`);
    await click(`input[data-kind="${k}"]`);
    await verify(`повернув «${k}»`);
  }
  // 5b. zones: the share of a battalion's intercepts whose village lies inside its oval, per period —
  // computed here with our own point-in-polygon, not the page's
  const inside = (pt, ring) => { let c = false; for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [yi, xi] = ring[i], [yj, xj] = ring[j];
    if ((yi > pt[0]) !== (yj > pt[0]) && pt[1] < (xj - xi) * (pt[0] - yi) / (yj - yi) + xi) c = !c; } return c; };
  const kmAxis = ring => { let m = 0; for (const a of ring) for (const b of ring)
    m = Math.max(m, Math.hypot((a[0] - b[0]) * 110.57, (a[1] - b[1]) * 111.32 * Math.cos(a[0] * Math.PI / 180))); return m; };
  for (const p of [7, 30, 9999]) {
    await click(`button[data-p="${p}"]`);
    const zs = await page.evaluate(() => window.__atlas.zones()), from = await page.evaluate(() => window.__atlas.from());
    for (const b of D.battalions) {
      const rows = b.places.map(x => ({ x, n: Object.entries(x.days).filter(([d]) => d >= from).reduce((a, [, v]) => a + v, 0) })).filter(r => r.n);
      if (!rows.length) { check(name, `зона ${b.key} (${p}): немає сіл — немає зони`, !zs[b.key]); continue; }
      const W = rows.reduce((a, r) => a + r.n, 0), In = rows.filter(r => inside([r.x.lat, r.x.lon], zs[b.key])).reduce((a, r) => a + r.n, 0);
      const axis = kmAxis(zs[b.key]);
      check(name, `зона ${b.key} (${p === 9999 ? 'усе' : p}): всередині ${Math.round(In / W * 100)}% перехоплень, довжина ${axis.toFixed(1)} км`,
        In / W >= 0.6 && axis <= 45, { inside: In / W, axis });
      if (p === 30) report.zones = Object.assign(report.zones || {}, { [b.key]: { inside: Math.round(In / W * 100), km: +axis.toFixed(1), villages: rows.length } });
    }
  }
  await click('button[data-p="30"]');
  // 5c. POW layer: every marker has its debrief named
  await click('input[data-kind="pow"]');
  const powOk = await page.evaluate(() => window.__atlas.shown()['__pow:points']);
  check(name, `полонені: на мапі ${powOk} з ${D.pow.length}`, powOk === D.pow.length);
  check(name, 'полонені: у кожної точки є протокол і опис', D.pow.every(p => p.doc && p.what.length));
  await shot('4b_pow');
  await click('input[data-kind="pow"]');
  // 6. every period
  for (const p of [3, 7, 14, 30, 9999]) {
    await click(`button[data-p="${p}"]`);
    st = await verify(`період ${p === 9999 ? 'усе' : p}`);
    check(name, `період ${p}: обрано`, st.period === p);
  }
  await click('input[data-kind="other"]');
  await verify('усе + інші станції');
  await shot('4_all_period_other');
  await click('input[data-kind="other"]');
  await click('button[data-p="30"]');
  // 7. zoom buttons move the map and break nothing
  await click('#allOn');
  for (const b of D.battalions) {
    await click(`button[data-zoom="${b.key}"]`);
    await page.waitForTimeout(500);
    const from = await page.evaluate(() => window.__atlas.from());
    const pts = b.places.filter(p => Object.keys(p.days).some(d => d >= from)).map(p => [p.lat, p.lon])
      .concat((await page.evaluate(k => window.__atlas.zones()[k] || [], b.key)));
    const seen = await page.evaluate(p => window.__atlas.inView(p), pts);
    check(name, `наблизити ${b.key}: видно всі його села й зону (${seen} з ${pts.length})`, seen === pts.length && !report.errors.length,
      report.errors.slice(-2));
    if (vp.isMobile) check(name, `наблизити ${b.key}: на телефоні панель згорнулась`,
      await page.evaluate(() => document.getElementById('panel').classList.contains('closed')));
    if (b === D.battalions[1]) await shot('7_zoomed');
  }
  // 8. reference tab: open a card, «show only it»
  await openPanel();
  await page.click('#tRef');
  const k0 = D.battalions[0].key;
  await page.click(`.ref[data-b="${k0}"] .t`);
  check(name, 'довідник: картка розгортається', await page.evaluate(k => document.querySelector(`.ref[data-b="${k}"]`).classList.contains('open'), k0));
  await shot('5_reference');
  await page.click(`button[data-only="${k0}"]`);
  await page.waitForTimeout(300);
  st = await page.evaluate(() => window.__atlas.state());
  check(name, 'довідник: «показати лише його»', Object.values(st.on).filter(Boolean).length === 1 && st.on[k0]);
  await openPanel();
  await page.click('#tLayers');
  await verify('після довідника, вкладка шари');
  await shot('6_only_one');
  // 9. collapse / expand
  await page.click('#toggle');
  check(name, 'панель згортається', await page.evaluate(() => getComputedStyle(document.getElementById('body')).display === 'none'));
  await page.click('#toggle');
  check(name, 'панель розгортається', await page.evaluate(() => getComputedStyle(document.getElementById('body')).display !== 'none'));
  // 10. layout
  const lay = await page.evaluate(() => {
    const p = document.getElementById('panel').getBoundingClientRect();
    const small = [...document.querySelectorAll('#panel button, #panel input[type=checkbox]')]
      .filter(x => x.offsetParent).map(x => x.getBoundingClientRect()).filter(r => r.height < 18 || r.width < 18).length;
    return { overflowX: document.documentElement.scrollWidth > window.innerWidth, panelH: p.height / window.innerHeight,
             panelW: p.width / window.innerWidth, small };
  });
  check(name, 'немає горизонтальної прокрутки', !lay.overflowX);
  check(name, `панель не закриває мапу (${Math.round(lay.panelH * 100)}% висоти, ${Math.round(lay.panelW * 100)}% ширини)`,
    vp.isMobile ? lay.panelH <= 0.47 : lay.panelW <= 0.3);
  check(name, 'немає надто дрібних кнопок/галочок', lay.small === 0, lay.small);
  if (vp.isMobile) {
    const tap = await page.evaluate(() => [...document.querySelectorAll('#panel .bat, #panel button')].filter(x => x.offsetParent)
      .map(x => x.getBoundingClientRect().height).filter(h => h < 36).length);
    check(name, 'на телефоні рядки й кнопки ≥ 36 px під палець', tap === 0, tap);
  }
  await browser.close();
}

for (const [name, vp] of Object.entries(VIEWPORTS)) {
  try { await run(name, vp); } catch (e) { report.errors.push(`${name}: ${e.message.split('\n')[0]}`); }
}
const failed = report.checks.filter(c => !c.ok);
console.log(JSON.stringify({ file: report.file, zones30: report.zones, passed: report.checks.length - failed.length, failed: failed.length,
  failures: failed, errors: report.errors, shots: report.shots }, null, 1));
process.exit(failed.length || report.errors.length ? 1 : 0);
