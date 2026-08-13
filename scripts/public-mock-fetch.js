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
    // rules_tldr (backend/routers/admin_settings.py) — homepage TL;DR-above-the-
    // accordion widget (frontend/index.js loadSiteSettings()), newline-separated bullets.
    rules_tldr: 'Без читов и дюпов\nУважайте других игроков\nРейды только по расписанию',
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

  // GameClanLeaderboardOut shape (backend/schemas.py) — GET /api/clans/leaderboard,
  // ranked by total combat power summed across each clan's FULL member roster (unlike
  // GET /api/clans's member_preview above, capped at 4 members). Values below are just
  // plausible numbers for the screenshot, not derived from memberPreview's power sum.
  const clanLeaderboardRows = [
    { id: 1, server_num: 1, server_name: '[RU] Just-Skill.Ru | Standart PvE', name: 'Кровавые Клыки', motto: 'Старейший клан сервера. Ищем активных игроков для рейдов.', member_count: 12, online_count: 4, total_power: 15680, avg_power: 1306.7 },
    { id: 4, server_num: 2, server_name: '[RU] Just-Skill.Ru | Brutal PvE', name: 'Пепельный Клинок', motto: 'PvP-кланы, объединяйтесь.', member_count: 5, online_count: 2, total_power: 9120, avg_power: 1824.0 },
    { id: 2, server_num: 1, server_name: '[RU] Just-Skill.Ru | Standart PvE', name: 'Ночная Стража', motto: 'PvE-фокус, помогаем новичкам освоиться.', member_count: 7, online_count: 1, total_power: 6440, avg_power: 920.0 },
    { id: 3, server_num: 2, server_name: '[RU] Just-Skill.Ru | Brutal PvE', name: 'Алый Договор', motto: '', member_count: 3, online_count: 0, total_power: 2210, avg_power: 736.7 },
  ];
  const clanLeaderboard = (server) => {
    const rows = server ? clanLeaderboardRows.filter(c => c.server_num === server) : clanLeaderboardRows;
    return rows.slice().sort((a, b) => b.total_power - a.total_power);
  };

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

  // ClanMembershipEventOut shape (backend/schemas.py) — GET /api/clans/{id}/history,
  // backing clans.html's "Последние изменения состава" list in the clan-detail modal.
  // Newest first (reverse-chronological), mixing "joined"/"left" so both row colors
  // render in the screenshot.
  const clanHistory = (id) => {
    const base = clans.find(c => c.id === Number(id)) || clans[0];
    return [
      { id: 5, clan_name: base.name, steam_id: '3', character_name: 'Dracarys', event_type: 'joined', recorded_at: iso(2 * 3600 * 1000) },
      { id: 4, clan_name: base.name, steam_id: '7', character_name: 'OldMember', event_type: 'left', recorded_at: iso(26 * 3600 * 1000) },
      { id: 3, clan_name: base.name, steam_id: '2', character_name: 'Shadowfang', event_type: 'joined', recorded_at: iso(3 * 24 * 3600 * 1000) },
    ];
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

  // GET /api/monitor/incidents (backend/main.py) — status.html's incident timeline,
  // derived server-side from ServerSnapshot history. Two closed incidents plus one
  // still-ongoing (ended_at:null) so the mock exercises both status-incident-duration
  // render paths (closed "Xh Ym" pill vs. the amber "Ongoing" pill).
  const monitorIncidents = () => ([
    { started_at: iso(30 * 60 * 1000), ended_at: null, duration_minutes: 30, ongoing: true },
    { started_at: iso(2 * 24 * 3600 * 1000), ended_at: iso(2 * 24 * 3600 * 1000 - 13 * 60 * 1000), duration_minutes: 13, ongoing: false },
    { started_at: iso(9 * 24 * 3600 * 1000), ended_at: iso(9 * 24 * 3600 * 1000 - 95 * 60 * 1000), duration_minutes: 95, ongoing: false },
  ]);

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

  // clan.role/is_online + top-level is_online/online_server_*/last_server_* mirror the
  // fields GET /api/users/{username} (backend/routers/users.py get_public_profile)
  // gained alongside profile.html's #1/#2/#3 online-status/current-server/clan-role
  // widgets — is_online true here so the preview exercises the "online now" state,
  // not just the "last known server" fallback state.
  const userProfile = {
    username: 'Vortigern', avatar_url: null, cover_url: null, role: 'user',
    created_at: iso(180 * 24 * 3600 * 1000), game_nickname: 'Vortigern',
    total_seconds: 500000, last_seen: iso(3600 * 1000), session_count: 45,
    last_duration: 5400, verified: true,
    clan: { id: 1, name: 'Кровавые Клыки', role: 'leader', is_online: true },
    admin_title: null, last_active_at: iso(600000), badge_icon_url: null,
    badge_style: 'default', comment_count: 23,
    is_online: true, online_server_num: 1, online_server_name: '[RU] Just-Skill.Ru | Standart PvE',
    last_server_num: 1, last_server_name: '[RU] Just-Skill.Ru | Standart PvE',
  };
  const userActivity = {
    username: 'Vortigern',
    items: [
      { type: 'comment', created_at: iso(2 * 3600 * 1000), news_slug: 'news-1', news_title: 'Обновление сервера', preview: 'Отличное обновление, спасибо!' },
      { type: 'reaction', created_at: iso(5 * 3600 * 1000), news_slug: 'news-2', news_title: 'Хэллоуин ивент', emoji: '🔥' },
      { type: 'comment', created_at: iso(26 * 3600 * 1000), news_slug: 'news-1', news_title: 'Обновление сервера', preview: 'Когда следующий вайп?' },
    ],
  };
  // GET /api/users/{u}/activity-trend (backend/routers/users.py) — daily playtime deltas
  // feeding the renderBarChart() "Активность за месяц" card on both user.html and (as of
  // the profile-tab work adding #6) profile.html. >=2 points so the card doesn't hide
  // itself (see that endpoint's own docstring on why a single day is dropped).
  const userActivityTrend = [0, 1, 2, 3, 4, 5, 6].map((daysAgo) => ({
    date: new Date(now - daysAgo * 24 * 3600 * 1000).toISOString().slice(0, 10),
    seconds: [5400, 9000, 0, 12600, 7200, 3600, 10800][daysAgo],
  })).reverse();
  // GET /api/users/{u}/activity-heatmap — presence-only calendar (see that endpoint's
  // docstring), also the data source for profile.html's #5 connect-streak stat tile
  // (computed client-side — see loadProfileHeatmapAndStreak()). Today plus the 4
  // preceding days are active so the streak tile shows a non-trivial "5".
  const userActivityHeatmap = {
    days: 180,
    active_dates: [0, 1, 2, 3, 4, 10, 11, 20].map((daysAgo) =>
      new Date(now - daysAgo * 24 * 3600 * 1000).toISOString().slice(0, 10)),
  };

  // ShopItemOut shape (backend/schemas.py) — GET /api/shop/items. shop.html is
  // login-gated (redirects to the login gate on a 401 /api/auth/me), so — unlike every
  // other page this file mocks — it needs an authenticated /api/auth/me response to
  // actually render past the gate. Scoped narrowly to shop.html only (see
  // _mockAuthedUser below): every other page in this file stays a true anonymous
  // visitor, so their own login-gate/CTA screenshots are unaffected.
  // category/weekly_limit_per_user/weekly_remaining/wishlisted cover the filter pills,
  // weekly-limit label, and heart-icon states added alongside this mock update.
  const shopItems = [
    { id: 1, name: 'Waypoint Shard', description: 'Телепорт-камень для быстрого перемещения.', cost: 50, image_url: null, is_active: true, stock: null, sort_order: 0, category: 'Телепорт', weekly_limit_per_user: null, weekly_remaining: null, wishlisted: true, created_at: iso(10 * 24 * 3600 * 1000), updated_at: iso(2 * 24 * 3600 * 1000) },
    { id: 2, name: 'Blood Rose Seeds', description: 'Редкие семена для фермы крови.', cost: 120, image_url: null, is_active: true, stock: 4, sort_order: 1, category: 'Ресурсы', weekly_limit_per_user: null, weekly_remaining: null, wishlisted: false, created_at: iso(8 * 24 * 3600 * 1000), updated_at: iso(8 * 24 * 3600 * 1000) },
    { id: 3, name: 'Плащ вампира', description: 'Косметический плащ — не влияет на характеристики.', cost: 300, image_url: null, is_active: true, stock: null, sort_order: 2, category: 'Косметика', weekly_limit_per_user: 2, weekly_remaining: 1, wishlisted: false, created_at: iso(5 * 24 * 3600 * 1000), updated_at: iso(24 * 3600 * 1000) },
    { id: 4, name: 'Смена внешности', description: 'Полная перекройка персонажа в игре.', cost: 200, image_url: null, is_active: true, stock: null, sort_order: 3, category: 'Косметика', weekly_limit_per_user: 1, weekly_remaining: 0, wishlisted: true, created_at: iso(15 * 24 * 3600 * 1000), updated_at: iso(3 * 24 * 3600 * 1000) },
  ];
  const shopWishlist = shopItems.filter(i => i.wishlisted);
  // UserOut shape (backend/schemas.py) — GET /api/auth/me for the authenticated-preview
  // pages (shop.html and profile.html, see _mockAuthedUser below). Username matches
  // userProfile above so profile.html's own-profile fetch of GET /api/users/{username}
  // resolves to the same identity as /api/auth/me.
  const shopAuthedUser = {
    id: 1, username: 'Vortigern', email: 'vortigern@example.com', role: 'user', is_active: true,
    created_at: iso(240 * 24 * 3600 * 1000), avatar_url: null, cover_url: null,
    rules_accepted_at: iso(240 * 24 * 3600 * 1000), game_nickname: 'Vortigern', admin_title: null,
    last_active_at: iso(5 * 60000), badge_icon_url: null, badge_style: 'default', totp_enabled: false,
    bio: null, points_balance: 340, newsletter_opt_in: true,
  };
  const myShopRedemptions = {
    total: 1, page: 1, per_page: 20,
    items: [
      { id: 1, user_id: 1, shop_item_id: 1, item_name_snapshot: 'Waypoint Shard', cost_snapshot: 50, status: 'pending', delivery_mode: 'manual', player_note: null, admin_note: null, created_at: iso(3600000), resolved_at: null, resolved_by: null },
    ],
  };
  // GET /api/points/transactions/me (backend/routers/points_shop.py) — own points
  // ledger, shown on profile.html's Очки tab AND (filtered to reason="nickname_change")
  // the Profile tab's #10 own-nickname-history panel (loadNickHistory()). One
  // nickname_change row included so that panel's screenshot isn't just an empty state.
  const myPointsTransactions = {
    total: 3, page: 1, per_page: 30,
    items: [
      { id: 1, user_id: 1, delta: 60, balance_after: 340, reason: 'playtime', detail: '3600s session on server 1', created_at: iso(2 * 3600 * 1000) },
      { id: 2, user_id: 1, delta: -100, balance_after: 280, reason: 'nickname_change', detail: 'OldVortigern -> Vortigern', created_at: iso(20 * 24 * 3600 * 1000) },
      { id: 3, user_id: 1, delta: -50, balance_after: 380, reason: 'redeem', detail: 'Waypoint Shard', created_at: iso(3600000) },
    ],
  };

  // SearchResultOut shape (backend/routers/search.py) — GET /api/search?q=, backing
  // the Ctrl+K global search dropdown (frontend/common.js openGlobalSearch()). One
  // canned result per category so the dropdown's six grouped sections + snippet
  // text are all visible regardless of what the operator types.
  const searchResults = [
    { type: 'player', title: 'Vortigern', url: '/user.html?u=Vortigern', snippet: 'superadmin' },
    { type: 'news', title: 'Обновление сервера 1.2', url: '/?news=obnovlenie-servera-1-2', snippet: 'Список изменений и исправлений в последнем патче.' },
    { type: 'clan', title: 'Кровавые Клыки', url: '/clans.html?clan=1', snippet: 'Старейший клан сервера. Ищем активных игроков для рейдов.' },
    { type: 'server', title: 'V Rising PvP #1', url: '/servers.html', snippet: 'Сервер 1' },
    { type: 'event', title: 'Турнир кланов «Кровавая арена»', url: '/events.html?event=1', snippet: 'Ежемесячное PvP событие с призами.' },
    { type: 'shop_item', title: 'Waypoint Shard', url: '/shop.html', snippet: 'Осколок телепорта для быстрого перемещения.' },
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
    // shop_redemption (backend/routers/activity_feed.py) — fulfilled ShopRedemption rows.
    { type: 'shop_redemption', title: 'Waypoint Shard', subtitle: 'Получил: Dracarys', url: '/shop.html', icon: '🛒', timestamp: iso(3 * 3600 * 1000) },
  ];

  // GET /api/homepage-stats (backend/main.py get_homepage_stats()) — backs the
  // trust-stats bar, the registered-player milestone banner and the community
  // hours progress bar (frontend/index.js loadHomepageStats()). total_users is
  // deliberately set just above a round milestone (1000) so the milestone
  // banner's band-based heuristic (see MILESTONE_BAND in index.js) is actually
  // exercised in the screenshot instead of staying hidden; total_hours sits well
  // under the next "nice" goal (100 000) so the progress bar shows a partial,
  // not-yet-complete fill.
  const homepageStats = {
    total_users: 1008,
    total_hours: 86500,
    top_clan: { name: 'Кровавые Клыки', member_count: 12 },
  };

  // GET /api/users/recent (backend/routers/users.py) — homepage "Новые игроки" avatar
  // strip (frontend/index.js loadNewPlayers()). Newest-first, matching the real endpoint.
  const recentUsers = [
    { username: 'Nightshade', avatar_url: null, created_at: iso(2 * 3600 * 1000) },
    { username: 'Emberclaw', avatar_url: null, created_at: iso(20 * 3600 * 1000) },
    { username: 'Grimwald', avatar_url: null, created_at: iso(2 * 24 * 3600 * 1000) },
    { username: 'Ashlynn', avatar_url: null, created_at: iso(4 * 24 * 3600 * 1000) },
  ];

  // GET /api/servers/{n}/restart-status (backend/routers/server_admin.py) — public
  // read-only counterpart to the admin/plugin restart-status endpoints, backing the
  // homepage restart-countdown banner (frontend/index.js loadRestartBanner()). Server 1
  // has a restart scheduled ~2h15m out (within the banner's visibility window); server 2
  // has nothing scheduled, exercising both the shown and hidden states.
  const restartStatus = (serverNum) => ({
    restart_at: serverNum === 1 ? iso(-(2 * 3600 + 15 * 60) * 1000) : null,
  });

  // PaginatedNews shape (backend/schemas.py NewsListOut) — GET /api/news, backing
  // index.html's news feed (frontend/index.js's loadNews()). Previously unmocked,
  // so every homepage screenshot this far just showed loadNews()'s catch-block
  // "Ошибка загрузки" state instead of real cards — this is what a visual review
  // of the homepage was actually blind to.
  const newsAuthor = { username: 'Vortigern', avatar_url: null, role: 'superadmin', admin_title: null, badge_icon_url: null, badge_style: 'crown' };
  const newsItems = [
    { id: 5, title: 'Обновление сервера 1.2 — исправления и баланс', slug: 'obnovlenie-servera-1-2', summary: 'Список изменений: исправлена дюп-уязвимость с алтарями, перебалансированы боссы Silverlight Hills, ускорена загрузка чанков.', thumbnail_url: null, tags: 'патч,баланс', published: true, pinned: true, views: 842, publish_at: null, is_template: false, created_at: iso(20 * 60 * 1000), author: newsAuthor, comment_count: 14 },
    { id: 4, title: 'Турнир кланов «Кровавая арена» — регистрация открыта', slug: 'turnir-krovavaya-arena', summary: 'PvP-турнир 3х3 в конце недели, призовой фонд в очках магазина. Регистрация на странице событий.', thumbnail_url: null, tags: 'события,pvp', published: true, pinned: false, views: 511, publish_at: null, is_template: false, created_at: iso(9 * 3600 * 1000), author: newsAuthor, comment_count: 6 },
    { id: 3, title: 'Хэллоуин-ивент стартовал', slug: 'halloween-event', summary: 'Особые дропы, тыквенные декорации построек и лимитированный бейдж профиля — до конца недели.', thumbnail_url: null, tags: 'события', published: true, pinned: false, views: 298, publish_at: null, is_template: false, created_at: iso(28 * 3600 * 1000), author: newsAuthor, comment_count: 3 },
    { id: 2, title: 'Плановые технические работы 12.08', slug: 'planovye-raboty-12-08', summary: 'Оба сервера будут недоступны примерно 30 минут для миграции базы данных.', thumbnail_url: null, tags: 'технические', published: true, pinned: false, views: 156, publish_at: null, is_template: false, created_at: iso(3 * 24 * 3600 * 1000), author: newsAuthor, comment_count: 1 },
    { id: 1, title: 'Добро пожаловать на Just-Skill.Ru', slug: 'dobro-pozhalovat', summary: 'Правила, вайпы, как привязать игровой аккаунт — коротко обо всём на странице FAQ.', thumbnail_url: null, tags: 'общее', published: true, pinned: false, views: 1204, publish_at: null, is_template: false, created_at: iso(20 * 24 * 3600 * 1000), author: newsAuthor, comment_count: 22 },
  ];
  // Respects ?tag= and ?search= like the real endpoint does — without this, a
  // tag-filtered request (e.g. loadFeatured()'s /api/news?tag=featured) got back
  // the exact same unfiltered list as the plain homepage feed, making the
  // "featured" card and the first regular list card show the identical article
  // on every mock screenshot — looked like a real duplicate-content bug, but was
  // actually just this mock ignoring the query string. None of the items above
  // carry a "featured" tag, so a real ?tag=featured request now correctly comes
  // back empty and the featured-wrap hides, matching what an admin who hasn't
  // tagged anything "featured" yet would actually see.
  const newsPage = (url) => {
    const qs = new URLSearchParams((url.split('?')[1] || ''));
    const tag = qs.get('tag');
    const search = (qs.get('search') || '').trim().toLowerCase();
    let items = newsItems;
    if (tag) items = items.filter(n => (n.tags || '').split(',').map(t => t.trim()).includes(tag));
    if (search) items = items.filter(n => n.title.toLowerCase().includes(search));
    return { items, total: items.length, page: 1, pages: 1 };
  };

  const routes = [
    [/\/api\/news(\?|$)/, newsPage],
    [/\/api\/activity-feed/, () => activityFeed],
    [/\/api\/homepage-stats/, () => homepageStats],
    [/\/api\/search(\?|$)/, () => searchResults],
    [/\/api\/settings\/public$/, () => settingsPublic],
    [/\/api\/auth\/me$/, () => null], // anonymous visitor — handled as 401 below
    [/\/api\/team/, () => team],
    [/\/api\/users\/recent/, () => recentUsers],
    // activity-trend/activity-heatmap registered before the generic .../activity below —
    // that broader pattern has no trailing anchor, so "activity-trend"/"activity-heatmap"
    // would otherwise match it first and get the wrong (activity-feed) response shape.
    [/\/api\/users\/[^/]+\/activity-trend/, () => userActivityTrend],
    [/\/api\/users\/[^/]+\/activity-heatmap/, () => userActivityHeatmap],
    [/\/api\/users\/[^/]+\/activity/, () => userActivity],
    [/\/api\/users\/[^/]+$/, () => userProfile],
    [/\/api\/servers\/\d+\/restart-status/, (url) => restartStatus(Number(url.match(/\/api\/servers\/(\d+)\/restart-status/)[1]))],
    [/\/api\/clans\/leaderboard/, (url) => clanLeaderboard(url.includes('server=2') ? 2 : (url.includes('server=1') ? 1 : null))],
    [/\/api\/clans\/\d+\/history/, (url) => clanHistory(url.match(/\/api\/clans\/(\d+)\/history/)[1])],
    [/\/api\/clans\/\d+$/, (url) => clanDetail(url.match(/\/api\/clans\/(\d+)/)[1])],
    [/\/api\/clans(\?|$)/, () => clans],
    [/\/api\/events/, () => events],
    [/\/api\/leaderboard\/points/, () => pointsLeaderboardPage()],
    [/\/api\/leaderboard\/trend/, (url) => { const m = url.match(/days=(\d+)/); return leaderboardTrend(m ? Number(m[1]) : 14); }],
    // GET /api/leaderboard/snapshot-range — feeds leaderboard.html's "as of a past
    // date" picker (min date + hint text). Registered before the generic
    // /api/leaderboard pattern below, same ordering reason as /points and /trend
    // (a substring match on the broader regex would otherwise win first and return
    // an array of player rows instead of {earliest_date}).
    [/\/api\/leaderboard\/snapshot-range/, () => ({ earliest_date: iso(30 * 24 * 3600 * 1000).slice(0, 10) })],
    [/\/api\/leaderboard/, (url) => leaderboardPage(url.includes('server=2') ? 2 : 1)],
    [/\/api\/monitor\/status2/, () => ({ enabled: true, ...monitorStatus('[RU] Just-Skill.Ru | Brutal PvE', 6, '127.0.0.1', 27017) })],
    [/\/api\/monitor\/status$/, () => monitorStatus('[RU] Just-Skill.Ru | Standart PvE', 14, '127.0.0.1', 27016)],
    [/\/api\/monitor\/stats/, () => monitorStats()],
    [/\/api\/monitor\/incidents/, () => monitorIncidents()],
    [/\/api\/monitor\/snapshots/, (url) => snapshots(url.includes('server=2') ? 5 : 12)],
    [/\/api\/wipes$/, () => wipes],
    [/\/api\/bans/, () => bans],
    [/\/api\/shop\/items$/, () => shopItems],
    [/\/api\/shop\/wishlist\/me$/, () => shopWishlist],
    [/\/api\/shop\/redemptions\/me/, () => myShopRedemptions],
    [/\/api\/points\/transactions\/me/, () => myPointsTransactions],
    [/\/api\/auth\/current-session/, () => currentSession],
    [/\/api\/auth\/login-history/, () => loginHistory],
  ];

  // shop.html and profile.html are the pages this file mocks as a logged-in visitor
  // rather than anonymous — both are entirely behind an auth gate with no anonymous
  // fallback (unlike every other page here), so previewing them (category/wishlist/
  // weekly-limit UI for shop.html; the Profile-tab widgets, security-tab cards, etc.
  // for profile.html) needs GET /api/auth/me to actually succeed.
  const _mockAuthedUser = /shop\.html|profile\.html/.test(location.pathname);

  // GET /api/auth/current-session, GET /api/auth/login-history — added for
  // profile.html's security-tab "current session" / "login history" cards (see
  // backend/routers/auth.py). Shapes match those endpoints' real JSON exactly.
  const currentSession = {
    ip_address: '203.0.113.42',
    user_agent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    issued_at: iso(15 * 60 * 1000),
    expires_at: new Date(now + 7 * 24 * 3600 * 1000).toISOString(),
    last_active_at: iso(30 * 1000),
  };
  const loginHistory = {
    items: [
      { id: 5, success: true, failure_reason: null, ip_address: '203.0.113.42', user_agent: currentSession.user_agent, created_at: iso(30 * 1000) },
      { id: 4, success: false, failure_reason: 'invalid_totp', ip_address: '203.0.113.42', user_agent: currentSession.user_agent, created_at: iso(20 * 60 * 1000) },
      { id: 3, success: true, failure_reason: null, ip_address: '198.51.100.7', user_agent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1', created_at: iso(2 * 24 * 3600 * 1000) },
      { id: 2, success: false, failure_reason: 'invalid_credentials', ip_address: '203.0.113.99', user_agent: currentSession.user_agent, created_at: iso(5 * 24 * 3600 * 1000) },
      { id: 1, success: true, failure_reason: null, ip_address: '203.0.113.42', user_agent: currentSession.user_agent, created_at: iso(30 * 24 * 3600 * 1000) },
    ],
    total: 5, limit: 20, offset: 0,
  };

  const realFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (/\/api\/auth\/me$/.test(url)) {
      if (_mockAuthedUser) {
        return Promise.resolve(new Response(JSON.stringify(shopAuthedUser), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      }
      return Promise.resolve(new Response('{"detail":"Not authenticated"}', { status: 401, headers: { 'Content-Type': 'application/json' } }));
    }
    // POST adds / DELETE removes a wishlist entry (backend/routers/points_shop.py) —
    // same URL for both methods, so this needs to branch on init.method rather than
    // living in the plain GET-shaped `routes` table above.
    const wishlistMatch = url.match(/\/api\/shop\/wishlist\/(\d+)$/);
    if (wishlistMatch) {
      const method = ((init && init.method) || 'GET').toUpperCase();
      const wishlisted = method === 'POST';
      return Promise.resolve(new Response(
        JSON.stringify({ shop_item_id: Number(wishlistMatch[1]), wishlisted }),
        { status: wishlisted ? 201 : 200, headers: { 'Content-Type': 'application/json' } },
      ));
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
