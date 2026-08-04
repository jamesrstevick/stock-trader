(function () {
  'use strict';

  // Bumped when trader logs events / syncs positions; Trader+Log poll uses this.
  var lastContentRev = null;

  function apiBase() {
    return (window.TRADER_API_BASE || '').replace(/\/$/, '');
  }

  function useMock() {
    return !!window.TRADER_USE_MOCK;
  }

  async function fetchJson(path) {
    if (useMock()) {
      var mockPath = '/static/mock' + path.replace('/api', '') + '.json';
      if (path.indexOf('/api/events') === 0) mockPath = '/static/mock/events.json';
      if (path.indexOf('/api/log') === 0) mockPath = '/static/mock/log.json';
      if (path.indexOf('/api/performance') === 0) mockPath = '/static/mock/performance.json';
      var mr = await fetch(mockPath);
      if (!mr.ok) throw new Error('Mock missing: ' + mockPath);
      var mockData = await mr.json();
      if (path.indexOf('/api/performance') === 0) {
        return clipMockPerformance(mockData, path);
      }
      return mockData;
    }
    var r = await fetch(apiBase() + path);
    if (!r.ok) throw new Error(path + ' → ' + r.status);
    return r.json();
  }

  var PERF_RANGE_DAYS = { '1D': 1, '1W': 7, '1M': 30, '3M': 91, '6M': 182, '1Y': 365 };
  var perfRange = '1M';
  var perfChart = null;

  function clipMockPerformance(full, path) {
    var range = '1M';
    var m = /[?&]range=([^&]+)/.exec(path);
    if (m) range = decodeURIComponent(m[1]).toUpperCase();
    var days = PERF_RANGE_DAYS[range] || 30;
    var pts = (full.points || []).slice();
    if (!pts.length) {
      return Object.assign({}, full, { range: range, range_days: days, ok: false });
    }
    var end = pts[pts.length - 1].date;
    var endDate = new Date(end + 'T12:00:00');
    var startDate = new Date(endDate.getTime());
    startDate.setDate(startDate.getDate() - Math.max(days - 1, 0));
    var startIso = startDate.toISOString().slice(0, 10);
    var algoStart = (full.algorithm_start || '').slice(0, 10);
    if (algoStart && algoStart > startIso) startIso = algoStart;
    var before = pts.filter(function (p) { return p.date < startIso; });
    var clipped = pts.filter(function (p) { return p.date >= startIso; });
    if (before.length && clipped.length) {
      clipped = [before[before.length - 1]].concat(clipped);
    } else if (!clipped.length) {
      clipped = before.length ? [before[before.length - 1]] : [pts[pts.length - 1]];
    }
    var basePort = clipped[0].equity;
    var baseSpy = null;
    for (var i = 0; i < clipped.length; i++) {
      if (clipped[i].spy != null) { baseSpy = clipped[i].spy; break; }
    }
    var renorm = clipped.map(function (p) {
      var portPct = basePort ? ((p.equity / basePort) - 1) * 100 : null;
      var spyPct = (baseSpy != null && p.spy != null) ? ((p.spy / baseSpy) - 1) * 100 : null;
      return {
        date: p.date,
        equity: p.equity,
        spy: p.spy,
        portfolio_pct: portPct,
        spy_pct: spyPct
      };
    });
    var last = renorm[renorm.length - 1];
    return {
      ok: true,
      range: range,
      range_days: days,
      algorithm_start: full.algorithm_start,
      window_start: renorm[0].date,
      window_end: last.date,
      clamped: !!algoStart && startIso === algoStart && days > renorm.length,
      portfolio_return_pct: last.portfolio_pct,
      spy_return_pct: last.spy_pct,
      points: renorm,
      note: full.note || ''
    };
  }

  function returnClass(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return 'flat';
    var n = Number(v);
    if (n > 0) return 'pos';
    if (n < 0) return 'neg';
    return 'flat';
  }

  function renderPerfReturns(data) {
    var box = document.getElementById('perf-returns');
    if (!box) return;
    if (!data || data.portfolio_return_pct == null) {
      box.innerHTML = '';
      return;
    }
    box.innerHTML =
      '<div class="perf-return">' +
        '<span class="label">Portfolio</span>' +
        '<span class="value ' + returnClass(data.portfolio_return_pct) + '">' +
          escapeHtml(pct(data.portfolio_return_pct)) +
        '</span>' +
      '</div>' +
      '<div class="perf-return">' +
        '<span class="label">S&amp;P 500 (SPY)</span>' +
        '<span class="value ' + returnClass(data.spy_return_pct) + '">' +
          escapeHtml(pct(data.spy_return_pct)) +
        '</span>' +
      '</div>';
  }

  function renderPerfChart(data) {
    var canvas = document.getElementById('perf-chart');
    var empty = document.getElementById('perf-empty');
    var meta = document.getElementById('perf-meta');
    if (!canvas) return;
    var points = (data && data.points) || [];
    if (!points.length || (data && data.ok === false && !points.length)) {
      if (perfChart) {
        perfChart.destroy();
        perfChart = null;
      }
      canvas.style.display = 'none';
      if (empty) {
        empty.hidden = false;
        empty.textContent = (data && data.error) || 'No performance data yet. Mark algorithm start and let daily closes accumulate.';
      }
      if (meta) meta.textContent = '';
      renderPerfReturns(null);
      return;
    }
    if (empty) empty.hidden = true;
    canvas.style.display = 'block';
    if (meta) {
      var bits = [];
      if (data.window_start && data.window_end) {
        bits.push(data.window_start + ' → ' + data.window_end);
      }
      if (data.clamped) bits.push('clamped to elapsed time since start');
      bits.push((points.length) + ' daily points');
      meta.textContent = bits.join(' · ');
    }
    renderPerfReturns(data);

    var labels = points.map(function (p) { return p.date; });
    var port = points.map(function (p) { return p.portfolio_pct; });
    var spy = points.map(function (p) { return p.spy_pct; });

    if (typeof Chart === 'undefined') {
      if (empty) {
        empty.hidden = false;
        empty.textContent = 'Chart library failed to load.';
      }
      return;
    }

    if (perfChart) {
      perfChart.data.labels = labels;
      perfChart.data.datasets[0].data = port;
      perfChart.data.datasets[1].data = spy;
      perfChart.update();
      return;
    }

    perfChart = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {
        labels: labels,
        datasets: [
          {
            label: 'Portfolio',
            data: port,
            borderColor: '#0f6e56',
            backgroundColor: 'rgba(15, 110, 86, 0.12)',
            borderWidth: 2,
            pointRadius: points.length <= 14 ? 3 : 0,
            pointHoverRadius: 4,
            tension: 0.2,
            fill: false
          },
          {
            label: 'S&P 500 (SPY)',
            data: spy,
            borderColor: '#9a3412',
            backgroundColor: 'transparent',
            borderWidth: 2,
            borderDash: [5, 4],
            pointRadius: points.length <= 14 ? 3 : 0,
            pointHoverRadius: 4,
            tension: 0.2,
            fill: false
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            position: 'top',
            labels: { boxWidth: 12, font: { family: 'IBM Plex Sans' } }
          },
          tooltip: {
            callbacks: {
              label: function (ctx) {
                var v = ctx.parsed.y;
                if (v == null || isNaN(v)) return ctx.dataset.label + ': —';
                return ctx.dataset.label + ': ' + (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
              }
            }
          }
        },
        scales: {
          x: {
            ticks: {
              maxRotation: 0,
              autoSkip: true,
              maxTicksLimit: 8,
              font: { size: 11 }
            },
            grid: { color: 'rgba(213, 208, 196, 0.5)' }
          },
          y: {
            ticks: {
              callback: function (v) { return (v >= 0 ? '+' : '') + v + '%'; },
              font: { size: 11 }
            },
            grid: { color: 'rgba(213, 208, 196, 0.5)' }
          }
        }
      }
    });
  }

  async function loadPerformance() {
    var ranges = document.getElementById('perf-ranges');
    try {
      var data = await fetchJson('/api/performance?range=' + encodeURIComponent(perfRange));
      renderPerfChart(data);
    } catch (e) {
      renderPerfChart({ ok: false, error: 'Could not load performance: ' + e.message, points: [] });
    }
    if (ranges) {
      ranges.querySelectorAll('button').forEach(function (btn) {
        btn.classList.toggle('active', btn.getAttribute('data-range') === perfRange);
      });
    }
  }

  function money(v) {
    if (v === null || v === undefined || v === '') return '—';
    var n = Number(v);
    if (isNaN(n)) return '—';
    return n.toLocaleString(undefined, { style: 'currency', currency: 'USD' });
  }

  function pct(v) {
    if (v === null || v === undefined || v === '') return '—';
    var n = Number(v);
    if (isNaN(n)) return '—';
    return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
  }

  function timeAgo(iso) {
    if (!iso) return '—';
    var t = Date.parse(iso);
    if (isNaN(t)) return String(iso);
    var sec = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (sec < 60) return 'just now';
    var min = Math.round(sec / 60);
    if (min < 60) return min === 1 ? '1 minute ago' : min + ' minutes ago';
    var hr = Math.round(min / 60);
    if (hr < 48) return hr === 1 ? '1 hour ago' : hr + ' hours ago';
    var day = Math.round(hr / 24);
    return day === 1 ? '1 day ago' : day + ' days ago';
  }

  function plClass(v) {
    if (v === null || v === undefined) return 'num';
    var n = Number(v);
    if (isNaN(n) || n === 0) return 'num';
    return 'num ' + (n > 0 ? 'pos' : 'neg');
  }

  function showPage(name) {
    document.querySelectorAll('.page').forEach(function (el) {
      el.classList.toggle('active', el.id === 'page-' + name);
    });
    document.querySelectorAll('nav.side button').forEach(function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-page') === name);
    });
    if (name === 'about') loadAbout();
    if (name === 'trader') loadTrader();
    if (name === 'log') loadLog();
  }

  var refreshLog = document.getElementById('btn-refresh-log');
  if (refreshLog) refreshLog.addEventListener('click', loadLog);

  var perfRanges = document.getElementById('perf-ranges');
  if (perfRanges) {
    perfRanges.addEventListener('click', function (ev) {
      var btn = ev.target.closest('button[data-range]');
      if (!btn) return;
      perfRange = btn.getAttribute('data-range') || '1M';
      loadPerformance();
    });
  }

  // Client fallback when /api/status was started before watchlist_filter existed.
  var FILTER_FALLBACK = {
    active: 'safe',
    count: 2,
    filters: [
      {
        name: 'safe',
        title: 'Safe Giant',
        summary: 'Mega-cap, relatively stable names with value characteristics, analyst upside, and a modest dividend — ranked by analyst upside.',
        ranking: 'Analyst upside ((target_price − current_price) / current_price), highest first',
        criteria: [
          { field: 'market_cap', meaning: 'Total market value of the company (shares outstanding × price)', set_to: '> $25B', why: 'Stick to giant / mega-cap firms that tend to be more liquid and durable' },
          { field: 'beta', meaning: 'Volatility vs the broad market (1.0 ≈ moves with the market)', set_to: '0.0 – 1.3', why: 'Cap high-volatility names; low beta is allowed for safety and diversity' },
          { field: 'short_float', meaning: 'Share of float sold short (0.10 = 10%)', set_to: '< 10%', why: 'Avoid crowded short interest (“no enemies”) that can spike volatility' },
          { field: 'pe_ratio', meaning: 'Price-to-earnings (price per share / earnings per share)', set_to: '0.0 – 35.0', why: 'Require profitability (P/E > 0) while excluding richly priced names' },
          { field: 'peg_ratio', meaning: 'P/E divided by expected earnings growth (growth at a reasonable price)', set_to: 'NULL or 0.0 – 2.0', why: 'Prefer growth that is not overpaying; missing PEG data is still allowed' },
          { field: 'analyst_upside', meaning: 'Implied upside from current price to consensus analyst target', set_to: '> 10%', why: 'Even “safe” names should still have meaningful expected appreciation' },
          { field: 'recommendation_mean', meaning: 'Analyst consensus score (1=Strong Buy … 5=Sell)', set_to: 'NULL or ≤ 2.0', why: 'Bias toward Buy / Strong Buy; missing recommendation is allowed' },
          { field: 'dividend_yield', meaning: 'Annual dividend yield in percentage points (1.0 = 1%)', set_to: '> 1%', why: 'Prefer income support for longer holds while waiting on upside' }
        ],
        disabled: [
          { field: 'fifty_day_average > two_hundred_day_average', meaning: 'Classic “golden cross” uptrend (50-day MA above 200-day MA)', set_to: 'disabled', why: 'Turned off as noisy — too many false signals for this strategy' }
        ]
      },
      {
        name: 'risky',
        title: 'Risky Momentum',
        summary: 'Liquid mid/large caps with high beta and short interest — short-squeeze / momentum candidates ranked by analyst upside.',
        ranking: 'Analyst upside ((target_price − current_price) / current_price), highest first',
        criteria: [
          { field: 'market_cap', meaning: 'Total market value of the company (shares outstanding × price)', set_to: '> $2B', why: 'Stay above micro-caps for basic size and liquidity safety' },
          { field: 'avg_volume', meaning: 'Average daily trading volume in shares', set_to: '> 1M shares/day', why: 'Need enough liquidity to enter and exit without huge slippage' },
          { field: 'current_price', meaning: 'Latest Yahoo Finance price', set_to: '> $5', why: 'Exclude penny stocks that are hard to trade cleanly' },
          { field: 'beta', meaning: 'Volatility vs the broad market (1.0 ≈ moves with the market)', set_to: '> 1.5', why: 'Want names that move more than the market (action / momentum)' },
          { field: 'short_float', meaning: 'Share of float sold short (0.15 = 15%)', set_to: '> 15%', why: 'High short interest is the squeeze-potential signal' },
          { field: 'analyst_upside', meaning: 'Implied upside from current price to consensus analyst target', set_to: '> 20%', why: 'Require a stronger value / upside case to justify the risk' }
        ],
        disabled: []
      }
    ]
  };

  var filterState = {
    active: 'safe',
    viewing: 'safe',
    byName: {},
    list: []
  };

  function normalizeFilterPayload(raw) {
    // New shape: { active, count, filters: [...] }
    if (raw && Array.isArray(raw.filters) && raw.filters.length) {
      return {
        active: String(raw.active || raw.filters[0].name || 'safe').toLowerCase(),
        filters: raw.filters
      };
    }
    // Old shape: single filter object with criteria
    if (raw && (raw.criteria || raw.name)) {
      var one = raw;
      var rest = FILTER_FALLBACK.filters.filter(function (f) {
        return f.name !== one.name;
      });
      return {
        active: String(one.name || FILTER_FALLBACK.active).toLowerCase(),
        filters: [one].concat(rest)
      };
    }
    return {
      active: FILTER_FALLBACK.active,
      filters: FILTER_FALLBACK.filters.slice()
    };
  }

  function findFilter(name) {
    return filterState.byName[name] || filterState.list[0] || null;
  }

  function renderFilterDetail(filter) {
    var title = document.getElementById('filter-title');
    var summary = document.getElementById('filter-summary');
    var ranking = document.getElementById('filter-ranking');
    var tbody = document.querySelector('#filter-table tbody');
    var disabledBox = document.getElementById('filter-disabled');
    if (!title || !tbody) return;

    if (!filter) {
      title.textContent = 'Watchlist filter';
      if (summary) summary.textContent = 'Filter description unavailable.';
      if (ranking) ranking.textContent = '';
      tbody.innerHTML = '<tr><td colspan="4" class="empty">No filter metadata.</td></tr>';
      if (disabledBox) {
        disabledBox.hidden = true;
        disabledBox.innerHTML = '';
      }
      return;
    }

    var label = filter.title || filter.name || 'Watchlist';
    title.textContent = 'Watchlist filter';
    if (summary) summary.textContent = filter.summary || '';
    if (ranking) {
      ranking.textContent = filter.ranking ? 'Ranking: ' + filter.ranking : '';
    }

    tbody.innerHTML = '';
    (filter.criteria || []).forEach(function (c) {
      var tr = document.createElement('tr');
      tr.innerHTML =
        '<td class="field"><code>' + escapeHtml(c.field || '') + '</code></td>' +
        '<td class="wrap">' + escapeHtml(c.meaning || '') + '</td>' +
        '<td class="set-to">' + escapeHtml(c.set_to || '') + '</td>' +
        '<td class="wrap">' + escapeHtml(c.why || '') + '</td>';
      tbody.appendChild(tr);
    });
    if (!(filter.criteria || []).length) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty">No active criteria listed.</td></tr>';
    }

    if (disabledBox) {
      var off = filter.disabled || [];
      if (!off.length) {
        disabledBox.hidden = true;
        disabledBox.innerHTML = '';
      } else {
        disabledBox.hidden = false;
        disabledBox.innerHTML =
          '<h3>Disabled rules</h3>' +
          '<ul>' +
          off.map(function (c) {
            return (
              '<li><code>' + escapeHtml(c.field || '') + '</code> — ' +
              escapeHtml(c.meaning || '') +
              ' <em>(' + escapeHtml(c.set_to || 'disabled') + ')</em>. ' +
              escapeHtml(c.why || '') +
              '</li>'
            );
          }).join('') +
          '</ul>';
      }
    }
  }

  function renderFilterPills() {
    var box = document.getElementById('filter-pills');
    if (!box) return;
    box.innerHTML = '';
    var active = findFilter(filterState.active);
    var viewing = findFilter(filterState.viewing);
    var activeLabel = active
      ? (active.title || active.name) + ' (' + active.name + ')'
      : filterState.active;
    box.appendChild(pill('Active: ' + activeLabel, 'ok'));
    box.appendChild(pill(filterState.list.length + ' filters', ''));
    if (viewing && viewing.name !== filterState.active) {
      box.appendChild(pill('Viewing: ' + (viewing.title || viewing.name), 'warn'));
    }
  }

  function populateFilterSelect() {
    var sel = document.getElementById('filter-select');
    if (!sel) return;
    sel.innerHTML = '';
    filterState.list.forEach(function (f) {
      var opt = document.createElement('option');
      opt.value = f.name;
      opt.textContent = (f.title || f.name) +
        (f.name === filterState.active ? ' — active' : '');
      sel.appendChild(opt);
    });
    sel.value = filterState.viewing;
  }

  function setupFilterPanel(rawPayload) {
    var normalized = normalizeFilterPayload(rawPayload);
    filterState.active = normalized.active;
    filterState.list = normalized.filters;
    filterState.byName = {};
    filterState.list.forEach(function (f) {
      if (f && f.name) filterState.byName[f.name] = f;
    });
    if (!filterState.byName[filterState.viewing]) {
      filterState.viewing = filterState.active;
    }
    if (!filterState.byName[filterState.viewing] && filterState.list.length) {
      filterState.viewing = filterState.list[0].name;
    }
    populateFilterSelect();
    renderFilterPills();
    renderFilterDetail(findFilter(filterState.viewing));
  }

  var filterSelect = document.getElementById('filter-select');
  if (filterSelect) {
    filterSelect.addEventListener('change', function () {
      filterState.viewing = filterSelect.value;
      renderFilterPills();
      renderFilterDetail(findFilter(filterState.viewing));
    });
  }

  var RULES_FALLBACK = [
    { title: 'Dry-run safety net', set_to: 'ON — no Schwab orders', why: 'Paper-trade by default; flip TRADE_DRY_RUN only when ready for real fills' },
    { title: 'Buys and sells only in regular hours', set_to: '9:30–16:00 America/New_York', why: 'Watchlist/buys and sell-check jobs skip when the US equity session is closed' },
    { title: 'No buy-and-sell the same day', set_to: 'Hold at least 16 hours after purchase', why: 'Avoid PDT / same-day round-trips; covers regular + extended hours buffer' },
    { title: 'Cash floor', set_to: 'Keep ≥ $15,000 effective cash', why: 'No buy may leave cash (minus pending buys) below this floor' },
    { title: 'Account size floor', set_to: 'Liquidation value ≥ $25,000', why: 'Trading is blocked entirely while account value is under this threshold' },
    { title: 'Buy only from the active filter / watchlist', set_to: "Filter 'safe'", why: 'New buys must be on the current watchlist — no random tickers' },
    { title: 'Fixed buy size', set_to: '~$1,000 per new name', why: 'Keeps position sizing consistent and cash-floor math simple' },
    { title: 'Re-buy debounce after a sell', set_to: '≤ last sell price − 5%', why: 'Avoid immediately chasing the same name after a sell' },
    { title: 'Trailing stop arms after a gain', set_to: 'Arm at +10% peak unrealized', why: 'Protect winners once they have moved enough; ignore noise before that' },
    { title: 'Trail buffer below peak', set_to: '5% on watchlist · 3% once off', why: 'Stop ratchets up with new highs; tighter once the thesis (watchlist) breaks' },
    { title: 'Hard stop vs cost', set_to: '−10% on watchlist · −5% once off', why: 'Catastrophe floor when a trail never armed, or thesis is gone' },
    { title: 'Resting broker STOP_LIMIT', set_to: 'Limit 0.50% below stop · GOOD_TILL_CANCEL', why: 'Protective sell sits at Schwab even if the bot is offline briefly' },
    { title: 'Sell check cadence', set_to: 'Every 15 minutes (RTH)', why: 'Re-evaluate trails / hard stops while the market is open' },
    { title: 'Legacy holdings are sell-skipped', set_to: 'Only algorithm / enrolled books get auto sells', why: 'Pre-marked legacy carve-outs stay human-managed until enrolled' },
    { title: 'Buys can be paused', set_to: 'Allowed (runtime flag)', why: 'A runtime pause blocks new buys without changing the rest of the system' }
  ];

  function renderTradingRules(payload) {
    var list = document.getElementById('rules-list');
    if (!list) return;
    var rules = (payload && payload.rules && payload.rules.length)
      ? payload.rules
      : RULES_FALLBACK;
    list.innerHTML = rules.map(function (r, i) {
      return (
        '<li>' +
          '<div class="rule-num">' + (i + 1) + '</div>' +
          '<div class="rule-body">' +
            '<div class="rule-title">' + escapeHtml(r.title || '') + '</div>' +
            '<div class="rule-set"><span class="rule-label">Set to</span> ' + escapeHtml(r.set_to || '') + '</div>' +
            '<div class="rule-why"><span class="rule-label">Why</span> ' + escapeHtml(r.why || '') + '</div>' +
          '</div>' +
        '</li>'
      );
    }).join('');
  }

  async function loadAbout() {
    var pills = document.getElementById('home-pills');
    var cards = document.getElementById('home-cards');
    var jobs = document.getElementById('home-jobs');
    try {
      var data = await fetchJson('/api/status');
      if (data.dashboard_rev != null) lastContentRev = data.dashboard_rev;
      var y = data.yahoo || {};
      var filterPayload = data.watchlist_filter || null;
      setupFilterPanel(filterPayload);
      renderTradingRules(data.trading_rules);

      var activeFilter = findFilter(filterState.active);
      pills.innerHTML = '';
      pills.appendChild(pill(data.trade_dry_run ? 'TRADE_DRY_RUN on' : 'LIVE trades', data.trade_dry_run ? 'ok' : 'warn'));
      if (activeFilter) {
        pills.appendChild(pill('Filter: ' + (activeFilter.title || activeFilter.name), 'ok'));
      }
      if (data.last_loop_wake) {
        pills.appendChild(pill('Last wake ' + timeAgo(data.last_loop_wake), ''));
      }
      if (useMock()) pills.appendChild(pill('Mock data', 'warn'));

      cards.innerHTML =
        card('Updated today', String(y.updated_today != null ? y.updated_today : '—'), 'Yahoo last_updated = today') +
        card('Stale / overdue', String(y.overdue_or_stale != null ? y.overdue_or_stale : '—'), 'SLA ' + (y.sla_days || 7) + 'd') +
        card('In database', String(y.in_db != null ? y.in_db : '—'), 'Universe ~' + (y.universe_size || '—')) +
        card('Jobs due now', String((data.jobs || []).filter(function (j) { return j.due_now; }).length), 'This scheduler pass');

      jobs.innerHTML = '';
      (data.jobs || []).forEach(function (j) {
        var li = document.createElement('li');
        li.innerHTML =
          '<div class="job-name">' + escapeHtml(j.job_name) + '</div>' +
          '<div>' + (j.due_now ? '<span class="pill warn">due</span>' : '<span class="pill">idle</span>') + '</div>' +
          '<div class="job-note">' +
            escapeHtml(j.progress_note || '—') +
            (j.last_completed ? ' · last completed ' + escapeHtml(j.last_completed) : '') +
            (j.next_due ? ' · next ' + escapeHtml(j.next_due) : '') +
          '</div>';
        jobs.appendChild(li);
      });
      if (!(data.jobs || []).length) {
        jobs.innerHTML = '<li class="empty">No job rows yet — run the trader once.</li>';
      }
    } catch (e) {
      pills.innerHTML = '';
      pills.appendChild(pill('API error: ' + e.message, 'bad'));
      cards.innerHTML = '';
      jobs.innerHTML = '<li class="empty">Could not load status. Is web_app.py running? Try ?mock=1</li>';
      setupFilterPanel(null);
      renderTradingRules(null);
    }
  }

  async function loadTrader() {
    var pills = document.getElementById('trader-pills');
    var cards = document.getElementById('trader-cards');
    var algoPills = document.getElementById('algo-pills');
    var algoCards = document.getElementById('algo-cards');
    var tbody = document.querySelector('#positions-table tbody');
    var obody = document.querySelector('#orders-table tbody');
    loadPerformance();
    try {
      var data = await fetchJson('/api/portfolio');
      var t = data.totals || {};
      var snap = data.account_snapshot || {};
      var algo = data.algorithm || {};
      var sc = algo.scorecard || {};
      pills.innerHTML = '';
      pills.appendChild(pill((data.positions || []).length + ' positions', 'ok'));
      if (snap.cash != null) pills.appendChild(pill('Cash ' + money(snap.cash), ''));
      var pSync = data.positions_sync || {};
      if (pSync.stale || pSync.ok === false) {
        pills.appendChild(pill('Schwab marks stale', 'warn'));
      } else if (pSync.synced_at) {
        pills.appendChild(pill('Marks ' + String(pSync.synced_at).replace('T', ' ').slice(0, 19), 'ok'));
      }
      if (useMock()) pills.appendChild(pill('Mock data', 'warn'));

      cards.innerHTML =
        card('Market value', money(t.market_value), 'Sum of positions') +
        card('Day G/L', pct(t.day_pct), money(t.day_pl)) +
        card('G/L', money(t.open_pl), pct(t.open_pct)) +
        card('Cost basis', money(t.cost_basis), t.realized_note || '');

      if (algoPills && algoCards) {
        algoPills.innerHTML = '';
        if (algo.algorithm_start) {
          algoPills.appendChild(pill('Start ' + algo.algorithm_start, 'ok'));
        } else {
          algoPills.appendChild(pill('Algorithm start not set', 'warn'));
        }
        var enrolled = (algo.books && algo.books.enrolled) || [];
        var algoBuys = (algo.books && algo.books.algo_buys) || [];
        algoPills.appendChild(pill(enrolled.length + ' enrolled (excluded)', ''));
        algoPills.appendChild(pill(algoBuys.length + ' algo buys', algoBuys.length ? 'ok' : ''));

        var realized = sc.realized_pl != null ? Number(sc.realized_pl) : 0;
        var unreal = sc.unrealized_pl != null ? Number(sc.unrealized_pl) : 0;
        var totalPl = sc.total_pl != null ? Number(sc.total_pl) : realized + unreal;
        algoCards.innerHTML =
          card('Realized G/L', money(realized), 'Closed algo buys only') +
          card('Unrealized G/L', money(unreal), 'Open algo_buy positions') +
          card('Total algo G/L', money(totalPl), 'Realized + unrealized') +
          card(
            'Deployed',
            money(sc.buy_dollars),
            (sc.trade_count != null ? sc.trade_count : 0) + ' algo trades · open MV ' + money(sc.open_market_value)
          );
      }

      var nextList = document.getElementById('algo-next-tasks-list');
      if (nextList) {
        var tasks = data.next_tasks || [];
        if (!tasks.length) {
          nextList.innerHTML = '<li class="empty">No scheduled tasks yet.</li>';
        } else {
          nextList.innerHTML = tasks.map(function (t) {
            var title = t.title || t.job_name || 'Task';
            var when = t.when || (
              t.due_now || (t.minutes_until != null && Number(t.minutes_until) <= 0)
                ? 'now'
                : ('in ' + t.minutes_until + ' minutes')
            );
            return (
              '<li class="' + (t.due_now ? 'due-now' : '') + '">' +
                '<span class="task-name">' + escapeHtml(title) + '</span>' +
                '<span class="task-when">' + escapeHtml(when) + '</span>' +
              '</li>'
            );
          }).join('');
        }
      }

      tbody.innerHTML = '';
      (data.positions || []).forEach(function (p) {
        var statusLabel = p.status_label || (
          p.trail_active ? 'armed' : (p.status === 'holding' ? 'holding' : '—')
        );
        var statusClass = 'pos-status';
        if (p.status === 'armed') statusClass += ' pos-status-armed';
        else if (p.status === 'holding') statusClass += ' pos-status-holding';
        else if (p.status === 'tracking') statusClass += ' pos-status-tracking';
        else if (p.status === 'skipped' || p.status === 'not_enrolled') statusClass += ' pos-status-muted';
        var tr = document.createElement('tr');
        tr.innerHTML =
          '<td>' + escapeHtml(p.ticker || '') + '</td>' +
          '<td class="num">' + (p.shares_owned != null ? p.shares_owned : '—') + '</td>' +
          '<td class="num">' + money(p.market_value) + '</td>' +
          '<td class="' + plClass(p.day_pct) + '">' + pct(p.day_pct) + '</td>' +
          '<td class="' + plClass(p.open_pl) + '">' + money(p.open_pl) + '</td>' +
          '<td class="' + plClass(p.open_pct) + '">' + pct(p.open_pct) + '</td>' +
          '<td class="num">' + money(p.current_price) + '</td>' +
          '<td class="' + statusClass + '">' + escapeHtml(statusLabel) + '</td>';
        tbody.appendChild(tr);
      });
      if (!(data.positions || []).length) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty">No open positions in the database.</td></tr>';
      }

      obody.innerHTML = '';
      (data.pending_orders || []).forEach(function (o) {
        var tr = document.createElement('tr');
        tr.innerHTML =
          '<td>pending buy</td>' +
          '<td>' + escapeHtml(o.ticker || '') + '</td>' +
          '<td class="num">' + (o.quantity_ordered != null ? o.quantity_ordered : '—') + '</td>' +
          '<td>' + money(o.order_amount_dollars) + (o.order_id ? ' · id ' + escapeHtml(String(o.order_id)) : '') + '</td>';
        obody.appendChild(tr);
      });
      (data.open_orders || []).forEach(function (o) {
        var tr = document.createElement('tr');
        tr.innerHTML =
          '<td>Schwab open</td>' +
          '<td>' + escapeHtml(o.ticker || '') + '</td>' +
          '<td class="num">' + (o.quantity != null ? o.quantity : '—') + '</td>' +
          '<td>' + (o.order_id ? 'id ' + escapeHtml(String(o.order_id)) : '—') + '</td>';
        obody.appendChild(tr);
      });
      if (!(data.pending_orders || []).length && !(data.open_orders || []).length) {
        obody.innerHTML = '<tr><td colspan="4" class="empty">No pending or open orders.</td></tr>';
      }
    } catch (e) {
      pills.innerHTML = '';
      pills.appendChild(pill('API error: ' + e.message, 'bad'));
      if (algoPills) algoPills.innerHTML = '';
      if (algoCards) algoCards.innerHTML = '';
      tbody.innerHTML = '<tr><td colspan="8" class="empty">Could not load portfolio.</td></tr>';
      obody.innerHTML = '';
    }
  }

  // Four viewer tags. Activity (Buy/Sell/Watchlist) is the default view; Tasks is ops noise.
  var LOG_CATEGORIES = [
    { id: 'buy', label: 'Buy', meaning: 'PLACING BUY → BOUGHT (fills). Money in.' },
    { id: 'sell', label: 'Sell', meaning: 'PLACING MARKET SELL / STOP LIVE → SOLD (hard or trail). Money out.' },
    { id: 'watchlist', label: 'Watchlist', meaning: 'Watchlist refresh from the filter (adds/removes).' },
    { id: 'task', label: 'Tasks', meaning: 'Scheduler noise: task start/finish, Yahoo batches, Schwab sync, account snapshots.' }
  ];

  // Default: activity only (not Tasks)
  var logFilterSelected = { buy: true, sell: true, watchlist: true };
  var logEventsCache = [];

  function normalizeLogCategory(ev) {
    var c = String((ev && ev.category) || '').toLowerCase();
    if (c === 'buy') return 'buy';
    if (c === 'sell' || c === 'order') return 'sell';
    if (c === 'watchlist') return 'watchlist';
    if (c === 'task' || c === 'job' || c === 'yahoo' || c === 'account' ||
        c === 'book' || c === 'algorithm' || c === 'web') {
      return 'task';
    }
    if (c === 'trade') {
      var m = String((ev && ev.message) || '').toUpperCase();
      if (m.indexOf('SOLD') >= 0 || m.indexOf('SELL') >= 0 || m.indexOf('STOP') >= 0) return 'sell';
      if (m.indexOf('BOUGHT') >= 0 || m.indexOf('BUY') >= 0) return 'buy';
      return 'task';
    }
    return 'task';
  }

  function logCategoryLabel(id) {
    var found = LOG_CATEGORIES.find(function (c) { return c.id === id; });
    return found ? found.label : id;
  }

  function ensureLogFilters() {
    var host = document.getElementById('log-filters');
    if (!host || host.getAttribute('data-ready') === '1') return;
    host.setAttribute('data-ready', '1');
    LOG_CATEGORIES.forEach(function (cat) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pill pill-filter' + (logFilterSelected[cat.id] ? ' active' : '');
      btn.setAttribute('data-cat', cat.id);
      btn.title = cat.meaning;
      btn.textContent = cat.label;
      btn.addEventListener('click', function () {
        if (logFilterSelected[cat.id]) delete logFilterSelected[cat.id];
        else logFilterSelected[cat.id] = true;
        btn.classList.toggle('active', !!logFilterSelected[cat.id]);
        renderLogEvents();
      });
      host.appendChild(btn);
    });
  }

  function updateLogFilterHint() {
    var hint = document.getElementById('log-filter-hint');
    if (!hint) return;
    var keys = Object.keys(logFilterSelected);
    if (!keys.length) {
      hint.textContent = 'Showing all. Tip: leave Buy + Sell + Watchlist on for the trading story; add Tasks for scheduler/Yahoo noise.';
      return;
    }
    var labels = keys.map(logCategoryLabel);
    hint.textContent = 'Showing: ' + labels.join(' + ') + '. Click a pill again to remove it.';
  }

  function renderLogEvents() {
    var box = document.getElementById('event-list');
    if (!box) return;
    updateLogFilterHint();
    var selected = Object.keys(logFilterSelected);
    var events = logEventsCache.map(function (ev) {
      return Object.assign({}, ev, { _cat: normalizeLogCategory(ev) });
    });
    if (selected.length) {
      events = events.filter(function (ev) { return !!logFilterSelected[ev._cat]; });
    }
    if (!logEventsCache.length) {
      box.innerHTML = '<div class="empty">No events yet. Run the trader or Yahoo refresh to generate log rows.</div>';
      return;
    }
    if (!events.length) {
      box.innerHTML = '<div class="empty">No events match the selected filters.</div>';
      return;
    }
    box.innerHTML = events.map(function (ev) {
      return (
        '<div class="event' + (ev.level === 'error' ? ' error' : '') + '">' +
          '<div class="ts">' + escapeHtml(ev.ts || '') + '</div>' +
          '<div class="cat">' + escapeHtml(logCategoryLabel(ev._cat)) + '</div>' +
          '<div>' + escapeHtml(ev.message || '') + '</div>' +
        '</div>'
      );
    }).join('');
  }

  async function loadLog() {
    var box = document.getElementById('event-list');
    ensureLogFilters();
    try {
      var data = await fetchJson('/api/events?limit=100');
      var events = data.events || data;
      if (!Array.isArray(events)) events = [];
      logEventsCache = events;
      renderLogEvents();
    } catch (e) {
      box.innerHTML = '<div class="empty">Could not load events: ' + escapeHtml(e.message) + '</div>';
    }
  }

  function pill(text, kind) {
    var span = document.createElement('span');
    span.className = 'pill' + (kind ? ' ' + kind : '');
    span.textContent = text;
    return span;
  }

  function card(label, value, hint) {
    return (
      '<div class="card"><div class="label">' + escapeHtml(label) + '</div>' +
      '<div class="value">' + escapeHtml(value) + '</div>' +
      (hint ? '<div class="hint">' + escapeHtml(hint) + '</div>' : '') +
      '</div>'
    );
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Hash routing for portability (#trader)
  function fromHash() {
    var h = (location.hash || '#about').replace('#', '');
    if (h === 'home') h = 'about'; // old bookmark
    if (['about', 'trader', 'log', 'actions'].indexOf(h) < 0) h = 'about';
    showPage(h);
  }
  window.addEventListener('hashchange', fromHash);
  document.querySelectorAll('nav.side button').forEach(function (btn) {
    btn.addEventListener('click', function () {
      location.hash = btn.getAttribute('data-page');
    });
  });

  fromHash();

  // Poll: skip when tab hidden.
  // About: always refresh (job due clocks).
  // Trader: always refresh so Price/Status stay downstream of Schwab position sync.
  // Log: only when dashboard_rev changes.
  setInterval(function () {
    if (typeof document !== 'undefined' && document.hidden) return;
    var active = document.querySelector('.page.active');
    if (!active) return;
    if (active.id === 'page-about') {
      loadAbout();
      return;
    }
    if (active.id === 'page-actions') return;
    if (active.id === 'page-trader') {
      loadTrader();
      return;
    }
    fetchJson('/api/status').then(function (data) {
      var rev = data.dashboard_rev;
      if (rev === lastContentRev) return;
      lastContentRev = rev;
      if (active.id === 'page-log') loadLog();
    }).catch(function () {});
  }, 15000);
})();
