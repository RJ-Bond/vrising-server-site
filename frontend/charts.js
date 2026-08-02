// frontend/charts.js — shared Chart.js wrapper for the site's player-count
// line charts. Wraps Chart.js (frontend/chart.min.js, window.Chart global)
// with the dark theme + a small wipe-annotation plugin so the previously
// duplicated hand-rolled canvas implementations (index.js mini sparklines,
// servers.html graph + compare chart, admin.html dashboard graph) can share
// one renderer instead of five near-identical quadratic-curve drawers.
//
// renderPlayerChart(canvas, series, opts) draws one or more { ts, players,
// online } snapshot series (the shape returned by GET /api/monitor/snapshots)
// as a themed line chart with a gradient fill, offline markers, a themed
// tooltip and optional dashed wipe-date annotations.
//
// renderBarChart(canvas, series, opts) draws one or two categorical bar
// series ({ x: label, y: number } points) sharing the same dark theme/
// tooltip styling — added for the admin registrations-per-day chart, the
// points-economy dashboard (issued vs spent, same unit → one axis, legit
// grouped bars, not a dual-axis chart), and public per-player activity
// trends. Bars, not a line, since each x is a discrete category (a day),
// not a continuously-sampled measurement the way snapshot polling is.

(function () {
  if (typeof Chart === 'undefined') return;

  // Draws dashed vertical lines (server wipes) over the chart, registered
  // once and toggled per-chart via the `wipeAnnotations` plugin option.
  const wipeAnnotationsPlugin = {
    id: 'wipeAnnotations',
    afterDraw(chart, _args, pluginOpts) {
      const wipes = (chart.options.plugins.wipeAnnotations || pluginOpts || {}).wipes;
      if (!wipes || !wipes.length) return;
      const { ctx, chartArea, scales } = chart;
      if (!chartArea || !scales.x) return;
      ctx.save();
      wipes.forEach((w) => {
        const x = scales.x.getPixelForValue(w.ts);
        if (x < chartArea.left || x > chartArea.right) return;
        const color = w.color || 'rgba(200,0,42,0.7)';
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(x, chartArea.top);
        ctx.lineTo(x, chartArea.bottom);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(x, chartArea.top + 4, 3, 0, Math.PI * 2);
        ctx.fill();
      });
      ctx.restore();
    },
  };
  Chart.register(wipeAnnotationsPlugin);

  function fmtChartTs(ts, mode) {
    const d = new Date(ts * 1000);
    const tz = window.__TZ || 'Europe/Moscow';
    const hour12 = !!window.__H12;
    return mode === 'time'
      ? d.toLocaleTimeString('ru-RU', { timeZone: tz, hour12, hour: '2-digit', minute: '2-digit' })
      : d.toLocaleDateString('ru-RU', { timeZone: tz, day: '2-digit', month: '2-digit' });
  }

  function playerWord(n) {
    return n === 1 ? 'игрок' : (n >= 2 && n <= 4) ? 'игрока' : 'игроков';
  }

  /**
   * @param {HTMLCanvasElement} canvas
   * @param {Array|Object} series - one series or an array of series, each
   *   { snapshots: [{ts,players,online}], color, rgb, label }
   * @param {Object} [opts]
   *   - wipes: [{ts, color}] dashed vertical annotations
   *   - showLegend: bool (default false)
   *   - minimal: bool - hide axes/grid for tiny widget sparklines (default false)
   *   - xTicks: max number of x-axis ticks (default 6)
   * @returns {Chart|null} the Chart.js instance, or null if there's no data
   */
  window.renderPlayerChart = function renderPlayerChart(canvas, series, opts = {}) {
    if (!canvas || typeof Chart === 'undefined') return null;
    const list = Array.isArray(series) ? series : [series];
    const nonEmpty = list.filter((s) => s.snapshots && s.snapshots.length >= 2);

    if (canvas._chart) {
      canvas._chart.destroy();
      canvas._chart = null;
    }
    if (!nonEmpty.length) {
      canvas.style.display = 'none';
      return null;
    }
    canvas.style.display = 'block';

    const allTs = nonEmpty.flatMap((s) => s.snapshots.map((p) => p.ts));
    const tMin = Math.min(...allTs);
    const tMax = Math.max(...allTs);
    const tSpan = Math.max(1, tMax - tMin);
    const timeMode = opts.timeMode || (tSpan <= 86400 * 1.5 ? 'time' : 'date');
    const minimal = !!opts.minimal;

    const datasets = nonEmpty.map((s) => {
      const color = s.color || '#c8002a';
      const rgb = s.rgb || '200,0,42';
      return {
        label: s.label || 'Игроков',
        data: s.snapshots.map((p) => ({ x: p.ts, y: p.players, online: p.online !== false })),
        borderColor: color,
        backgroundColor(ctx) {
          const { chart } = ctx;
          if (!chart.chartArea) return 'transparent';
          const g = chart.ctx.createLinearGradient(0, chart.chartArea.top, 0, chart.chartArea.bottom);
          g.addColorStop(0, `rgba(${rgb},${minimal ? 0.25 : 0.35})`);
          g.addColorStop(1, `rgba(${rgb},0.02)`);
          return g;
        },
        fill: true,
        tension: 0.35,
        borderWidth: minimal ? 1.5 : 2,
        pointRadius: (pctx) => (pctx.raw && pctx.raw.online === false ? 2 : 0),
        pointBackgroundColor: (pctx) => (pctx.raw && pctx.raw.online === false ? '#f87171' : color),
        pointHoverRadius: minimal ? 3 : 4,
        pointHitRadius: 12,
      };
    });

    canvas._chart = new Chart(canvas, {
      type: 'line',
      data: { datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        // Charts here are destroyed+recreated on every poll refresh / period
        // switch (not `chart.update()`), so Chart.js's default "grow up from
        // the zero baseline" entrance animation would replay every ~30s —
        // disabled for a stable redraw instead of a recurring flicker.
        animation: false,
        interaction: { mode: 'nearest', intersect: false, axis: 'x' },
        plugins: {
          legend: {
            display: !!opts.showLegend,
            labels: { color: '#c9bcd6', boxWidth: 10, font: { size: 10 } },
          },
          wipeAnnotations: { wipes: opts.wipes || [] },
          tooltip: {
            enabled: !minimal || opts.tooltip !== false,
            backgroundColor: 'rgba(15,8,20,0.92)',
            borderColor: 'rgba(200,0,42,0.4)',
            borderWidth: 1,
            titleColor: '#ece3f7',
            bodyColor: '#d4c4e0',
            padding: 8,
            displayColors: nonEmpty.length > 1,
            callbacks: {
              title: (items) =>
                items.length
                  ? new Date(items[0].parsed.x * 1000).toLocaleString('ru-RU', {
                      timeZone: window.__TZ || 'Europe/Moscow',
                      hour12: !!window.__H12,
                      day: '2-digit',
                      month: '2-digit',
                      hour: '2-digit',
                      minute: '2-digit',
                    })
                  : '',
              label: (item) => {
                const raw = item.raw;
                const off = raw.online === false ? ' (офлайн)' : '';
                const prefix = nonEmpty.length > 1 ? `${item.dataset.label}: ` : '';
                return `${prefix}${raw.y} ${playerWord(raw.y)}${off}`;
              },
            },
          },
        },
        scales: {
          x: {
            type: 'linear',
            min: tMin,
            max: tMax,
            display: !minimal,
            grid: { color: 'rgba(255,255,255,0.04)' },
            ticks: {
              color: 'rgba(150,130,170,0.6)',
              font: { size: 9 },
              maxTicksLimit: opts.xTicks || 6,
              callback: (v) => fmtChartTs(v, timeMode),
            },
          },
          y: {
            display: !minimal,
            beginAtZero: true,
            grid: { color: 'rgba(255,255,255,0.06)' },
            ticks: { color: 'rgba(150,130,170,0.6)', font: { size: 9 }, precision: 0, maxTicksLimit: 4 },
          },
        },
      },
    });
    return canvas._chart;
  };

  /**
   * @param {HTMLCanvasElement} canvas
   * @param {Array|Object} series - one series or an array of (1-2) series,
   *   each { data: [{x: string, y: number}], label, color, rgb }. All series
   *   must share the same x categories (same unit/axis — e.g. "issued" vs
   *   "spent" points per day), matched by array index, not re-sorted.
   * @param {Object} [opts]
   *   - showLegend: bool (default: series.length > 1)
   *   - xTicks: max number of x-axis ticks (default 8)
   *   - valueLabel: unit word appended in the tooltip (e.g. "очков", "ч")
   *   - valueFormatter: (n) => string, overrides the default tooltip number format
   * @returns {Chart|null} the Chart.js instance, or null if there's no data
   */
  window.renderBarChart = function renderBarChart(canvas, series, opts = {}) {
    if (!canvas || typeof Chart === 'undefined') return null;
    const list = Array.isArray(series) ? series : [series];
    const nonEmpty = list.filter((s) => s.data && s.data.length);

    if (canvas._chart) {
      canvas._chart.destroy();
      canvas._chart = null;
    }
    if (!nonEmpty.length) {
      canvas.style.display = 'none';
      return null;
    }
    canvas.style.display = 'block';

    const labels = nonEmpty[0].data.map((p) => p.x);
    const fmtValue = opts.valueFormatter || ((n) => String(n));

    const datasets = nonEmpty.map((s) => {
      const color = s.color || '#c8002a';
      const rgb = s.rgb || '200,0,42';
      return {
        label: s.label || '',
        data: s.data.map((p) => p.y),
        backgroundColor: `rgba(${rgb},0.55)`,
        hoverBackgroundColor: `rgba(${rgb},0.8)`,
        borderColor: color,
        borderWidth: 1,
        borderRadius: 3,
        borderSkipped: false,
        maxBarThickness: 28,
      };
    });

    canvas._chart = new Chart(canvas, {
      type: 'bar',
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: {
            display: opts.showLegend !== undefined ? !!opts.showLegend : nonEmpty.length > 1,
            labels: { color: '#c9bcd6', boxWidth: 10, font: { size: 10 } },
          },
          tooltip: {
            backgroundColor: 'rgba(15,8,20,0.92)',
            borderColor: 'rgba(200,0,42,0.4)',
            borderWidth: 1,
            titleColor: '#ece3f7',
            bodyColor: '#d4c4e0',
            padding: 8,
            displayColors: nonEmpty.length > 1,
            callbacks: {
              label: (item) => {
                const prefix = nonEmpty.length > 1 ? `${item.dataset.label}: ` : '';
                const unit = opts.valueLabel ? ` ${opts.valueLabel}` : '';
                return `${prefix}${fmtValue(item.raw)}${unit}`;
              },
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: {
              color: 'rgba(150,130,170,0.6)',
              font: { size: 9 },
              maxTicksLimit: opts.xTicks || 8,
              maxRotation: 0,
              autoSkip: true,
            },
          },
          y: {
            beginAtZero: true,
            grid: { color: 'rgba(255,255,255,0.06)' },
            ticks: { color: 'rgba(150,130,170,0.6)', font: { size: 9 }, precision: 0, maxTicksLimit: 4 },
          },
        },
      },
    });
    return canvas._chart;
  };
})();
