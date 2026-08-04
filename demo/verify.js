/* Guards the inter-frame diffing in record.js.

     DUMP=1 node record.js && node verify.js

   Decodes the produced GIF with disposal applied — exactly how a viewer plays
   it back — and compares chosen frames against the PNGs the recorder dumped.
   If the transparent index or the disposal method were wrong, decoded frames
   would carry stale pixels from earlier ones and nothing else would notice. */
const fs = require('fs'), path = require('path');
const { GifReader } = require('omggif');
const { PNG } = require('pngjs');

const OUT = path.join(__dirname, 'out');
const gif = new GifReader(fs.readFileSync(path.join(OUT, 'atlas-demo.gif')));
const w = gif.width, h = gif.height;
console.log('gif:', w + 'x' + h, gif.numFrames(), 'frames');

const canvas = new Uint8Array(w * h * 4);
const checks = {};
for (let i = 0; i < gif.numFrames(); i++) {
  gif.decodeAndBlitFrameRGBA(i, canvas);
  if ((i + 1) % 25 === 0) checks[i + 1] = canvas.slice();
}

let worst = 0, compared = 0;
for (const n of Object.keys(checks)) {
  const f = path.join(OUT, 'frame-' + n + '.png');
  if (!fs.existsSync(f)) continue;
  const png = PNG.sync.read(fs.readFileSync(f));
  if (png.width !== w || png.height !== h) {
    console.log(n, 'size mismatch', png.width + 'x' + png.height, 'vs', w + 'x' + h);
    continue;
  }
  compared++;
  const a = checks[n], b = png.data;
  let sum = 0, big = 0;
  for (let p = 0; p < w * h; p++) {
    for (let c = 0; c < 3; c++) {
      const d = Math.abs(a[p * 4 + c] - b[p * 4 + c]);
      sum += d; if (d > 60) big++;
    }
  }
  const mad = sum / (w * h * 3), pct = (100 * big) / (w * h * 3);
  worst = Math.max(worst, mad);
  console.log(`frame ${n}: mean abs diff ${mad.toFixed(2)}  ·  ${pct.toFixed(3)}% channels off by >60`);
}

// A run that compared nothing must not report success — the first version of
// this script did exactly that and looked green.
const ok = compared >= 3 && worst < 12;
console.log(ok ? `\nOK — ${compared} decoded frames match the source`
               : `\nFAILED — compared ${compared} frames, worst mean abs diff ${worst.toFixed(2)}`);
process.exit(ok ? 0 : 1);
