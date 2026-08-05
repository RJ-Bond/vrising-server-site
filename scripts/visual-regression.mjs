#!/usr/bin/env node
// Visual-regression check for CI (Linux-only mechanism — this is deliberately
// NOT a port of scripts/preview.sh / scripts/preview-mock.sh, which hardcode
// a Windows Chrome path + cygpath and only run under Git Bash on Windows; see
// CLAUDE.md's "Tooling (scripts/)" section for those. This script uses
// Playwright (headless Chromium it manages itself, works on any OS/CI runner)
// plus pixelmatch for the pixel diff. It reuses the *concept* of
// scripts/public-mock-fetch.js (anonymous-visitor canned API responses) via
// Playwright's page.addInitScript(), instead of the bash script's approach of
// physically inlining a <script> tag into a throwaway copy of the HTML file.
//
// Usage:
//   node scripts/visual-regression.mjs            # compare against committed baselines, exit 1 on regression
//   node scripts/visual-regression.mjs --update    # (re)generate the baseline PNGs and exit 0
//
// npm shortcuts: `npm run visual-regression` / `npm run visual-regression:update`
//
// --- Updating a baseline after an intentional visual change -----------------
// 1. Make your CSS/HTML change.
// 2. Run `npm ci && npx playwright install --with-deps chromium` once if you
//    haven't already (same as the CI job does).
// 3. Run `npm run visual-regression:update` — this overwrites the PNGs under
//    .github/visual-baselines/ with fresh screenshots of your changed pages.
// 4. Look at the diff (`git diff --stat .github/visual-baselines/`) or open
//    the new PNGs to confirm the change is the one you intended, nothing else
//    moved.
// 5. Commit the updated PNGs alongside your code change.
//
// --- Known risk with the very first baseline set ----------------------------
// The PNGs initially committed to .github/visual-baselines/ were generated on
// a Windows dev machine's native Chromium (this repo's other preview tooling
// is Windows-only too — see CLAUDE.md — and a sandboxed Docker daemon wasn't
// available to render on Linux instead). Page fonts (Cinzel/Inter/Oswald/…)
// are loaded from Google Fonts at render time, not vendored, so the actual
// glyph *files* are identical across OSes — but Chromium's glyph
// *rasterizer* (hinting/anti-aliasing) still differs meaningfully between
// Windows and Linux for the same font file, which can show up as a diffuse,
// low-magnitude pixel difference across all text. If the very first real CI
// run fails and the uploaded diff PNGs (visual-regression-diffs artifact)
// show exactly that pattern (text-shaped noise, not a real layout shift),
// don't chase it as a bug — instead run the "Update visual-regression
// baselines (manual)" workflow_dispatch job in ci.yml, which regenerates the
// baselines FROM an ubuntu-latest runner (the same environment every future
// CI run compares against), download the resulting artifact, and commit
// those PNGs over the Windows-generated ones.

import { chromium } from 'playwright';
import pixelmatch from 'pixelmatch';
import { PNG } from 'pngjs';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const FRONTEND_DIR = path.join(ROOT, 'frontend');
const BASELINE_DIR = path.join(ROOT, '.github', 'visual-baselines');
const OUT_DIR = path.join(ROOT, '.visual-regression-out'); // gitignored scratch dir — actual/diff PNGs for a failed run

const UPDATE = process.argv.includes('--update');

// Diff tolerance. 0.5% of pixels differing is generous enough to absorb
// font-hinting/sub-pixel anti-aliasing noise between runs on the *same*
// platform, but tight enough to catch a real layout regression (an
// overlapping element or overflow typically moves a much larger fraction of
// the page than that). Tune here if it proves too noisy or too loose.
const DIFF_RATIO_THRESHOLD = 0.005;
// Per-pixel color-distance sensitivity for pixelmatch itself (0..1, pixelmatch
// default is 0.1). Left at the default — pixelmatch already does its own
// anti-aliased-edge detection (excludes them unless includeAA is set).
const PIXELMATCH_THRESHOLD = 0.1;

// Representative pages: index.html (marketing/landing, heaviest page), plus
// two data-driven pages whose layouts are exactly what has broken before per
// CLAUDE.md's incident history (compact-mode nav needed 5 sequential
// screenshot-driven fixes) — leaderboard.html (dense table/podium layout)
// and clans.html (card grid with variable-length content, avatar stacks).
const PAGES = ['index.html', 'leaderboard.html', 'clans.html'];

// Desktop ~1280 and mobile ~390 per CLAUDE.md's note that mobile must render
// inside a genuinely narrow viewport (Playwright's `viewport` does this
// correctly out of the box — no desktop-Chrome-resized-window pitfall like
// the one CLAUDE.md warns about for the old --window-size approach).
const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 900 },
  { name: 'mobile', width: 390, height: 844 },
];

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css',
  '.js': 'application/javascript',
  '.json': 'application/json',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.webp': 'image/webp',
  '.woff2': 'font/woff2',
  '.gif': 'image/gif',
};

function startStaticServer() {
  const server = http.createServer((req, res) => {
    let rel = decodeURIComponent((req.url || '/').split('?')[0]);
    if (rel === '/') rel = '/index.html';
    const file = path.join(FRONTEND_DIR, rel);
    // Guard against escaping frontend/ via ../ in the URL.
    if (!file.startsWith(FRONTEND_DIR)) {
      res.writeHead(403);
      res.end();
      return;
    }
    fs.readFile(file, (err, data) => {
      if (err) {
        res.writeHead(404);
        res.end('not found');
        return;
      }
      const ext = path.extname(file).toLowerCase();
      res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
      res.end(data);
    });
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

// Forces every CSS animation/transition to complete instantly. This does NOT
// touch anime.js-driven tweens (frontend/anim.js sets inline styles via rAF,
// not CSS animations) — those are handled below by simply waiting out their
// (short, known) duration after load instead of trying to freeze a JS tween
// mid-flight, which would be far more fragile.
const FREEZE_CSS = `
  <style>
    *, *::before, *::after {
      animation-duration: 0s !important;
      animation-delay: 0s !important;
      transition-duration: 0s !important;
      transition-delay: 0s !important;
      scroll-behavior: auto !important;
    }
  </style>
`;

async function run() {
  const server = await startStaticServer();
  const port = server.address().port;
  const mockScript = fs.readFileSync(path.join(ROOT, 'scripts', 'public-mock-fetch.js'), 'utf8');

  fs.mkdirSync(BASELINE_DIR, { recursive: true });
  if (!UPDATE) {
    fs.rmSync(OUT_DIR, { recursive: true, force: true });
    fs.mkdirSync(OUT_DIR, { recursive: true });
  }

  const browser = await chromium.launch();
  const results = [];

  try {
    for (const pageFile of PAGES) {
      for (const viewport of VIEWPORTS) {
        const name = `${path.basename(pageFile, '.html')}_${viewport.name}`;
        const context = await browser.newContext({
          viewport: { width: viewport.width, height: viewport.height },
          deviceScaleFactor: 1,
        });
        const page = await context.newPage();
        // Mock fetch before any page script runs (see public-mock-fetch.js).
        await page.addInitScript({ content: mockScript });
        // Freeze CSS animations/transitions before first paint.
        await page.addInitScript({ content: `document.addEventListener('DOMContentLoaded', () => { const s = document.createElement('style'); s.textContent = ${JSON.stringify(FREEZE_CSS.replace(/<\/?style>/g, ''))}; document.head.appendChild(s); });` });

        await page.goto(`http://127.0.0.1:${port}/${pageFile}`, { waitUntil: 'networkidle' });
        // anime.js entrance tweens (frontend/anim.js) run 380ms + a small
        // per-item stagger on top — this flat wait is the same idea as the
        // old bash tooling's --virtual-time-budget=4000, just shorter since
        // we don't need to boot a whole new Chrome process per shot.
        await page.waitForTimeout(900);

        const buffer = await page.screenshot({ fullPage: true });
        await context.close();

        const outPath = path.join(UPDATE ? BASELINE_DIR : OUT_DIR, `${name}.png`);
        if (UPDATE) {
          fs.writeFileSync(outPath, buffer);
          results.push({ name, status: 'updated' });
          console.log(`[update] ${name} -> ${path.relative(ROOT, outPath)}`);
          continue;
        }

        fs.writeFileSync(outPath, buffer);
        const baselinePath = path.join(BASELINE_DIR, `${name}.png`);
        if (!fs.existsSync(baselinePath)) {
          results.push({ name, status: 'missing-baseline' });
          continue;
        }

        const actual = PNG.sync.read(buffer);
        const baseline = PNG.sync.read(fs.readFileSync(baselinePath));
        if (actual.width !== baseline.width || actual.height !== baseline.height) {
          results.push({
            name,
            status: 'size-mismatch',
            detail: `baseline ${baseline.width}x${baseline.height} vs actual ${actual.width}x${actual.height}`,
          });
          continue;
        }

        const { width, height } = actual;
        const diff = new PNG({ width, height });
        const mismatched = pixelmatch(baseline.data, actual.data, diff.data, width, height, {
          threshold: PIXELMATCH_THRESHOLD,
        });
        const ratio = mismatched / (width * height);
        if (ratio > DIFF_RATIO_THRESHOLD) {
          const diffPath = path.join(OUT_DIR, `${name}.diff.png`);
          fs.writeFileSync(diffPath, PNG.sync.write(diff));
          results.push({ name, status: 'fail', detail: `${(ratio * 100).toFixed(3)}% pixels differ (threshold ${(DIFF_RATIO_THRESHOLD * 100).toFixed(2)}%)` });
        } else {
          results.push({ name, status: 'pass', detail: `${(ratio * 100).toFixed(3)}% pixels differ` });
        }
      }
    }
  } finally {
    await browser.close();
    server.close();
  }

  console.log('');
  console.log('Visual regression results:');
  let hasFailure = false;
  for (const r of results) {
    console.log(`  ${r.status.padEnd(16)} ${r.name}${r.detail ? '  (' + r.detail + ')' : ''}`);
    if (!UPDATE && (r.status === 'fail' || r.status === 'missing-baseline' || r.status === 'size-mismatch')) {
      hasFailure = true;
    }
  }
  console.log('');

  if (UPDATE) {
    console.log(`Baselines written to ${path.relative(ROOT, BASELINE_DIR)}/ — review and commit them.`);
    return;
  }

  if (hasFailure) {
    console.log(`FAILED. Actual/diff PNGs saved under ${path.relative(ROOT, OUT_DIR)}/ for inspection (gitignored, not committed).`);
    console.log(`If this is an intentional visual change, run: npm run visual-regression:update`);
    process.exitCode = 1;
  } else {
    console.log('OK — all pages within threshold.');
  }
}

run().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
