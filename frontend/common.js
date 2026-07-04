/* common.js — shared utilities for all pages */

// Inject toast-in animation if not already present
if (!document.getElementById('common-toast-styles')) {
  const _s = document.createElement('style');
  _s.id = 'common-toast-styles';
  _s.textContent = '@keyframes toast-in{from{opacity:0;transform:translateX(10px)}to{opacity:1;transform:translateX(0)}}';
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

/* Auto-load timezone / date settings from admin panel once at startup */
(async function _initSettings() {
  if (window.__settingsLoaded) return;
  try {
    const s = await fetch('/api/settings/public').then(r => r.ok ? r.json() : null);
    if (s) {
      window.__TZ      = s.timezone     || 'Europe/Moscow';
      window.__H12     = (s.time_format || '24h') === '12h';
      window.__DATEFMT = s.date_format  || 'dd.mm.yyyy';
      window.__settingsLoaded = true;
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

/* Admin role badge */
function _renderAdminBadge(u) {
  if ((u.role || u.type) !== 'admin') return '';
  const label = esc(u.admin_title || 'Админ');
  const icon  = u.badge_icon_url;
  const style = u.badge_style || 'default';
  const B = 'display:inline-flex;align-items:center;gap:.28rem;font-size:.55rem;font-weight:700;letter-spacing:.06em;padding:.12rem .52rem;white-space:nowrap;';
  if (icon) return `<span style="${B}border-radius:9999px;background:rgba(180,130,0,0.18);border:1px solid rgba(210,165,0,0.45);color:#f0c040;"><img src="${esc(icon)}" style="width:12px;height:12px;object-fit:contain;border-radius:2px;flex-shrink:0;">${label}</span>`;
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
