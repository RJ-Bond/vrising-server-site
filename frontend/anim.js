// frontend/anim.js — shared Anime.js entrance-animation helper (frontend/anime.min.js,
// window.anime global). The site had the same one-shot "fade + rise in on load" effect
// copy-pasted as a @keyframes block in several places (components.css `fade-up`,
// index.css `teamCardIn`/`news-card-appear`, clans.html `clan-in`, bans.html `ban-in`,
// leaderboard.html podium/rank-in, admin.html `row-in`, …). animateEntrance() gives new/
// touched call sites a single staggered-entrance helper instead of pasting another
// @keyframes block — existing CSS keyframe usages are left as-is (not a mandate to
// migrate every animation site-wide).
(function () {
  window.animateEntrance = function animateEntrance(targets, opts = {}) {
    if (typeof anime === 'undefined') return null;
    const els = typeof targets === 'string' ? document.querySelectorAll(targets) : targets;
    if (!els || !('length' in els) || !els.length) return null;
    // Start from the CSS "fade-up" resting state (opacity 0, translateY below final
    // position) so there's no flash-of-unstyled-content before Anime.js takes over.
    Array.prototype.forEach.call(els, (el) => {
      el.style.opacity = 0;
      el.style.transform = `translateY(${opts.distance ?? 14}px)`;
    });
    return anime({
      targets: els,
      opacity: [0, 1],
      translateY: [opts.distance ?? 14, 0],
      duration: opts.duration ?? 380,
      delay: anime.stagger(opts.stagger ?? 60, { start: opts.delayStart ?? 0 }),
      easing: opts.easing ?? 'easeOutCubic',
    });
  };
})();
