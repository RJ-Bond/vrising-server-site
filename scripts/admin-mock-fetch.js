// Mock-auth shim for screenshotting frontend/admin.html WITHOUT a running backend.
// Injected by scripts/preview-admin.sh as the very FIRST <script> in a throwaway copy
// of admin.html — runs before common.js / the page's own inline script, so:
//   1. localStorage already has a fake admin "user" by the time admin.html's own
//      auth-gate IIFE checks it synchronously (real admin.html: it redirects to
//      /login.html immediately if this is missing/not role:'admin' — no fetch involved).
//   2. window.fetch is patched before any real fetch() call fires, so known admin/API
//      endpoints resolve with canned JSON instead of 404ing against the static server.
// Never committed as part of admin.html itself — this is a dev-only test harness.
(function () {
  // Role is mockable via ?mockRole=moderator|admin|superadmin on the page URL (the
  // outer iframe's src, since this shim reads the iframe document's own location) —
  // defaults to 'admin' for backward-compat with existing preview-admin.sh calls.
  const _mockRole = new URLSearchParams(location.search).get('mockRole') || 'admin';
  localStorage.setItem('user', JSON.stringify({ id: 1, username: 'RJ Bond', role: _mockRole, email: 'admin@example.com' }));
  localStorage.setItem('token', 'mock-token');

  const now = Date.now();
  const iso = (msAgo) => new Date(now - msAgo).toISOString();

  const settingsPublic = {
    site_title: 'Just-Skill.Ru',
    site_tagline: 'Игровое сообщество',
    site_logo_url: '/icon-vrising.png',
    favicon_url: '/icon-vrising.png',
    bg_image_url: '',
    timezone: 'Europe/Moscow',
    time_format: '24h',
    date_format: 'dd.mm.yyyy',
    maintenance_mode: 'false',
    server_name: '[RU] Just-Skill.Ru | Standart PvE',
    server2_name: '[RU] Just-Skill.Ru | Brutal PvE',
    discord_url: '',
  };

  const adminSettingsList = Object.entries({
    ...settingsPublic,
    server_ip: '127.0.0.1', server_port: '27016',
    server2_ip: '127.0.0.1', server2_port: '27017',
  }).map(([key, value]) => ({ key, value: String(value) }));

  // UserOut shape (backend/schemas.py) — auth/me and admin/users both return this.
  const userOut = (i, username, role) => ({
    id: i, username, email: `${username.toLowerCase().replace(/\s+/g, '')}@example.com`,
    role, is_active: true, created_at: iso(i * 36 * 3600 * 1000), avatar_url: null,
    cover_url: null, rules_accepted_at: iso(i * 36 * 3600 * 1000), game_nickname: null,
    admin_title: role === 'admin' ? 'Основатель' : null, last_active_at: iso(600000),
    badge_icon_url: null, badge_style: 'default', totp_enabled: false, bio: null,
  });
  const fakeUsers = [
    userOut(1, 'RJ Bond', 'admin'),
    ...['buhalovna', 'Shadowfang', 'Dracarys', 'Vortigern', 'Nightshade', 'Emberclaw', 'Grimwald'].map((n, i) => userOut(i + 2, n, 'user')),
  ];

  // /api/monitor/status(2) shape (backend/main.py) — plain dict, not a Pydantic model.
  const monitorStatus = (name, players, ip, port) => ({
    online: true, name, players, max_players: 40, version: '1.0', map: 'Farbane Woods', vac: true,
    players_list: Array.from({ length: players }, (_, i) => ({ name: `Player${i}`, score: 0, duration: 3600 + i * 300 })),
    latency_ms: 42, ip, game_port: port,
  });

  const snapshots = (n) => Array.from({ length: 24 }, (_, i) => ({
    ts: Math.floor((now - (24 - i) * 3600 * 1000) / 1000),
    players: Math.max(0, Math.round(n + Math.sin(i / 3) * n * 0.6)),
    online: true, latency_ms: 40 + Math.round(Math.random() * 20),
  }));

  const routes = [
    [/\/api\/auth\/me$/, () => userOut(1, 'RJ Bond', _mockRole)],
    [/\/api\/settings\/public$/, () => settingsPublic],
    [/\/api\/admin\/stats$/, () => ({
      user_count: fakeUsers.length, news_count: 37, comment_count: 214, file_count: 58,
      recent_comments: [
        { id: 1, author: 'buhalovna', content: 'Когда вайп?', news_slug: 'news-1', news_title: 'Обновление сервера', created_at: iso(3600000) },
        { id: 2, author: 'Shadowfang', content: 'Отличное событие!', news_slug: 'news-2', news_title: 'Хэллоуин ивент', created_at: iso(7200000) },
      ],
    })],
    [/\/api\/admin\/password-resets$/, () => []],
    [/\/api\/admin\/users$/, () => fakeUsers],
    [/\/api\/admin\/settings$/, () => adminSettingsList.map(s => ({ ...s, updated_at: iso(0) }))],
    [/\/api\/monitor\/status2/, () => ({ enabled: true, ...monitorStatus('[RU] Just-Skill.Ru | Brutal PvE', 0, '127.0.0.1', 27017) })],
    [/\/api\/monitor\/status$/, () => monitorStatus('[RU] Just-Skill.Ru | Standart PvE', 10, '127.0.0.1', 27016)],
    [/\/api\/monitor\/snapshots/, (url) => snapshots(url.includes('server=2') ? 3 : 10)],
    [/\/api\/wipes$/, () => ([
      { id: 1, server_num: 1, wipe_type: 'full', wipe_date: iso(-56 * 24 * 3600 * 1000), note: null, created_at: iso(30 * 24 * 3600 * 1000) },
      { id: 2, server_num: 2, wipe_type: 'map', wipe_date: iso(-56 * 24 * 3600 * 1000), note: null, created_at: iso(30 * 24 * 3600 * 1000) },
    ])],
  ];

  const realFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    for (const [pattern, respond] of routes) {
      if (pattern.test(url)) {
        const body = respond(url);
        return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      }
    }
    // Unmocked /api/* calls: return 404 (matches the real static preview server) so
    // admin.html's existing r.ok-guarded fallbacks kick in instead of throwing.
    if (url.includes('/api/')) {
      return Promise.resolve(new Response('not mocked', { status: 404 }));
    }
    return realFetch(input, init);
  };
})();
