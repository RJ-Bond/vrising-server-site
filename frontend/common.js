/* common.js — shared utilities for all pages */

// Inject shared styles once: toast animation + a keyboard-focus ring (a11y).
// :focus-visible shows only on keyboard navigation (not mouse clicks), so it
// adds an accessible focus indicator without affecting pointer users.
if (!document.getElementById('common-toast-styles')) {
  const _s = document.createElement('style');
  _s.id = 'common-toast-styles';
  _s.textContent =
    '@keyframes toast-in{from{opacity:0;transform:translateX(10px)}to{opacity:1;transform:translateX(0)}}' +
    ':focus-visible{outline:2px solid var(--gold,#c9a94a);outline-offset:2px;border-radius:3px}' +
    'a:focus-visible,button:focus-visible,[role="button"]:focus-visible{outline-offset:3px}';
  document.head.appendChild(_s);
}

/* HTML escape */
function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* Normalise naive ISO strings from SQLite (no tz suffix) to UTC before parsing */
function _toDate(iso) {
  if (!iso) return null;
  return new Date(iso.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(iso) ? iso : iso + 'Z');
}

/* Read cached user from storage */
function getUser() {
  try { return JSON.parse(localStorage.getItem('user') || sessionStorage.getItem('user')); } catch { return null; }
}

/* Any staff tier (moderator/admin/superadmin) — used for maintenance-mode bypass etc. */
const STAFF_ROLES = ['moderator', 'admin', 'superadmin'];

/* Gradient avatar background from username hash */
function nameGradient(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = Math.imul(h, 31) + name.charCodeAt(i) | 0;
  const hue = Math.abs(h) % 360;
  return `linear-gradient(135deg,hsl(${hue},60%,20%),hsl(${(hue+50)%360},45%,13%))`;
}

/* Date formatting (uses per-page window.__TZ / __H12 / __DATEFMT) */
window.__TZ     = window.__TZ     || 'Europe/Moscow';
window.__H12    = window.__H12    || false;
window.__DATEFMT = window.__DATEFMT || 'dd.mm.yyyy';

/* Single shared fetch of public settings — memoized so common.js and each
   page reuse ONE request instead of hitting /api/settings/public twice. */
window.getSettings = function () {
  if (!window.__settingsReq) {
    window.__settingsReq = fetch('/api/settings/public').then(r => r.ok ? r.json() : null).catch(() => null);
  }
  return window.__settingsReq;
};

/* Auto-load timezone / date settings from admin panel once at startup */
(async function _initSettings() {
  if (window.__settingsLoaded) return;
  try {
    const s = await window.getSettings();
    if (s) {
      window.__TZ      = s.timezone     || 'Europe/Moscow';
      window.__H12     = (s.time_format || '24h') === '12h';
      window.__DATEFMT = s.date_format  || 'dd.mm.yyyy';
      window.__settingsLoaded = true;

      // Favicon — applied on every page from the admin-configured URL
      const favUrl = (s.favicon_url || '').trim();
      if (favUrl) {
        let link = document.querySelector('link[rel="icon"]');
        if (!link) { link = document.createElement('link'); link.rel = 'icon'; document.head.appendChild(link); }
        link.href = favUrl;
      }

      // Nav-header logo — on EVERY page (was only set per-page as text, so an
      // uploaded logo only showed on the homepage). Renders the image if
      // site_logo_url is set, else "⚔ <title>". Pages must NOT also set it.
      const navLogoEl = document.getElementById('nav-logo');
      if (navLogoEl) {
        const navLogo  = (s.site_logo_url || '').trim();
        const navTitle = (s.site_title || '').trim() || 'V Rising';
        if (navLogo) {
          const img = document.createElement('img');
          img.src = navLogo; img.alt = navTitle;
          img.style.cssText = 'height:2rem;max-width:11rem;object-fit:contain;vertical-align:middle;';
          img.onerror = () => { navLogoEl.textContent = '⚔ ' + navTitle; };
          navLogoEl.replaceChildren(img);
        } else {
          navLogoEl.textContent = '⚔ ' + navTitle;
        }
      }

      // Maintenance mode redirect
      if (s.maintenance_mode === 'true' || s.maintenance_mode === true) {
        const path = location.pathname.replace(/\/+$/, '') || '/';
        const exempt = ['/maintenance.html', '/admin.html', '/login.html'];
        if (!exempt.includes(path)) {
          try {
            const u = JSON.parse(localStorage.getItem('user') || sessionStorage.getItem('user') || 'null');
            if (!u || !STAFF_ROLES.includes(u.role)) {
              window.location.replace('/maintenance.html');
            }
          } catch {
            window.location.replace('/maintenance.html');
          }
        }
      }

      // Admin maintenance banner
      if ((s.maintenance_mode === 'true' || s.maintenance_mode === true)) {
        const path = location.pathname;
        if (path !== '/maintenance.html') {
          try {
            const u2 = JSON.parse(localStorage.getItem('user') || sessionStorage.getItem('user') || 'null');
            if (u2 && STAFF_ROLES.includes(u2.role) && !document.getElementById('maint-admin-banner')) {
              const banner = document.createElement('div');
              banner.id = 'maint-admin-banner';
              banner.style.cssText = 'position:fixed;bottom:0;left:0;right:0;z-index:9999;background:rgba(180,0,30,0.95);backdrop-filter:blur(8px);border-top:1px solid rgba(255,80,80,0.4);padding:.55rem 1rem;display:flex;align-items:center;justify-content:space-between;gap:1rem;font-size:.78rem;font-family:Inter,sans-serif;color:#fff;';
              banner.innerHTML = `
                <span style="flex-shrink:0;">⚠️ <strong>Режим обслуживания включён</strong></span>
                <div style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap;justify-content:flex-end;">
                  <span style="font-size:.68rem;opacity:.7;flex-shrink:0;">Продлить:</span>
                  <button onclick="_maintExtend(15)" style="background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.25);border-radius:.3rem;color:#fff;padding:.2rem .5rem;cursor:pointer;font-size:.68rem;">+15м</button>
                  <button onclick="_maintExtend(30)" style="background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.25);border-radius:.3rem;color:#fff;padding:.2rem .5rem;cursor:pointer;font-size:.68rem;">+30м</button>
                  <button onclick="_maintExtend(60)" style="background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.25);border-radius:.3rem;color:#fff;padding:.2rem .5rem;cursor:pointer;font-size:.68rem;">+1ч</button>
                  <a href="/maintenance.html" target="_blank" style="color:rgba(255,255,255,.7);text-decoration:none;font-size:.68rem;padding:.2rem .5rem;border:1px solid rgba(255,255,255,.2);border-radius:.3rem;">👁</a>
                  <button onclick="(async()=>{await fetch('/api/admin/settings',{method:'PUT',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:'maintenance_mode',value:'false'})});location.reload();})()" style="background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.3);border-radius:.3rem;color:#fff;padding:.2rem .6rem;cursor:pointer;font-size:.68rem;">✕ Выкл</button>
                </div>`;
              document.body.appendChild(banner);
              window._maintExtend = async (min) => {
                try {
                  const r = await fetch('/api/admin/maintenance/extend', {
                    method:'POST', credentials:'include',
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({minutes: min})
                  });
                  if (r.ok) {
                    const d = await r.json();
                    const t = new Date(d.new_end).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
                    const span = banner.querySelector('span');
                    if(span) span.textContent = `⚠️ Обслуживание · конец в ${t}`;
                  }
                } catch {}
              };
            }
          } catch {}
        }
      }
    }
  } catch {}
})();

function _dateOpts() {
  const m = {
    'dd.mm.yyyy':   {day:'2-digit',month:'2-digit',year:'numeric'},
    'dd.mm.yy':     {day:'2-digit',month:'2-digit',year:'2-digit'},
    'd mmm yyyy':   {day:'numeric',month:'short',year:'numeric'},
    'd mmmm yyyy':  {day:'numeric',month:'long',year:'numeric'},
  };
  return { timeZone: window.__TZ || 'Europe/Moscow', ...(m[window.__DATEFMT] || m['dd.mm.yyyy']) };
}

function fmtDate(iso)     { if (!iso) return '—'; return _toDate(iso).toLocaleDateString('ru-RU', _dateOpts()); }
function fmtDateTime(iso) { if (!iso) return '—'; return _toDate(iso).toLocaleString('ru-RU', { ..._dateOpts(), hour:'2-digit', minute:'2-digit', hour12: !!(window.__H12) }); }

/* Admin role badge — shows for any staff tier (moderator/admin/superadmin), not just
   literally "admin". Before this, an account migrated to "superadmin" (the real site
   owner, post role-tiers migration) would render NO badge anywhere this is used
   (comments, news authorship, profile) — a regression the 3-tier role system would
   otherwise have introduced silently. */
function _renderAdminBadge(u) {
  if (!STAFF_ROLES.includes(u.role || u.type)) return '';
  const _roleDefaultLabel = { superadmin: 'Суперадмин', admin: 'Админ', moderator: 'Модератор' };
  const label = esc(u.admin_title || _roleDefaultLabel[u.role] || 'Админ');
  const icon  = u.badge_icon_url;
  const style = u.badge_style || 'default';
  const B = 'display:inline-flex;align-items:center;gap:.28rem;font-size:.55rem;font-weight:700;letter-spacing:.06em;padding:.12rem .52rem;white-space:nowrap;';
  if (icon) return `<span style="${B}border-radius:9999px;background:rgba(180,130,0,0.18);border:1px solid rgba(210,165,0,0.45);color:#f0c040;"><img src="${esc(icon)}" alt="" style="width:12px;height:12px;object-fit:contain;border-radius:2px;flex-shrink:0;">${label}</span>`;
  switch (style) {
    case 'crown':   return `<span style="${B}border-radius:9999px;background:linear-gradient(135deg,rgba(200,150,0,.28),rgba(255,195,0,.14));border:1px solid rgba(225,175,0,.62);color:#f5c842;box-shadow:0 0 9px rgba(200,150,0,.18);"><svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" style="flex-shrink:0;margin-bottom:1px;"><path d="M5 16 3 5l5.5 5L12 4l3.5 6L21 5l-2 11H5zm2 3a1 1 0 0 0 0 2h10a1 1 0 0 0 0-2H7z"/></svg>${label}</span>`;
    case 'shield':  return `<span style="${B}border-radius:.42rem;background:linear-gradient(135deg,rgba(160,0,30,.32),rgba(90,0,15,.18));border:1px solid rgba(205,0,42,.52);color:#f87171;"><svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" style="flex-shrink:0;"><path d="M12 1 3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4z"/></svg>${label}</span>`;
    case 'diamond': return `<span style="${B}border-radius:9999px;background:linear-gradient(135deg,rgba(110,0,210,.3),rgba(155,0,255,.15));border:1px solid rgba(165,0,255,.48);color:#c4b5fd;box-shadow:0 0 8px rgba(130,0,210,.15);">✦ ${label}</span>`;
    case 'flame':   return `<span style="${B}border-radius:.42rem;background:linear-gradient(135deg,rgba(200,55,0,.3),rgba(255,125,0,.14));border:1px solid rgba(225,80,0,.52);color:#fb923c;">🔥 ${label}</span>`;
    case 'swords':  return `<span style="${B}border-radius:.42rem;background:linear-gradient(135deg,rgba(25,25,60,.55),rgba(50,50,100,.32));border:1px solid rgba(100,100,185,.42);color:#a5b4fc;">⚔ ${label}</span>`;
    default:        return `<span style="${B}border-radius:9999px;background:rgba(180,130,0,0.18);border:1px solid rgba(210,165,0,0.45);color:#f0c040;">${label}</span>`;
  }
}

/* Online status helper */
function _statusInfo(iso) {
  if (!iso) return { dot:'#374151', label:'Не в сети', color:'#4b3f5c', glow:'' };
  const m = (Date.now() - _toDate(iso).getTime()) / 60000;
  if (m < 5)    return { dot:'#22c55e', label:'Онлайн',          color:'#22c55e', glow:'0 0 7px rgba(34,197,94,.65)' };
  if (m < 60)   { const v = Math.round(m); return { dot:'#f59e0b', label:`Был ${v} мин. назад`, color:'#c9922a', glow:'' }; }
  if (m < 1440) { const h = Math.floor(m/60); const w = h===1?'час':h<5?'часа':'часов'; return { dot:'#6b7280', label:`Был ${h} ${w} назад`,  color:'#6b5878', glow:'' }; }
  const d = Math.floor(m/1440); const w = d===1?'день':d<5?'дня':'дней';
  return { dot:'#374151', label:`Был ${d} ${w} назад`, color:'#4b3f5c', glow:'' };
}

/* Back-to-top button — self-contained, works on any page */
(function() {
  let _btn = null, _visible = false;
  function _init() {
    if (_btn) return;
    _btn = document.createElement('button');
    _btn.id = 'btt-btn';
    _btn.setAttribute('aria-label', 'Наверх');
    _btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg>';
    _btn.style.cssText = 'position:fixed;bottom:1.5rem;left:1.5rem;z-index:9990;width:2.4rem;height:2.4rem;border-radius:50%;background:rgba(10,2,18,0.92);border:1px solid rgba(150,0,28,0.45);color:rgba(180,160,210,0.75);display:none;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,0.5);transition:opacity .25s,transform .25s,border-color .2s,color .2s,box-shadow .2s;backdrop-filter:blur(6px);opacity:0;transform:translateY(8px);';
    _btn.onmouseover = () => { _btn.style.borderColor='rgba(200,0,40,0.65)'; _btn.style.color='#f0e8ff'; _btn.style.boxShadow='0 4px 20px rgba(150,0,28,0.35)'; };
    _btn.onmouseout  = () => { _btn.style.borderColor='rgba(150,0,28,0.45)'; _btn.style.color='rgba(180,160,210,0.75)'; _btn.style.boxShadow='0 4px 16px rgba(0,0,0,0.5)'; };
    _btn.onclick = () => window.scrollTo({ top: 0, behavior: 'smooth' });
    document.body.appendChild(_btn);
    window.addEventListener('scroll', () => {
      const show = window.scrollY > 320;
      if (show === _visible) return;
      _visible = show;
      if (show) {
        _btn.style.display = 'flex';
        requestAnimationFrame(() => { _btn.style.opacity='1'; _btn.style.transform='translateY(0)'; });
      } else {
        _btn.style.opacity='0'; _btn.style.transform='translateY(8px)';
        setTimeout(() => { if (!_visible) _btn.style.display='none'; }, 260);
      }
    }, { passive: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _init);
  else _init();
})();

/* Global Ctrl+K search modal — self-contained, works on any page */
(function() {
  /* Inject CSS */
  const _css = document.createElement('style');
  _css.textContent = [
    '#gs-overlay{box-sizing:border-box;}',
    '.gs-item:focus{background:rgba(150,0,28,0.12)!important;outline:none;}',
    '#gs-results::-webkit-scrollbar{width:4px;}',
    '#gs-results::-webkit-scrollbar-thumb{background:rgba(130,0,24,0.5);border-radius:2px;}',
  ].join('');
  document.head.appendChild(_css);

  let _overlay = null, _input = null, _results = null, _debTimer = null, _open = false;

  /* Build DOM */
  function _build() {
    if (_overlay) return;

    _overlay = document.createElement('div');
    _overlay.id = 'gs-overlay';
    _overlay.style.cssText = 'display:none;position:fixed;inset:0;z-index:10001;background:rgba(0,0,0,0.65);backdrop-filter:blur(4px);opacity:0;transition:opacity .2s;';

    _overlay.innerHTML = `
      <div id="gs-modal" style="position:absolute;top:15%;left:50%;transform:translateX(-50%);width:100%;max-width:540px;padding:0 1rem;box-sizing:border-box;">
        <div style="background:rgba(10,2,18,0.98);border:1px solid rgba(150,0,28,0.4);border-radius:1rem;overflow:hidden;box-shadow:0 24px 64px rgba(0,0,0,0.8);">
          <div style="display:flex;align-items:center;gap:.75rem;padding:.85rem 1.1rem;border-bottom:1px solid rgba(110,0,150,0.18);">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--muted,#9488a8)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            <input id="gs-input" type="text" placeholder="Поиск игроков, новостей, кланов…"
              style="flex:1;background:none;border:none;outline:none;color:#e2d8f0;font-size:.92rem;font-family:'Inter',sans-serif;" autocomplete="off"/>
            <kbd style="font-size:.62rem;color:#9488a8;background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.12);border-radius:.3rem;padding:.1rem .4rem;flex-shrink:0;">Esc</kbd>
          </div>
          <div id="gs-results" style="max-height:360px;overflow-y:auto;padding:.5rem 0;"></div>
          <div style="padding:.5rem 1.1rem;border-top:1px solid rgba(110,0,150,0.12);font-size:.62rem;color:#9488a8;display:flex;gap:1rem;">
            <span>↑↓ навигация</span><span>↵ открыть</span><span>Esc закрыть</span>
          </div>
        </div>
      </div>`;

    document.body.appendChild(_overlay);

    _input   = document.getElementById('gs-input');
    _results = document.getElementById('gs-results');

    /* Close on overlay click (not modal) */
    _overlay.addEventListener('click', function(e) {
      if (!document.getElementById('gs-modal').contains(e.target)) closeGlobalSearch();
    });

    /* Input events */
    _input.addEventListener('input', function() {
      clearTimeout(_debTimer);
      _debTimer = setTimeout(() => _doSearch(_input.value.trim()), 280);
    });

    /* Keyboard nav inside modal */
    _overlay.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') { closeGlobalSearch(); return; }
      const items = Array.from(_results.querySelectorAll('.gs-item'));
      if (!items.length) return;
      const focused = document.activeElement;
      const idx = items.indexOf(focused);
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        items[idx < items.length - 1 ? idx + 1 : 0].focus();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        items[idx > 0 ? idx - 1 : items.length - 1].focus();
      } else if (e.key === 'Enter' && idx >= 0) {
        e.preventDefault();
        items[idx].click();
      }
    });

    _showPlaceholder();
  }

  /* Placeholder state */
  function _showPlaceholder() {
    _results.innerHTML = `<div style="padding:.9rem 1.1rem;font-size:.75rem;color:#9488a8;text-align:center;">Начните вводить для поиска…</div>`;
  }

  /* Spinner */
  function _showSpinner() {
    _results.innerHTML = `<div style="padding:.9rem 1.1rem;font-size:.75rem;color:#9488a8;text-align:center;display:flex;align-items:center;justify-content:center;gap:.5rem;">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#9488a8" stroke-width="2" stroke-linecap="round"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur=".75s" repeatCount="indefinite"/></path></svg>
      Поиск…</div>`;
  }

  /* Category header */
  function _catHeader(label) {
    const d = document.createElement('div');
    d.style.cssText = 'font-size:.58rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:rgba(200,0,42,0.6);padding:.35rem 1.1rem .15rem;';
    d.textContent = label;
    return d;
  }

  /* Single result item */
  function _item(href, icon, title, subtitle) {
    const d = document.createElement('div');
    d.className = 'gs-item';
    d.dataset.href = href;
    d.tabIndex = 0;
    d.style.cssText = 'display:flex;align-items:center;gap:.75rem;padding:.55rem 1.1rem;cursor:pointer;transition:background .12s;';
    d.innerHTML = `<span style="font-size:.88rem;flex-shrink:0;">${icon}</span>
      <div style="min-width:0;">
        <div style="font-size:.82rem;font-weight:600;color:#e2d8f0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(title)}</div>
        <div style="font-size:.65rem;color:#9488a8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(subtitle)}</div>
      </div>`;
    d.addEventListener('mouseover', () => { d.style.background = 'rgba(150,0,28,0.12)'; });
    d.addEventListener('mouseout',  () => { d.style.background = ''; });
    d.addEventListener('click', () => { location.href = href; closeGlobalSearch(); });
    d.addEventListener('keydown', function(e) { if (e.key === 'Enter') { e.preventDefault(); d.click(); } });
    return d;
  }

  /* Main search */
  async function _doSearch(q) {
    if (q.length < 2) { _showPlaceholder(); return; }
    _showSpinner();

    const fetches = [
      fetch(`/api/users?search=${encodeURIComponent(q)}&limit=5`).then(r => r.ok ? r.json() : null).catch(() => null),
      fetch(`/api/news?search=${encodeURIComponent(q)}&limit=5`).then(r => r.ok ? r.json() : null).catch(() => null),
      fetch(`/api/clans?search=${encodeURIComponent(q)}&limit=5`).then(r => r.ok ? r.json() : null).catch(() => null),
    ];

    const [usersRes, newsRes, clansRes] = await Promise.all(fetches);

    /* Normalise to arrays */
    const users  = Array.isArray(usersRes)       ? usersRes       : (usersRes  && Array.isArray(usersRes.users))  ? usersRes.users  : [];
    const news   = Array.isArray(newsRes)        ? newsRes        : (newsRes   && Array.isArray(newsRes.news))    ? newsRes.news    : [];
    const clans  = Array.isArray(clansRes)       ? clansRes       : (clansRes  && Array.isArray(clansRes.clans))  ? clansRes.clans  : [];

    _results.innerHTML = '';
    let total = 0;

    if (users.length) {
      _results.appendChild(_catHeader('Игроки'));
      users.forEach(u => {
        const sub = u.role && u.role !== 'user' ? u.role : (u.playtime ? `${u.playtime} ч` : '');
        _results.appendChild(_item(`/user.html?u=${encodeURIComponent(u.username || u.name || '')}`, '👤', u.username || u.name || '', sub));
        total++;
      });
    }

    if (news.length) {
      _results.appendChild(_catHeader('Новости'));
      news.forEach(n => {
        const sub = n.published_at || n.created_at ? fmtDate(n.published_at || n.created_at) : '';
        const slug = n.slug || n.id || '';
        _results.appendChild(_item(`/?news=${encodeURIComponent(slug)}`, '📰', n.title || '', sub));
        total++;
      });
    }

    if (clans.length) {
      _results.appendChild(_catHeader('Кланы'));
      clans.forEach(c => {
        const label = (c.tag ? `[${c.tag}] ` : '') + (c.name || '');
        const sub   = c.member_count != null ? `${c.member_count} участников` : '';
        _results.appendChild(_item('/clans.html', '🛡', label, sub));
        total++;
      });
    }

    if (total === 0) {
      _results.innerHTML = `<div style="padding:.9rem 1.1rem;font-size:.75rem;color:#9488a8;text-align:center;">Ничего не найдено</div>`;
    }
  }

  /* Public open/close */
  window.openGlobalSearch = function() {
    _build();
    _input.value = '';
    _showPlaceholder();
    _overlay.style.display = 'block';
    requestAnimationFrame(() => { _overlay.style.opacity = '1'; });
    setTimeout(() => _input.focus(), 50);
    _open = true;
  };

  window.closeGlobalSearch = function() {
    if (!_overlay) return;
    _overlay.style.opacity = '0';
    setTimeout(() => { _overlay.style.display = 'none'; }, 210);
    _open = false;
  };

  /* Global keyboard: Ctrl+K / Cmd+K to open, Escape to close */
  document.addEventListener('keydown', function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      if (_open) closeGlobalSearch(); else openGlobalSearch();
    } else if (e.key === 'Escape' && _open) {
      closeGlobalSearch();
    }
  });
})();

/* Toast notification — self-contained, works on any page */
function showToast(msg, type = 'info', duration = 4500) {
  let wrap = document.getElementById('toast-wrap') || document.getElementById('toast-container');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'toast-wrap';
    wrap.style.cssText = 'position:fixed;bottom:1.25rem;right:1.25rem;z-index:10000;display:flex;flex-direction:column;gap:.5rem;align-items:flex-end;pointer-events:none;';
    document.body.appendChild(wrap);
  }
  const colors = {
    success: { bg:'rgba(0,70,15,.92)',  border:'rgba(0,160,35,.5)',   color:'#86efac', icon:'✔' },
    error:   { bg:'rgba(120,0,15,.92)', border:'rgba(200,0,30,.5)',   color:'#fca5a5', icon:'✘' },
    info:    { bg:'rgba(20,5,30,.92)',  border:'rgba(150,0,28,.4)',   color:'#d4c4e0', icon:'ℹ' },
    warning: { bg:'rgba(90,55,0,.92)', border:'rgba(200,130,0,.5)',  color:'#fcd34d', icon:'⚠' },
    achievement: { bg:'rgba(40,24,0,.95)', border:'rgba(201,169,74,.55)', color:'#e8cf8a', icon:'✦' },
  };
  const c = colors[type] || colors.info;
  const el = document.createElement('div');
  el.style.cssText = `pointer-events:auto;display:flex;align-items:center;gap:.55rem;padding:.65rem 1rem;border-radius:.55rem;background:${c.bg};border:1px solid ${c.border};color:${c.color};font-size:.82rem;font-weight:600;box-shadow:0 8px 24px rgba(0,0,0,.5);max-width:320px;animation:toast-in .25s ease;`;
  el.innerHTML = `<span style="flex-shrink:0;">${c.icon}</span><span>${esc(msg)}</span>`;
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .25s, transform .25s';
    el.style.opacity = '0';
    el.style.transform = 'translateX(12px)';
    setTimeout(() => el.remove(), 280);
  }, duration);
}
