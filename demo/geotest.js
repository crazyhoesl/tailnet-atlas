/* Replicates the layout maths from template.html and checks that no two nodes
   — or their labels — end up on top of each other, at several window sizes. */
const fs = require('fs');
const h = fs.readFileSync(process.argv[2], 'utf8');
const NODES = JSON.parse(h.match(/var NODES = (\[.*?\]), EDGES/s)[1]);
const SITES = JSON.parse(h.match(/SITES = (\[.*?\]);/s)[1]);

/* Placement is dynamic — a phone or travel router joins whichever site its
   current address belongs to. Cluster sizes therefore vary a lot between
   refreshes, so the layout has to hold for every plausible distribution, not
   just the one that happens to be live right now. `move` herds the movable
   devices into one site to build those worst cases, and the radii are
   recomputed from the resulting counts with the same formula render.py uses.

   Everything is derived from the rendered page, so this works against any
   config: containers stay put (they cannot leave their host), everything else
   is treated as able to move. */
const MOVABLE = NODES.filter(n => n.site !== 'dock').map(n => n.name);
const SITE_IDS = [...new Set(SITES.map(s => s.id))];

// mirrors ringPlan() in template.html — keep the two in step
function ringPlan(n) {
  if (n <= 7) return [{ n, r: 0.86 }];
  if (n <= 16) {
    const o = Math.max(3, Math.round(n * 0.65));
    return [{ n: o, r: 0.86 }, { n: n - o, r: 0.50 }];
  }
  const a = Math.round(n * 0.45), b = Math.round(n * 0.33);
  return [{ n: a, r: 0.86 }, { n: b, r: 0.60 }, { n: n - a - b, r: 0.32 }];
}

function place(W, H, move) {
  const nodes = NODES.map(n => ({
    ...n, site: move && MOVABLE.includes(n.name) ? move : n.site,
  }));
  const count = {};
  nodes.forEach(n => count[n.site] = (count[n.site] || 0) + 1);

  const M = Math.min(W, H), pad = M * 0.05;
  const sites = SITES
    .map(s => {
      const n = count[s.id] || 0;
      const fr = Math.min(0.28, Math.max(0.085, 0.075 + 0.024 * n));
      return { ...s, fr, n };
    })
    .filter(s => s.n > 0)   // render.py drops empty sites entirely
    .map(s => ({
      ...s, cx: pad + s.fx * (W - 2 * pad), cy: pad + s.fy * (H - 2 * pad), r: M * s.fr,
    }));

  // mirrors template.html: cap the area the regions may cover, then separate
  // and clamp. Only the wide-screen branch is modelled here — the narrow one
  // stacks the regions in a single column and is verified in the browser.
  const want = sites.reduce((a, s) => a + Math.PI * s.r * s.r, 0);
  const budget = W * H * 0.32;
  const fit = Math.min(1.55, Math.max(0.6, Math.sqrt(budget / Math.max(1, want))));
  const rMax = Math.min(W - 12, H - 72) / 2 - 4;
  sites.forEach(s => { s.r = Math.min(s.r * fit, rMax); });

  const GAP = 22, EDGE = 6, TOP = 66;
  for (let it = 0; it < 60; it++) {
    for (let i = 0; i < sites.length; i++)
      for (let j = i + 1; j < sites.length; j++) {
        const a = sites[i], b = sites[j];
        const dx = b.cx - a.cx, dy = b.cy - a.cy;
        const d = Math.hypot(dx, dy) || 0.01, need = a.r + b.r + GAP;
        if (d >= need) continue;
        const push = (need - d) / 2, ux = dx / d, uy = dy / d;
        a.cx -= ux * push; a.cy -= uy * push;
        b.cx += ux * push; b.cy += uy * push;
      }
    sites.forEach(s => {
      s.cx = Math.min(Math.max(s.cx, s.r + EDGE), W - s.r - EDGE);
      s.cy = Math.min(Math.max(s.cy, s.r + TOP), H - s.r - EDGE);
    });
  }

  const per = {};
  nodes.forEach(n => (per[n.site] = per[n.site] || []).push(n));
  const out = [];
  sites.forEach(s => {
    const w = { home: 0, server: 1, exit: 2, subnet: 2, proxy: 3, full: 4, client: 5 };
    const list = (per[s.id] || []).slice().sort(
      (a, b) => (w[a.role] - w[b.role]) || a.name.localeCompare(b.name));
    const n = list.length;
    const plan = ringPlan(n);
    let ring = 0, base = 0;
    list.forEach((nd, i) => {
      let x, y, lab = 1, r = 0;
      if (n === 1) { x = s.cx; y = s.cy; }
      else {
        while (i >= base + plan[ring].n) { base += plan[ring].n; ring++; }
        const idx = i - base, cnt = plan[ring].n;
        const rad = s.r * plan[ring].r;
        const start = cnt <= 3 ? 0 : -Math.PI / 2;
        const a = (idx / cnt) * Math.PI * 2 + start + (ring ? Math.PI / cnt : 0);
        x = s.cx + Math.cos(a) * rad;
        y = s.cy + Math.sin(a) * rad * 0.88;
        lab = (Math.sin(a) < -0.15 && Math.abs(Math.cos(a)) > 0.38) ? -1 : 1;
        r = ring;
      }
      out.push({ name: nd.name, site: s.id, x, y, lab, ring: r });
    });
    sites.forEach(() => {});
  });
  return { nodes: out, sites };
}

// the live distribution, plus "everything piled into one site" for each site
const SCENARIOS = [['live', null],
  ...SITE_IDS.map(id => ['all in ' + id, id])];

let problems = 0;
for (const [W, H] of [[1200, 680], [2000, 1157], [1440, 820], [900, 520]])
for (const [label, move] of SCENARIOS) {
  const { nodes, sites } = place(W, H, move);
  console.log(`\n=== ${W}x${H} · ${label} ` +
              `(${sites.map(s => s.id + '=' + s.n).join(' ')}) ===`);

  // 1) node distance
  let minD = 1e9, pair = '';
  for (let i = 0; i < nodes.length; i++)
    for (let j = i + 1; j < nodes.length; j++) {
      const d = Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y);
      if (d < minD) { minD = d; pair = `${nodes[i].name} / ${nodes[j].name}`; }
    }
  const okD = minD >= 34;
  if (!okD) problems++;
  console.log(`  closest nodes : ${minD.toFixed(0)}px  (${pair}) ${okD ? 'OK' : '<< TOO CLOSE'}`);

  // 2) label boxes (label sits centred ~13px below the dot).
  // 2.8 px per character per side ≈ the 10.5px sans the canvas draws with.
  // An earlier 3.1 overstated it by ~11% and reported overlaps of 1-2px that
  // do not exist on screen.
  const CHAR = 2.8;
  const boxes = nodes.map(n => ({
    name: n.name,
    x0: n.x - n.name.length * CHAR, x1: n.x + n.name.length * CHAR,
    y0: n.lab < 0 ? n.y - 22 : n.y + 7, y1: n.lab < 0 ? n.y - 9 : n.y + 20,
  }));
  const hits = [];
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i], b = boxes[j];
      if (a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1)
        hits.push(`${a.name}@(${nodes[i].x | 0},${nodes[i].y | 0},ring${nodes[i].ring}` +
                  `,lab${nodes[i].lab}) / ${b.name}@(${nodes[j].x | 0},${nodes[j].y | 0}` +
                  `,ring${nodes[j].ring},lab${nodes[j].lab})`);
    }
  if (hits.length) problems++;
  console.log(`  label overlaps: ${hits.length ? hits.join(', ') : 'none'}`);

  // 3) stay inside the canvas
  const out = nodes.filter(n => n.x < 12 || n.x > W - 12 || n.y < 12 || n.y > H - 26);
  const ringOut = sites.filter(s => s.cy + s.r > H - 4 || s.cy - s.r < 4 ||
                                    s.cx - s.r < 4 || s.cx + s.r > W - 4);
  if (out.length || ringOut.length) problems++;
  console.log(`  outside frame : nodes=${out.map(n => n.name).join(',') || 'none'}` +
              `  rings=${ringOut.map(s => s.id).join(',') || 'none'}`);

  // 4) site circles must not intersect
  const ov = [];
  for (let i = 0; i < sites.length; i++)
    for (let j = i + 1; j < sites.length; j++) {
      const d = Math.hypot(sites[i].cx - sites[j].cx, sites[i].cy - sites[j].cy);
      if (d < sites[i].r + sites[j].r)
        ov.push(`${sites[i].id}/${sites[j].id} by ${Math.round(sites[i].r + sites[j].r - d)}px`);
    }
  if (ov.length) problems++;
  console.log(`  ring overlaps : ${ov.length ? ov.join(', ') : 'none'}`);
}
console.log(problems ? `\n${problems} problem(s)` : '\nall clear');
process.exit(problems ? 1 : 0);
