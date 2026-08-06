// Mock-data shim for screenshotting PUBLIC pages (clans/events/leaderboard/servers/...)
// with realistic content instead of the loading/empty/error states plain preview.sh
// shows (no backend running). Injected by scripts/preview-mock.sh as the first
// <script> — runs before common.js / the page's own inline script, so window.fetch
// is patched before any real fetch() call fires. Anonymous visitor (no session).
// Never committed as part of any real page — dev-only test harness.
(function () {
  const now = Date.now();
  const iso = (msAgo) => new Date(now - msAgo).toISOString();

  const settingsPublic = {
    site_title: 'Just-Skill.Ru', site_tagline: 'Игровое сообщество',
    site_logo_url: '', favicon_url: '/icon-vrising.png', bg_image_url: '',
    timezone: 'Europe/Moscow', time_format: '24h', date_format: 'dd.mm.yyyy',
    maintenance_mode: 'false',
    server_name: '[RU] Just-Skill.Ru | Standart PvE',
    server2_name: '[RU] Just-Skill.Ru | Brutal PvE',
    wipe_date: iso(-56 * 24 * 3600 * 1000), wipe_type: 'full',
    wipe_date2: iso(-56 * 24 * 3600 * 1000), wipe_type2: 'map',
    discord_url: 'https://discord.gg/example',
    event_active: 'false', rules: '1. Уважайте других игроков.\n2. Без читов.',
    nav_hidden: '["/shop.html"]',
  };

  // member_preview mirrors GET /api/clans's shape (GameClanMemberOut in backend/schemas.py)
  // — up to 4 members, leaders/officers first, feeding clans.html's card avatar-stack.
  // is_online mixed true/false so the presence-dot screenshot shows both states.
  // last_connected_unix/physical_power/spell_power are part of the real response shape
  // but not displayed yet (experimental / needs eyeballing first) — included here only
  // to mirror the backend's exact fields, not because the frontend reads them.
  const memberPreview = (n) => [
    { steam_id: '1', character_name: 'Vortigern', role: 'leader', username: 'Vortigern', avatar_url: null, is_online: true, last_connected_unix: Math.floor(now / 1000), physical_power: 812.5, spell_power: 640.2 },
    { steam_id: '2', character_name: 'Shadowfang', role: 'officer', username: 'Shadowfang', avatar_url: null, is_online: false, last_connected_unix: Math.floor((now - 3600000) / 1000), physical_power: 705.0, spell_power: 512.8 },
    { steam_id: '3', character_name: 'Dracarys', role: 'member', username: 'Dracarys', avatar_url: null, is_online: true, last_connected_unix: Math.floor(now / 1000), physical_power: 590.4, spell_power: 480.1 },
    { steam_id: '999', character_name: 'UnlinkedWanderer', role: 'member', username: null, avatar_url: null, is_online: false, last_connected_unix: Math.floor((now - 86400000) / 1000), physical_power: 320.0, spell_power: 210.5 },
  ].slice(0, Math.min(n, 4));

  // GameClanBaseOut shape (backend/schemas.py) — castle base(s) synced per clan.
  // min/max x/z are for a future map overlay, not used by clans.html yet.
  const clans = [
    { id: 1, server_num: 1, server_name: '[RU] Just-Skill.Ru | Standart PvE', clan_guid: 'guid-1', name: 'Кровавые Клыки', motto: 'Старейший клан сервера. Ищем активных игроков для рейдов.', member_count: 12, updated_at: iso(2 * 3600 * 1000), member_preview: memberPreview(12),
      bases: [{ level: 5, floor_count: 12, is_raid_protected: true, min_x: -420, min_z: -180, max_x: -340, max_z: -100 }] },
    { id: 2, server_num: 1, server_name: '[RU] Just-Skill.Ru | Standart PvE', clan_guid: 'guid-2', name: 'Ночная Стража', motto: 'PvE-фокус, помогаем новичкам освоиться.', member_count: 7, updated_at: iso(5 * 3600 * 1000), member_preview: memberPreview(7),
      bases: [{ level: 3, floor_count: 6, is_raid_protected: false, min_x: 60, min_z: 200, max_x: 130, max_z: 270 }] },
    { id: 3, server_num: 2, server_name: '[RU] Just-Skill.Ru | Brutal PvE', clan_guid: 'guid-3', name: 'Алый Договор', motto: '', member_count: 3, updated_at: iso(24 * 3600 * 1000), member_preview: memberPreview(3),
      bases: [] },
    { id: 4, server_num: 2, server_name: '[RU] Just-Skill.Ru | Brutal PvE', clan_guid: 'guid-4', name: 'Пепельный Клинок', motto: 'PvP-кланы, объединяйтесь.', member_count: 5, updated_at: iso(10 * 3600 * 1000), member_preview: memberPreview(5),
      // Multiple bases — exercises clans.html's "highest level / summed floors / any protected" aggregation.
      bases: [
        { level: 4, floor_count: 8, is_raid_protected: false, min_x: -800, min_z: 300, max_x: -730, max_z: 370 },
        { level: 6, floor_count: 3, is_raid_protected: true, min_x: -700, min_z: 320, max_x: -660, max_z: 360 },
      ] },
  ];

  const clanDetail = (id) => {
    const base = clans.find(c => c.id === Number(id)) || clans[0];
    return {
      ...base,
      members: [
        { steam_id: '1', character_name: 'Vortigern', role: 'leader', username: 'Vortigern', avatar_url: null, is_online: true, last_connected_unix: Math.floor(now / 1000), physical_power: 812.5, spell_power: 640.2 },
        { steam_id: '2', character_name: 'Shadowfang', role: 'officer', username: 'Shadowfang', avatar_url: null, is_online: false, last_connected_unix: Math.floor((now - 3600000) / 1000), physical_power: 705.0, spell_power: 512.8 },
        { steam_id: '999', character_name: 'UnlinkedWanderer', role: 'member', username: null, avatar_url: null, is_online: false, last_connected_unix: Math.floor((now - 86400000) / 1000), physical_power: 320.0, spell_power: 210.5 },
      ],
    };
  };

  const events = {
    items: [
      { id: 1, title: 'Полный вайп сервера', description: 'Готовьтесь к новому циклу — сервер будет сброшен полностью.', event_type: 'wipe', start_date: iso(-2 * 24 * 3600 * 1000), end_date: null, max_participants: null, status: 'upcoming', cover_url: null, created_by: 1, created_at: iso(10 * 24 * 3600 * 1000), participant_count: 34, is_joined: false },
      { id: 2, title: 'Турнир кланов «Кровавая арена»', description: 'PvP-турнир 3х3, победитель получает экслюзивный титул.', event_type: 'tournament', start_date: iso(-5 * 24 * 3600 * 1000), end_date: iso(-4 * 24 * 3600 * 1000), max_participants: 32, status: 'upcoming', cover_url: null, created_by: 1, created_at: iso(8 * 24 * 3600 * 1000), participant_count: 18, is_joined: false },
      { id: 3, title: 'Хэллоуин ивент', description: 'Особые дропы и декорации до конца недели.', event_type: 'event', start_date: iso(1 * 24 * 3600 * 1000), end_date: iso(-3 * 24 * 3600 * 1000), max_participants: null, status: 'active', cover_url: null, created_by: 1, created_at: iso(3 * 24 * 3600 * 1000), participant_count: 52, is_joined: true },
    ],
    total: 3,
  };

  // PlayerRecordOut (backend/schemas.py) gained clan_id/clan_name/physical_power/
  // spell_power/is_online/streak_days this session — ALL nullable, so most rows below
  // are explicit null/0 (a player outside a synced clan), with a handful of rows
  // populated to exercise every rendered state: clan chip + online dot (row 0), clan
  // chip + explicit-offline grey dot (row 1), a second clan (row 2, tests the chip
  // doesn't hardcode one clan), a streak with no clan (row 3), a big streak number
  // (row 9), and row 0's total_seconds bumped past the 1000h tier so the achievement
  // badge's top icon (🏆, same as user.html's hours1000) is visible somewhere too.
  const CLAN_ID    = [1, 1, 2, null, null, null, null, null, null, null, null, null];
  const CLAN_NAME  = ['Кровавые Клыки', 'Кровавые Клыки', 'Ночная Стража', null, null, null, null, null, null, null, null, null];
  const PHYS_POWER = [812.5, 705.0, 590.4, null, null, null, null, null, null, null, null, null];
  const SPELL_POWER = [640.2, 512.8, 480.1, null, null, null, null, null, null, null, null, null];
  const IS_ONLINE  = [true, false, true, null, null, null, null, null, null, null, null, null];
  const STREAK_DAYS = [12, 3, 0, 7, 0, 0, 5, 0, 0, 21, 0, 0];
  const leaderboardPage = (server) => Array.from({ length: 12 }, (_, i) => ({
    id: i + 1, server_num: server, player_name: ['Vortigern', 'Shadowfang', 'Dracarys', 'buhalovna', 'Nightshade', 'Emberclaw', 'Grimwald', 'Ashlynn', 'Malakor', 'Seraphine', 'Thornwick', 'Ravenna'][i],
    total_seconds: i === 0 ? 3700000 : Math.max(600, 500000 - i * 38000), last_seen: iso(i * 3600 * 1000),
    last_duration: 3600 + i * 120, session_count: 40 - i, avatar_url: null,
    rank_delta: [3, -1, 0, 2, null, -4, 1, 0, null, 5, -2, 0][i],
    // PlayerRecordOut.verified (backend/schemas.py) — True once a real /api/plugin/sessions
    // report claimed this row; mixed here so the preview shows both badge states.
    verified: i % 2 === 0,
    clan_id: CLAN_ID[i], clan_name: CLAN_NAME[i],
    physical_power: PHYS_POWER[i], spell_power: SPELL_POWER[i],
    is_online: IS_ONLINE[i], streak_days: STREAK_DAYS[i],
  }));

  // GET /api/leaderboard/trend?player_name=X&server=N&days=14 — per-player daily
  // playtime feeding leaderboard.html's expandable row trend chart (renderBarChart()).
  // A couple of zero-second days are included so the "no bar" rendering is exercised too.
  const leaderboardTrend = (days) => Array.from({ length: days }, (_, i) => {
    const daysAgo = days - 1 - i;
    const dateStr = new Date(now - daysAgo * 24 * 3600 * 1000).toISOString().slice(0, 10);
    const seconds = i % 5 === 0 ? 0 : Math.max(0, Math.round(1800 + Math.sin(i / 2) * 1500 + i * 60));
    return { date: dateStr, seconds };
  });

  // PointsLeaderboardEntryOut shape (backend/schemas.py) — GET /api/leaderboard/points,
  // the leaderboard.html "💎 Очки" toggle. Global per-account balance, not per-server.
  const pointsLeaderboardPage = () => Array.from({ length: 10 }, (_, i) => ({
    username: ['Vortigern', 'Shadowfang', 'Dracarys', 'buhalovna', 'Nightshade', 'Emberclaw', 'Grimwald', 'Ashlynn', 'Malakor', 'Seraphine'][i],
    avatar_url: null,
    points_balance: Math.max(10, 8200 - i * 740),
  }));

  const monitorStatus = (name, players, ip, port) => ({
    online: true, name, players, max_players: 40, version: '1.0', map: 'Farbane Woods', vac: true,
    players_list: Array.from({ length: players }, (_, i) => ({ name: `Player${i}`, score: 0, duration: 3600 + i * 300 })),
    latency_ms: 42, ip, game_port: port,
  });

  const monitorStats = () => ({
    uptime_24h: 99.2, uptime_7d: 97.8, peak_24h: 18, peak_7d: 27,
    peak_alltime: 40, peak_alltime_date: iso(20 * 24 * 3600 * 1000),
    heatmap: Array.from({ length: 24 }, (_, h) => Math.round(5 + 10 * Math.sin((h - 6) / 24 * Math.PI * 2) + 10)),
  });

  // 7 days × every 30min = 336 points — spans a full week (not just the last 24h)
  // so the servers.html day×hour heatmap actually has more than one row of data to
  // render, and the 3d/7d period toggle has something to visibly differ on.
  const snapshots = (n) => Array.from({ length: 336 }, (_, i) => ({
    ts: Math.floor((now - (336 - i) * 1800 * 1000) / 1000),
    players: Math.max(0, Math.round(n + Math.sin(i / 5) * n * 0.6)),
    online: true, latency_ms: 40 + Math.round(Math.random() * 20),
  }));

  const wipes = [
    { id: 1, server_num: 1, wipe_type: 'full', wipe_date: iso(-56 * 24 * 3600 * 1000), note: null, created_at: iso(60 * 24 * 3600 * 1000) },
    { id: 2, server_num: 2, wipe_type: 'map', wipe_date: iso(-56 * 24 * 3600 * 1000), note: null, created_at: iso(60 * 24 * 3600 * 1000) },
  ];

  // GET /api/bans (backend/main.py) is a public, unauthenticated list of
  // currently-active in-game bans — character names and reasons ARE included
  // deliberately (ordinary server-transparency content, not sensitive personal data)
  // — used by bans.html's public bans table.
  const bans = {
    bans: [
      { id: 101, server_num: 1, server_name: '[RU] Just-Skill.Ru | Standart PvE', character_name: 'Griefer42', admin_name: 'Overseer', reason: 'Использование читов (дюп предметов)', banned_at: iso(3 * 3600 * 1000), unban_at: null, linked_username: 'Griefer42' },
      { id: 102, server_num: 1, server_name: '[RU] Just-Skill.Ru | Standart PvE', character_name: 'ToxicPlayer', admin_name: 'Overseer', reason: 'Оскорбления в чате', banned_at: iso(5 * 3600 * 1000), unban_at: iso(-19 * 3600 * 1000), linked_username: null },
      { id: 103, server_num: 2, server_name: '[RU] Just-Skill.Ru | Brutal PvE', character_name: 'RaidAbuser', admin_name: 'Nightwatch', reason: 'Рейд в защищённый период', banned_at: iso(30 * 3600 * 1000), unban_at: null, linked_username: null },
    ],
  };

  // GET /api/team (backend/routers/profile.py) — public admin/superadmin staff
  // roster shown on index.html's "Команда" section (frontend/index.js loadTeam()).
  const team = [
    { id: 1, username: 'Vortigern', avatar_url: null, created_at: iso(300 * 24 * 3600 * 1000), admin_title: null, last_active_at: iso(60 * 1000), is_online: true, badge_icon_url: null, badge_style: 'crown', role: 'superadmin' },
    { id: 2, username: 'Overseer', avatar_url: null, created_at: iso(250 * 24 * 3600 * 1000), admin_title: null, last_active_at: iso(2 * 3600 * 1000), is_online: false, badge_icon_url: null, badge_style: 'shield', role: 'admin' },
    { id: 3, username: 'Nightwatch', avatar_url: null, created_at: iso(200 * 24 * 3600 * 1000), admin_title: 'Модератор чата', last_active_at: iso(20 * 24 * 3600 * 1000), is_online: false, badge_icon_url: null, badge_style: 'flame', role: 'admin' },
  ];

  const userProfile = {
    username: 'Vortigern', avatar_url: null, cover_url: null, role: 'user',
    created_at: iso(180 * 24 * 3600 * 1000), game_nickname: 'Vortigern',
    total_seconds: 500000, last_seen: iso(3600 * 1000), session_count: 45,
    last_duration: 5400, verified: true, clan: { id: 1, name: 'Кровавые Клыки' },
    admin_title: null, last_active_at: iso(600000), badge_icon_url: null,
    badge_style: 'default', comment_count: 23,
  };
  const userActivity = {
    username: 'Vortigern',
    items: [
      { type: 'comment', created_at: iso(2 * 3600 * 1000), news_slug: 'news-1', news_title: 'Обновление сервера', preview: 'Отличное обновление, спасибо!' },
      { type: 'reaction', created_at: iso(5 * 3600 * 1000), news_slug: 'news-2', news_title: 'Хэллоуин ивент', emoji: '🔥' },
      { type: 'comment', created_at: iso(26 * 3600 * 1000), news_slug: 'news-1', news_title: 'Обновление сервера', preview: 'Когда следующий вайп?' },
    ],
  };

  // ShopItemOut shape (backend/schemas.py) — GET /api/shop/items. shop.html is
  // login-gated (redirects to the login gate on a 401 /api/auth/me, which this mock
  // always returns for the anonymous-visitor case below), so these routes aren't
  // exercised by the default anonymous preview — kept here so a future authenticated
  // mock mode (or a page that browses items while logged out) has canned data ready,
  // matching this file's existing convention of covering every new-endpoint shape.
  const shopItems = [
    { id: 1, name: 'Waypoint Shard', description: 'Телепорт-камень для быстрого перемещения.', cost: 50, image_url: null, is_active: true, stock: null, sort_order: 0, created_at: iso(10 * 24 * 3600 * 1000), updated_at: iso(2 * 24 * 3600 * 1000) },
    { id: 2, name: 'Blood Rose Seeds', description: 'Редкие семена для фермы крови.', cost: 120, image_url: null, is_active: true, stock: 4, sort_order: 1, created_at: iso(8 * 24 * 3600 * 1000), updated_at: iso(8 * 24 * 3600 * 1000) },
  ];
  const myShopRedemptions = {
    total: 1, page: 1, per_page: 20,
    items: [
      { id: 1, user_id: 1, shop_item_id: 1, item_name_snapshot: 'Waypoint Shard', cost_snapshot: 50, status: 'pending', delivery_mode: 'manual', player_note: null, admin_note: null, created_at: iso(3600000), resolved_at: null, resolved_by: null },
    ],
  };

  // SearchResultOut shape (backend/routers/search.py) — GET /api/search?q=, backing
  // the Ctrl+K global search dropdown (frontend/common.js openGlobalSearch()). One
  // canned result per category so the dropdown's three grouped sections + snippet
  // text are all visible regardless of what the operator types.
  const searchResults = [
    { type: 'player', title: 'Vortigern', url: '/user.html?u=Vortigern', snippet: 'superadmin' },
    { type: 'news', title: 'Обновление сервера 1.2', url: '/?news=obnovlenie-servera-1-2', snippet: 'Список изменений и исправлений в последнем патче.' },
    { type: 'clan', title: 'Кровавые Клыки', url: '/clans.html?clan=1', snippet: 'Старейший клан сервера. Ищем активных игроков для рейдов.' },
  ];

  // ActivityFeedItemOut shape (backend/routers/activity_feed.py) — GET /api/activity-feed,
  // the index.html right-sidebar "Лента событий" widget (frontend/index.js
  // loadActivityFeed()). One item per source type (news/event/milestone) so all three
  // icon/subtitle layouts are exercised in the screenshot, already in the
  // reverse-chronological order the real endpoint would return.
  const activityFeed = [
    { type: 'news', title: 'Обновление сервера 1.2', subtitle: 'Список изменений и исправлений в последнем патче.', url: '/?news=obnovlenie-servera-1-2', icon: '📰', timestamp: iso(20 * 60 * 1000) },
    { type: 'milestone', title: 'Vortigern', subtitle: '100+ часов на сервере', url: '/user.html?u=Vortigern', icon: '⚔', timestamp: iso(90 * 60 * 1000) },
    { type: 'event', title: 'Турнир кланов «Кровавая арена»', subtitle: `Начало: ${iso(-5 * 24 * 3600 * 1000)}`, url: '/events.html', icon: '📅', timestamp: iso(4 * 3600 * 1000) },
    { type: 'milestone', title: 'Shadowfang', subtitle: 'Играет 7 дней подряд', url: '/user.html?u=Shadowfang', icon: '🔥', timestamp: iso(9 * 3600 * 1000) },
    { type: 'news', title: 'Хэллоуин ивент стартовал', subtitle: 'Особые дропы и декорации до конца недели.', url: '/?news=halloween-event', icon: '📰', timestamp: iso(28 * 3600 * 1000) },
  ];

  const routes = [
    [/\/api\/activity-feed/, () => activityFeed],
    [/\/api\/search(\?|$)/, () => searchResults],
    [/\/api\/settings\/public$/, () => settingsPublic],
    [/\/api\/auth\/me$/, () => null], // anonymous visitor — handled as 401 below
    [/\/api\/team/, () => team],
    [/\/api\/users\/[^/]+\/activity/, () => userActivity],
    [/\/api\/users\/[^/]+$/, () => userProfile],
    [/\/api\/clans\/\d+$/, (url) => clanDetail(url.match(/\/api\/clans\/(\d+)/)[1])],
    [/\/api\/clans(\?|$)/, () => clans],
    [/\/api\/events/, () => events],
    [/\/api\/leaderboard\/points/, () => pointsLeaderboardPage()],
    [/\/api\/leaderboard\/trend/, (url) => { const m = url.match(/days=(\d+)/); return leaderboardTrend(m ? Number(m[1]) : 14); }],
    [/\/api\/leaderboard/, (url) => leaderboardPage(url.includes('server=2') ? 2 : 1)],
    [/\/api\/monitor\/status2/, () => ({ enabled: true, ...monitorStatus('[RU] Just-Skill.Ru | Brutal PvE', 6, '127.0.0.1', 27017) })],
    [/\/api\/monitor\/status$/, () => monitorStatus('[RU] Just-Skill.Ru | Standart PvE', 14, '127.0.0.1', 27016)],
    [/\/api\/monitor\/stats/, () => monitorStats()],
    [/\/api\/monitor\/snapshots/, (url) => snapshots(url.includes('server=2') ? 5 : 12)],
    [/\/api\/wipes$/, () => wipes],
    [/\/api\/bans/, () => bans],
    [/\/api\/shop\/items$/, () => shopItems],
    [/\/api\/shop\/redemptions\/me/, () => myShopRedemptions],
  ];

  const realFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (/\/api\/auth\/me$/.test(url)) {
      return Promise.resolve(new Response('{"detail":"Not authenticated"}', { status: 401, headers: { 'Content-Type': 'application/json' } }));
    }
    for (const [pattern, respond] of routes) {
      if (pattern.test(url)) {
        const body = respond(url);
        return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      }
    }
    if (url.includes('/api/')) {
      return Promise.resolve(new Response('not mocked', { status: 404 }));
    }
    return realFetch(input, init);
  };
})();
