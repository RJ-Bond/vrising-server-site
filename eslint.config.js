// ESLint flat config — pure linter, NOT a build step. frontend/ still has
// no bundler and is served as-is by nginx (see CLAUDE.md); this only checks
// the standalone frontend/*.js files for real bugs (typos, undefined
// variables, unreachable code) before they ship.
//
// Run: npx eslint frontend   (or: bash scripts/lint_frontend.sh)

const js = require('@eslint/js');
const globals = require('globals');

// frontend/common.js loads first on every page (plain <script src>, no
// modules/bundler) and defines these as real top-level `function`/`const`
// declarations that the other standalone files (and every page's inline
// scripts) rely on as ambient globals. Declared here so no-undef doesn't
// false-positive across file boundaries — but only for files OTHER than
// common.js itself, where they're already declared (applying it there too
// would trip no-redeclare: "already defined as a built-in global variable").
const commonJsDeclaredGlobals = {
  esc: 'readonly',
  getUser: 'readonly',
  nameGradient: 'readonly',
  fmtDate: 'readonly',
  fmtDateTime: 'readonly',
  showToast: 'readonly',
  STAFF_ROLES: 'readonly',
  _dateOpts: 'readonly',
  _toDate: 'readonly',
  _renderAdminBadge: 'readonly',
  _statusInfo: 'readonly',
};

// These are only ever created via `window.foo = function(){...}` (property
// assignment, not a `function`/`const` declaration ESLint can see), so bare
// calls to them need a global declaration in EVERY file, including the file
// that assigns them (common.js itself calls closeGlobalSearch()/
// openGlobalSearch() bare from its own keydown handler, before/without a
// matching identifier declaration in scope).
const windowAssignedGlobals = {
  getSettings: 'readonly',
  openGlobalSearch: 'readonly',
  closeGlobalSearch: 'readonly',
  // frontend/charts.js / frontend/anim.js — shared widgets used across pages.
  renderPlayerChart: 'readonly',
  animateEntrance: 'readonly',
  // Vendored third-party globals (frontend/*.min.js, loaded via <script src>,
  // not npm packages — see CLAUDE.md "no build step").
  DOMPurify: 'readonly',
  Chart: 'readonly',
  anime: 'readonly',
  Alpine: 'readonly',
  Quill: 'readonly',
  Sentry: 'readonly',
};

module.exports = [
  {
    // Global ignores. NOTE: this must be its own config object with ONLY an
    // `ignores` key — putting `ignores` alongside `files` in a config block
    // only narrows *that block*, it does not exclude the files from the rest
    // of the array (they'd still get linted by js.configs.recommended below).
    ignores: [
      // Vendored/minified third-party libraries — not our code to lint.
      'frontend/alpine.min.js',
      'frontend/anime.min.js',
      'frontend/chart.min.js',
      'frontend/purify.min.js',
      'frontend/quill.min.js',
      'frontend/sentry.min.js',
    ],
  },
  js.configs.recommended,
  {
    files: ['frontend/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'script',
      globals: {
        ...globals.browser,
        ...windowAssignedGlobals,
      },
    },
    rules: {
      // Conservative, high-signal set: catch real bugs, not style. No
      // stylistic rules (quotes/semi/indent/etc) — this would otherwise
      // produce a giant reformatting diff on existing code for zero benefit.
      //
      // vars: 'local' — this codebase's pages expose many top-level
      // functions purely as onclick="foo()" handler targets for HTML the
      // page renders (in index.html's inline tail script and via innerHTML
      // elsewhere) or for other standalone files to call; ESLint can't see
      // those call sites since they're outside the file (or inside string
      // literals), so checking module/global-scope declarations for "unused"
      // would flood the output with false positives. Locals (inside function
      // bodies) are still fully checked — that's where a real unused-var
      // typo would show up.
      'no-unused-vars': ['warn', { vars: 'local', args: 'none', caughtErrors: 'none' }],
      'no-undef': 'error',
      // This codebase deliberately uses `catch {}` to silently swallow
      // errors in many best-effort paths (settings load, maintenance
      // banner, etc. — see frontend/common.js) — not a bug, so don't flag it.
      'no-empty': ['error', { allowEmptyCatch: true }],
    },
  },
  {
    // Every standalone file except common.js may call common.js's helpers
    // as bare identifiers (see commonJsDeclaredGlobals comment above).
    files: ['frontend/**/*.js'],
    ignores: ['frontend/common.js'],
    languageOptions: {
      globals: { ...commonJsDeclaredGlobals },
    },
  },
  {
    // Service worker runs in its own global scope (self/caches/clients, no window/document).
    files: ['frontend/sw.js'],
    languageOptions: {
      globals: { ...globals.serviceworker },
    },
  },
];
