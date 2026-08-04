/* Records the demo Atlas as a GIF: drives a real cursor across the map so the
   hover tooltips, the pin and the role filter are all visible.

     python3 demo/make_demo.py
     cd demo && npm install && node record.js   # -> out/atlas-demo.gif

   Three things are less obvious than they look:

   1. The page animates on requestAnimationFrame, i.e. wall-clock time.
      Screenshotting is far slower than that, so instead of racing it we own
      the clock: rAF is replaced by a queue the recorder steps forward by
      exactly 1/FPS per captured frame. Playback then runs at true speed
      instead of stuttering.

   2. Node positions live inside the page's closure and drift constantly
      (nodes breathe, regions float). Rather than adding a test hook to the
      production template, the recorder finds nodes the way a user does: it
      moves the pointer and reads the tooltip. A coarse sweep builds the map
      once, a small local sweep re-acquires the exact spot before each step.

   3. ~96% of pixels are identical between frames, so every frame after the
      first stores only what changed and the rest shows through as transparent.
      That is the difference between a 22 MB file and a 7 MB one — and it is
      also the part most likely to break silently, hence verify.js. */
const fs = require('fs');
const http = require('http');
const path = require('path');
const puppeteer = require('puppeteer');
const { PNG } = require('pngjs');
const { GIFEncoder, quantize, applyPalette } = require('gifenc');

const W = 900, H = 900, FPS = 12, STEP = Math.round(1000 / FPS);
const COLORS = 128;
const PORT = 8911;
const OUT = path.join(__dirname, 'out');

const TOUR = [
  { hold: 10 },
  { at: 'hub', hold: 20 },
  { at: 'theo-iphone', hold: 18 },
  { at: 'atlas', hold: 18 },
  { at: 'van-router', hold: 20 },
  { at: 'office-nuc', hold: 18 },
  { click: 'thinkpad-x1', hold: 26 },
  { unpin: true, hold: 6 },
  { legend: 'server', hold: 18 },
  { legend: 'server', hold: 12 },
  { away: true, hold: 12 },
];

const sleep = ms => new Promise(r => setTimeout(r, ms));

const serve = () => new Promise(res => {
  // A file:// load makes the live poll fail and paints a red error badge over
  // the map. Serving the generated state alongside the page shows what a real
  // viewer sees: a green "live" indicator.
  const srv = http.createServer((req, rq) => {
    const live = req.url.startsWith('/api/state');
    const body = fs.readFileSync(path.join(OUT, live ? 'state.json' : 'index.html'));
    rq.writeHead(200, {
      'content-type': (live ? 'application/json' : 'text/html') + '; charset=utf-8',
      'cache-control': 'no-store',
    });
    rq.end(body);
  });
  srv.listen(PORT, '127.0.0.1', () => res(srv));
});

(async () => {
  const srv = await serve();
  const browser = await puppeteer.launch({
    headless: true,
    args: ['--force-device-scale-factor=1', '--hide-scrollbars'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: W, height: H, deviceScaleFactor: 1 });
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'dark' }]);

  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(document, 'hidden', { get: () => false });
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible' });
    window.__q = []; window.__t = 0;
    window.requestAnimationFrame = cb => { window.__q.push(cb); return 1; };
    window.__advance = ms => {
      window.__t += ms;
      const q = window.__q; window.__q = [];
      q.forEach(cb => cb(window.__t));
    };
  });

  await page.goto('http://127.0.0.1:' + PORT + '/', { waitUntil: 'load' });
  await sleep(400);
  // let the layout settle so nodes are at rest when recording starts
  await page.evaluate(() => { for (let i = 0; i < 300; i++) window.__advance(16); });

  const rect = await page.evaluate(() => {
    const r = document.getElementById('stage').getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height };
  });

  // Crop to what matters: headline, counters, map, legend.
  const clip = await page.evaluate(() => {
    const top = document.querySelector('.brand').getBoundingClientRect();
    const leg = document.getElementById('legend').getBoundingClientRect();
    const pad = 14;
    return {
      x: 0, y: Math.max(0, Math.round(top.y - pad)),
      width: Math.round(document.querySelector('.wrap').getBoundingClientRect().width),
      height: Math.round(leg.bottom - top.y + pad * 2),
    };
  });

  // probe(points) -> name under each point, using the page's own hit testing
  const probe = (pts) => page.evaluate((pts) => {
    const stage = document.getElementById('stage');
    const tip = document.getElementById('tip');
    const r = stage.getBoundingClientRect();
    const out = [];
    for (const p of pts) {
      stage.dispatchEvent(new MouseEvent('mousemove', {
        clientX: r.x + p.x, clientY: r.y + p.y, bubbles: true,
      }));
      const h = tip.querySelector('h4');
      out.push(tip.classList.contains('on') && h
        ? h.textContent.replace(/offline/i, '').trim() : null);
    }
    stage.dispatchEvent(new MouseEvent('mouseleave', { bubbles: true }));
    return out;
  }, pts);

  const grid = (x0, y0, x1, y1, s) => {
    const pts = [];
    for (let y = y0; y <= y1; y += s) for (let x = x0; x <= x1; x += s) pts.push({ x, y });
    return pts;
  };

  const sweep = grid(6, 6, rect.w - 6, rect.h - 6, 6);
  const hits = await probe(sweep);
  const hint = {};
  hits.forEach((n, i) => { if (n && !hint[n]) hint[n] = sweep[i]; });
  console.log('located', Object.keys(hint).length, 'nodes');

  const locate = async (name) => {
    const h = hint[name];
    if (!h) return null;
    const pts = grid(Math.max(4, h.x - 26), Math.max(4, h.y - 26),
                     Math.min(rect.w - 4, h.x + 26), Math.min(rect.h - 4, h.y + 26), 3);
    const got = await probe(pts);
    const found = pts.filter((_, i) => got[i] === name);
    if (!found.length) return { x: rect.x + h.x, y: rect.y + h.y };
    const c = found.reduce((a, p) => ({ x: a.x + p.x, y: a.y + p.y }), { x: 0, y: 0 });
    return { x: rect.x + c.x / found.length, y: rect.y + c.y / found.length };
  };

  const frames = [];
  const shoot = async () => {
    await page.evaluate(ms => window.__advance(ms), STEP);
    const shot = Buffer.from(await page.screenshot({ type: 'png', clip }));
    frames.push(shot);
    if (process.env.DUMP && frames.length % 25 === 0)
      fs.writeFileSync(path.join(OUT, 'frame-' + frames.length + '.png'), shot);
  };

  let cur = { x: rect.x + rect.w * 0.5, y: rect.y + rect.h * 0.95 };
  const glideTo = async (target, steps) => {
    for (let i = 1; i <= steps; i++) {
      const t = i / steps, e = t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      await page.mouse.move(cur.x + (target.x - cur.x) * e, cur.y + (target.y - cur.y) * e);
      await shoot();
    }
    cur = target;
    await page.mouse.move(target.x, target.y);
  };

  for (const step of TOUR) {
    const name = step.at || step.click;
    if (name) {
      const p = await locate(name);
      if (!p) { console.warn('not found:', name); continue; }
      await glideTo(p, 9);
      if (step.click) await page.mouse.click(p.x, p.y);
    }
    if (step.unpin) await page.mouse.click(rect.x + 24, rect.y + 40);
    if (step.legend) {
      const box = await page.evaluate(role => {
        const el = [...document.querySelectorAll('.lg')]
          .find(e => e.textContent.trim().startsWith(role));
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      }, step.legend);
      if (box) { await glideTo(box, 7); await page.mouse.click(box.x, box.y); }
    }
    if (step.away) await glideTo({ x: rect.x + rect.w * .5, y: rect.y + rect.h - 8 }, 7);
    for (let i = 0; i < (step.hold || 10); i++) await shoot();
  }

  await browser.close();
  srv.close();
  console.log('frames:', frames.length);

  // One palette for the whole clip (stable colours, smaller file), and every
  // frame after the first stores only what changed.
  const enc = GIFEncoder();
  const first = PNG.sync.read(frames[0]);
  const asRGBA = png => new Uint8ClampedArray(png.data.buffer, png.data.byteOffset, png.data.length);
  const palette = quantize(asRGBA(first), COLORS - 1, { format: 'rgb565' });
  const TI = palette.length;            // reserved index meaning "unchanged"
  palette.push([0, 0, 0]);

  let prev = null, kept = 0, total = 0;
  frames.forEach((buf, i) => {
    const png = PNG.sync.read(buf);
    const idx = applyPalette(asRGBA(png), palette, 'rgb565');
    if (prev) {
      for (let p = 0; p < idx.length; p++) if (idx[p] === prev[p]) { idx[p] = TI; kept++; }
      total += idx.length;
    }
    const next = prev ? prev.slice() : idx.slice();
    if (prev) for (let p = 0; p < idx.length; p++) if (idx[p] !== TI) next[p] = idx[p];
    prev = next;
    enc.writeFrame(idx, png.width, png.height, {
      palette: i === 0 ? palette : undefined,
      delay: STEP, transparent: i > 0, transparentIndex: TI, dispose: 1,
    });
  });
  enc.finish();
  const gif = Buffer.from(enc.bytes());
  fs.writeFileSync(path.join(OUT, 'atlas-demo.gif'), gif);
  console.log('unchanged pixels skipped:', (100 * kept / Math.max(1, total)).toFixed(1) + '%');
  console.log('gif:', (gif.length / 1e6).toFixed(2), 'MB ·',
              (frames.length / FPS).toFixed(1), 's');
})();
