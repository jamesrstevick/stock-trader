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

  function payloadUserMismatch(data, response) {
    if (!currentUser || currentUser.id == null) return false;
    var id = null;
    if (data && data.user && data.user.id != null) id = data.user.id;
    if (id == null && response && response.headers) {
      id = response.headers.get('X-Trader-User-Id');
    }
    if (id == null || id === '') return false;
    return String(id) !== String(currentUser.id);
  }

  async function fetchJson(path) {
    if (useMock()) {
      var mockPath = '/static/mock' + path.replace('/api', '') + '.json';
      if (path.indexOf('/api/events') === 0) mockPath = '/static/mock/events.json';
      if (path.indexOf('/api/log') === 0) mockPath = '/static/mock/log.json';
      if (path.indexOf('/api/performance') === 0) mockPath = '/static/mock/performance.json';
      if (path.indexOf('/api/schwab/auth') === 0) mockPath = '/static/mock/schwab_auth.json';
      if (path.indexOf('/api/host') === 0) mockPath = '/static/mock/host.json';
      var mr = await fetch(mockPath, { cache: 'no-store' });
      if (!mr.ok) throw new Error('Mock missing: ' + mockPath);
      var mockData = await mr.json();
      if (path.indexOf('/api/performance') === 0) {
        return clipMockPerformance(mockData, path);
      }
      return mockData;
    }
    var headers = window.traderAuthHeaders ? window.traderAuthHeaders() : {};
    var r = await fetch(apiBase() + path, {
      credentials: 'include',
      cache: 'no-store',
      headers: headers,
    });
    if (r.status === 401) {
      if (window.traderClearSessionToken) window.traderClearSessionToken();
      window.location.href = '/login';
      throw new Error('login_required');
    }
    if (!r.ok) throw new Error(path + ' → ' + r.status);
    var data = await r.json();
    if (payloadUserMismatch(data, r)) {
      var err = new Error('stale_user');
      err.staleUser = true;
      throw err;
    }
    return data;
  }

  async function postJson(path, body) {
    if (useMock()) {
      if (path.indexOf('/api/host') === 0) {
        return {
          ok: true,
          queued: true,
          message: 'Mock: would pull or restart on the Dell',
        };
      }
      return {
        ok: true,
        message: 'Mock: would submit Schwab callback (no live exchange)',
        schwab: await fetchJson('/api/schwab/auth'),
      };
    }
    var headers = window.traderAuthHeaders ? window.traderAuthHeaders() : {};
    headers['Content-Type'] = 'application/json';
    var r = await fetch(apiBase() + path, {
      method: 'POST',
      credentials: 'include',
      cache: 'no-store',
      headers: headers,
      body: JSON.stringify(body || {}),
    });
    if (r.status === 401) {
      if (window.traderClearSessionToken) window.traderClearSessionToken();
      window.location.href = '/login';
      throw new Error('login_required');
    }
    var data = {};
    try { data = await r.json(); } catch (e) { data = {}; }
    if (!r.ok) {
      var err = (data && data.error) ? data.error : (path + ' → ' + r.status);
      throw new Error(err);
    }
    return data;
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
      if (e && e.staleUser) return;
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

  function formatMdY(iso) {
    if (!iso) return '—';
    var s = String(iso).trim();
    var m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (m) return m[2] + '/' + m[3] + '/' + m[1];
    var t = Date.parse(s);
    if (isNaN(t)) return s;
    var d = new Date(t);
    var mm = String(d.getMonth() + 1).padStart(2, '0');
    var dd = String(d.getDate()).padStart(2, '0');
    return mm + '/' + dd + '/' + d.getFullYear();
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
    if (name === 'actions') loadActions();
  }

  // --- Schwab reconnect (Actions + site banner) ---
  var SCHWAB_SNOOZE_KEY = 'schwabAuthBannerSnoozeUntil';
  var lastSchwabStatus = null;

  function schwabSnoozeUntil() {
    try {
      var v = parseInt(localStorage.getItem(userScopedKey(SCHWAB_SNOOZE_KEY)) || '0', 10);
      return isNaN(v) ? 0 : v;
    } catch (e) {
      return 0;
    }
  }

  function setSchwabSnooze(hours) {
    var ms = (hours != null ? Number(hours) : 4) * 3600 * 1000;
    try {
      localStorage.setItem(userScopedKey(SCHWAB_SNOOZE_KEY), String(Date.now() + ms));
    } catch (e) {}
  }

  function clearSchwabSnooze() {
    try { localStorage.removeItem(userScopedKey(SCHWAB_SNOOZE_KEY)); } catch (e) {}
  }

  function schwabBannerSnoozed() {
    return Date.now() < schwabSnoozeUntil();
  }

  function formatTimeLeft(h, warnHours) {
    /** Days while above warn window; hours (or minutes) once inside it. */
    if (h == null || isNaN(h)) return '—';
    if (h <= 0) return '0 hours';
    var warnH = (warnHours != null && !isNaN(warnHours)) ? Number(warnHours) : 48;
    if (h > warnH) {
      var days = h / 24;
      var dRounded = days >= 10 ? Math.round(days) : Math.round(days * 10) / 10;
      var dStr = String(dRounded).replace(/\.0$/, '');
      return dStr + (dRounded === 1 ? ' day' : ' days');
    }
    if (h < 1) return Math.max(1, Math.round(h * 60)) + ' minutes';
    var rounded = h >= 10 ? Math.round(h) : Math.round(h * 10) / 10;
    var s = String(rounded).replace(/\.0$/, '');
    return s + (rounded === 1 ? ' hour' : ' hours');
  }

  function looksLikeSchwabCallback(url, redirectUri) {
    var s = (url || '').trim();
    if (!s) return false;
    var redir = (redirectUri || 'https://127.0.0.1').toLowerCase();
    if (s.toLowerCase().indexOf(redir) !== 0) return false;
    return /[?&]code=/.test(s);
  }

  var lastStatusPayload = null;
  var currentUser = null;
  var hostBusy = false;
  var lastOnboardingStage = 'done';
  var filterBuilderState = { criteria: [], catalog: [], debounce: null };
  var ONBOARD_SNOOZE_KEY = 'onboardingGoLiveSnoozeUntil';

  function userScopedKey(base) {
    var id = currentUser && currentUser.id;
    return base + (id != null ? ('.u' + id) : '');
  }

  function onboardingGoLiveSnoozed() {
    try {
      var v = parseInt(localStorage.getItem(userScopedKey(ONBOARD_SNOOZE_KEY)) || '0', 10);
      return !isNaN(v) && Date.now() < v;
    } catch (e) {
      return false;
    }
  }

  function setOnboardingGoLiveSnooze(hours) {
    var ms = (hours != null ? Number(hours) : 4) * 3600 * 1000;
    try {
      localStorage.setItem(userScopedKey(ONBOARD_SNOOZE_KEY), String(Date.now() + ms));
    } catch (e) {}
  }

  function scrollToActionCard(id) {
    location.hash = 'actions';
    showPage('actions');
    var card = document.getElementById(id);
    if (card && card.scrollIntoView) {
      setTimeout(function () {
        card.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, 60);
    }
  }

  function applyActionsBadge(status) {
    var badge = document.getElementById('nav-actions-badge');
    if (!badge) return;
    var attention = !!(status && status.actions_attention);
    if (!attention && status && status.schwab) attention = !!status.schwab.warn;
    badge.hidden = !attention;
  }

  function applyOnboardingBanner(stage) {
    lastOnboardingStage = stage || 'done';
    var banner = document.getElementById('onboarding-banner');
    var text = document.getElementById('onboarding-banner-text');
    var cta = document.getElementById('onboarding-banner-cta');
    var dismiss = document.getElementById('onboarding-banner-dismiss');
    if (!banner || !text || !cta) return;
    var copy = {
      schwab: {
        text: 'Connect Schwab to get started — the bot will not run for this account until linked.',
        cta: 'Connect Schwab',
        target: 'schwab-reconnect-card',
        dismiss: false,
      },
      settings: {
        text: 'Schwab linked — set your cash floors and buy size next.',
        cta: 'Finish account setup',
        target: 'account-setup-card',
        dismiss: false,
      },
      algorithm: {
        text: 'Account ready — pick a filter and press Run (starts in dry-run).',
        cta: 'Start algorithm',
        target: 'algorithm-control-card',
        dismiss: false,
      },
      go_live: {
        text: 'Dry-run is active — Go live when you are ready for real orders.',
        cta: 'Review Go live',
        target: 'algorithm-control-card',
        dismiss: true,
      },
    };
    var cfg = copy[stage];
    if (!cfg || stage === 'done') {
      banner.hidden = true;
      return;
    }
    if (stage === 'go_live' && onboardingGoLiveSnoozed()) {
      banner.hidden = true;
      return;
    }
    text.textContent = cfg.text;
    cta.textContent = cfg.cta;
    cta.setAttribute('data-target', cfg.target);
    if (dismiss) dismiss.hidden = !cfg.dismiss;
    banner.hidden = false;
  }

  function applySchwabChrome(schwab, stage) {
    lastSchwabStatus = schwab || null;
    var banner = document.getElementById('schwab-banner');
    var bannerText = document.getElementById('schwab-banner-text');
    var warn = !!(schwab && schwab.warn);
    if (!banner || !bannerText) return;

    // During first-time Schwab stage, onboarding banner owns the CTA.
    if (stage === 'schwab') {
      banner.hidden = true;
      return;
    }

    if (!warn || schwabBannerSnoozed()) {
      banner.hidden = true;
      return;
    }
    var hours = schwab.hours_left;
    if (schwab.needs_login || (hours != null && hours <= 0)) {
      bannerText.textContent =
        'Schwab disconnected — reconnect required to resume live trades.';
    } else {
      bannerText.textContent =
        'Schwab login expires in ' + formatTimeLeft(hours, schwab.warn_hours) +
        '. Reconnect now to avoid missed trades?';
    }
    banner.hidden = false;
  }

  function showWelcomeBanner(user) {
    var banner = document.getElementById('welcome-banner');
    var text = document.getElementById('welcome-banner-text');
    if (!banner || !text) return;
    var name = (user && (user.display_name || user.username)) || 'there';
    text.textContent = 'Welcome ' + name;
    banner.hidden = false;
    setTimeout(function () {
      banner.hidden = true;
    }, 3000);
  }

  function renderSchwabActionCard(schwab, stage) {
    var card = document.getElementById('schwab-reconnect-card');
    var pillEl = document.getElementById('schwab-status-pill');
    var detail = document.getElementById('schwab-status-detail');
    var submit = document.getElementById('schwab-submit');
    var input = document.getElementById('schwab-callback-input');
    if (!card || !pillEl) return;

    schwab = schwab || {};
    stage = stage || lastOnboardingStage;
    var warn = !!schwab.warn;
    var state = schwab.state || (schwab.available ? 'connected' : 'disconnected');
    var needsLink = state !== 'connected' && state !== 'expiring';
    card.classList.toggle('urgent', stage === 'schwab' || (needsLink && stage !== 'done') || warn);
    card.classList.remove('urgent-warn');

    pillEl.className = 'pill';
    if (state === 'connected') {
      pillEl.textContent = 'Connected';
      pillEl.classList.add('ok');
    } else if (state === 'expiring') {
      pillEl.textContent = 'Expiring soon';
      pillEl.classList.add('warn');
    } else {
      pillEl.textContent = 'Disconnected';
      pillEl.classList.add('bad');
    }

    var parts = [];
    if (stage === 'schwab') {
      parts.push('Required first step for this account');
    }
    if (schwab.hours_left != null) {
      if (schwab.hours_left > 0) {
        parts.push('Expires in ' + formatTimeLeft(schwab.hours_left, schwab.warn_hours));
      } else {
        parts.push('Refresh token expired (0 hours left)');
      }
    } else {
      parts.push('No Schwab credentials on file');
    }
    if (warn || needsLink) {
      parts.push('Connect now to renew ~7 days');
    } else {
      parts.push('You can reconnect early anytime to reset the clock');
    }
    if (detail) detail.textContent = parts.join(' · ');

    if (input && submit) {
      submit.disabled = !looksLikeSchwabCallback(input.value, schwab.redirect_uri);
    }
  }

  async function refreshSchwabUi() {
    try {
      var data = await fetchJson('/api/status');
      lastStatusPayload = data;
      if (data.user) currentUser = data.user;
      var schwab = data.schwab || await fetchJson('/api/schwab/auth');
      var stage = data.onboarding_stage ||
        (data.account_setup && data.account_setup.onboarding_stage) ||
        'done';
      applyActionsBadge(data);
      applyOnboardingBanner(stage);
      applySchwabChrome(schwab, stage);
      var active = document.querySelector('.page.active');
      if (active && active.id === 'page-actions') {
        renderSchwabActionCard(schwab, stage);
        renderAccountSetup(data.account_setup || { setup_complete: !!(currentUser && currentUser.is_admin) }, stage);
        renderAlgorithmControl(data.algorithm_control, stage);
      }
      return schwab;
    } catch (e) {
      return lastSchwabStatus;
    }
  }

  function fillFilterSelect(sel, options, selected) {
    if (!sel) return;
    sel.innerHTML = '';
    (options || []).forEach(function (o) {
      var opt = document.createElement('option');
      opt.value = o.name;
      opt.textContent = o.title || o.name;
      if (o.name === selected) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  function moneyHint(boundsKey, bounds, exclusiveMax) {
    if (!bounds || !bounds[boundsKey]) return '';
    var b = bounds[boundsKey];
    if (boundsKey === 'order_amount_dollars') {
      return 'More than $0, at most $' + Math.floor(b.max_inclusive || 0).toLocaleString();
    }
    var lo = b.min_exclusive;
    var hi = b.max_exclusive;
    return 'More than $' + Math.floor(lo).toLocaleString() +
      ', less than $' + Math.floor(hi).toLocaleString();
  }

  function renderAccountSetup(setup, stage) {
    var card = document.getElementById('account-setup-card');
    if (!card) return;
    setup = setup || {};
    stage = stage || setup.onboarding_stage || lastOnboardingStage;
    // Owner/admin accounts that already trade: never show onboarding setup.
    var done = !!(setup.setup_complete) || !!(currentUser && currentUser.is_admin);
    if (done) {
      card.hidden = true;
      card.setAttribute('hidden', '');
      return;
    }
    card.hidden = false;
    card.removeAttribute('hidden');
    card.classList.toggle('urgent', stage === 'settings');
    card.classList.remove('urgent-warn');

    var schwabOk = !!(setup.steps && setup.steps.schwab_linked);
    var steps = setup.steps || {};
    var list = document.getElementById('setup-checklist');
    if (list) {
      var items = [
        ['schwab_linked', 'Schwab account linked'],
        ['minimum_cash', 'Minimum cash set'],
        ['minimum_liquidation_value', 'Minimum account value set'],
        ['order_amount_dollars', 'Buy size set'],
      ];
      list.innerHTML = items.map(function (pair) {
        var ok = !!steps[pair[0]];
        return '<li class="' + (ok ? 'ok' : 'bad') + '">' + pair[1] + '</li>';
      }).join('');
    }

    var acctLine = document.getElementById('setup-account-line');
    var acct = setup.account;
    if (acctLine) {
      if (acct) {
        acctLine.hidden = false;
        acctLine.textContent =
          'Live from Schwab — cash $' +
          Math.round(acct.cash || 0).toLocaleString() +
          ' · account value $' +
          Math.round(acct.liquidation_value || 0).toLocaleString();
      } else {
        acctLine.hidden = !schwabOk;
        acctLine.textContent = schwabOk
          ? 'Could not load account balances yet — reconnect Schwab or try again.'
          : '';
      }
    }

    var s = setup.settings || {};
    var sug = setup.suggestions || {};
    var bounds = setup.bounds || {};
    var cash = document.getElementById('setup-min-cash');
    var liq = document.getElementById('setup-min-liq');
    var ord = document.getElementById('setup-order-amt');
    var fieldsDisabled = !schwabOk;
    [cash, liq, ord].forEach(function (el) {
      if (el) el.disabled = fieldsDisabled;
    });
    var saveBtn = document.getElementById('setup-save-btn');
    if (saveBtn) saveBtn.disabled = fieldsDisabled;

    if (cash && document.activeElement !== cash) {
      cash.value = s.minimum_cash != null ? s.minimum_cash : (sug.minimum_cash || '');
      if (bounds.minimum_cash) {
        cash.min = bounds.minimum_cash.min_exclusive;
        cash.max = bounds.minimum_cash.max_exclusive;
      }
    }
    if (liq && document.activeElement !== liq) {
      liq.value = s.minimum_liquidation_value != null
        ? s.minimum_liquidation_value
        : (sug.minimum_liquidation_value || '');
      if (bounds.minimum_liquidation_value) {
        liq.min = bounds.minimum_liquidation_value.min_exclusive;
        liq.max = bounds.minimum_liquidation_value.max_exclusive;
      }
    }
    if (ord && document.activeElement !== ord) {
      ord.value = s.order_amount_dollars != null
        ? s.order_amount_dollars
        : (sug.order_amount_dollars || '');
      if (bounds.order_amount_dollars) {
        ord.min = 1;
        ord.max = bounds.order_amount_dollars.max_inclusive;
      }
    }
    var hCash = document.getElementById('setup-min-cash-hint');
    var hLiq = document.getElementById('setup-min-liq-hint');
    var hOrd = document.getElementById('setup-order-amt-hint');
    if (hCash) {
      hCash.textContent = fieldsDisabled
        ? 'Link Schwab first'
        : moneyHint('minimum_cash', bounds);
    }
    if (hLiq) {
      hLiq.textContent = fieldsDisabled
        ? 'Link Schwab first'
        : moneyHint('minimum_liquidation_value', bounds);
    }
    if (hOrd) {
      hOrd.textContent = fieldsDisabled
        ? 'Link Schwab first'
        : moneyHint('order_amount_dollars', bounds);
    }
  }

  function setActionFeedback(id, msg, ok) {
    var el = document.getElementById(id);
    if (!el) return;
    if (!msg) {
      el.hidden = true;
      el.textContent = '';
      el.className = 'action-feedback';
      return;
    }
    el.hidden = false;
    el.textContent = msg;
    el.className = 'action-feedback ' + (ok ? 'ok' : 'bad');
  }

  function renderAlgorithmControl(algo, stage) {
    var card = document.getElementById('algorithm-control-card');
    var pill = document.getElementById('algo-status-pill');
    var detail = document.getElementById('algo-status-detail');
    if (!card || !pill) return;
    algo = algo || {};
    stage = stage || algo.onboarding_stage || lastOnboardingStage;
    var needs = !!algo.needs_first_run;
    var setupDone = !!algo.setup_complete || !!(currentUser && currentUser.is_admin);
    var canRun = !!algo.can_run || (setupDone && !!algo.schwab_linked);
    var canLive = !!algo.can_go_live;
    var blocked = stage === 'schwab' || stage === 'settings';

    card.classList.toggle('muted-card', blocked);
    card.classList.toggle('urgent', stage === 'algorithm');
    card.classList.toggle('urgent-warn', stage === 'go_live');

    pill.className = 'pill';
    if (blocked) {
      pill.textContent = 'Locked';
      pill.classList.add('warn');
    } else if (needs) {
      pill.textContent = 'Not started';
      pill.classList.add('warn');
    } else if (algo.trade_dry_run) {
      pill.textContent = 'Dry-run';
      pill.classList.add('warn');
    } else {
      pill.textContent = 'Live';
      pill.classList.add('ok');
    }
    if (detail) {
      if (blocked) {
        detail.textContent = 'Finish Schwab connection and account setup before starting the algorithm.';
      } else if (needs) {
        detail.textContent =
          'Choose a filter and press Run. That sets your performance start and begins dry-run loops.';
      } else {
        var filterLabel = algo.active_filter || 'safe';
        (algo.filter_options || []).forEach(function (o) {
          if (o.name === algo.active_filter) filterLabel = o.title || o.name;
        });
        detail.textContent =
          'Running since ' + (algo.algorithm_start || '—') +
          ' · Filter: ' + filterLabel +
          ' · ' + (algo.trade_dry_run ? 'Dry-run (no live orders)' : 'LIVE orders enabled') +
          '.';
      }
    }
    fillFilterSelect(
      document.getElementById('algo-filter-select'),
      algo.filter_options,
      algo.active_filter
    );
    var runBtn = document.getElementById('algo-run-btn');
    var liveBtn = document.getElementById('algo-live-btn');
    var pauseBtn = document.getElementById('algo-pause-btn');
    var filterSel = document.getElementById('algo-filter-select');
    if (runBtn) runBtn.disabled = blocked || !canRun;
    if (liveBtn) liveBtn.disabled = blocked || !canLive;
    if (pauseBtn) pauseBtn.disabled = blocked || needs;
    if (filterSel) filterSel.disabled = blocked;

    if (!filterBuilderState.catalog.length && algo.field_catalog) {
      filterBuilderState.catalog = algo.field_catalog;
    }
    if (!filterBuilderState.criteria.length && (algo.starter_criteria || algo.custom_filter)) {
      var custom = algo.custom_filter;
      filterBuilderState.criteria = (custom && custom.criteria && custom.criteria.length)
        ? JSON.parse(JSON.stringify(custom.criteria))
        : JSON.parse(JSON.stringify(algo.starter_criteria || []));
      var nameInput = document.getElementById('filter-save-name');
      if (nameInput && custom && custom.name) nameInput.value = custom.name;
      renderFilterBuilderTableAndBind();
      scheduleFilterPreview();
    }
  }

  function criterionMeta(field) {
    for (var i = 0; i < filterBuilderState.catalog.length; i++) {
      if (filterBuilderState.catalog[i].field === field) return filterBuilderState.catalog[i];
    }
    return { field: field, meaning: field, ops: ['gt', 'lt', 'between'] };
  }

  function readFilterBuilderFromDom() {
    filterBuilderState.criteria.forEach(function (c, idx) {
      var opEl = document.querySelector('select[data-idx="' + idx + '"][data-k="op"]');
      if (opEl) c.op = opEl.value;
      ['min', 'max', 'value', 'why'].forEach(function (k) {
        var el = document.querySelector('[data-idx="' + idx + '"][data-k="' + k + '"]');
        if (!el) return;
        if (k === 'why') c.why = el.value;
        else if (el.value !== '') c[k] = Number(el.value);
      });
    });
  }

  function scheduleFilterPreview() {
    var el = document.getElementById('filter-match-count');
    if (filterBuilderState.debounce) clearTimeout(filterBuilderState.debounce);
    filterBuilderState.debounce = setTimeout(async function () {
      readFilterBuilderFromDom();
      try {
        var res = await postJson('/api/filters/preview', { criteria: filterBuilderState.criteria });
        if (el) el.textContent = (res.count != null ? res.count : '—') + ' stocks match';
      } catch (e) {
        if (el) el.textContent = 'Could not preview';
      }
    }, 300);
  }

  function renderFilterBuilderTableAndBind() {
    renderFilterBuilderTable();
    var body = document.getElementById('filter-builder-body');
    if (!body) return;
    body.onchange = function (ev) {
      readFilterBuilderFromDom();
      if (ev.target && ev.target.classList && ev.target.classList.contains('fb-op')) {
        renderFilterBuilderTableAndBind();
      }
      scheduleFilterPreview();
    };
    body.oninput = function (ev) {
      if (ev.target && ev.target.getAttribute('data-k')) scheduleFilterPreview();
    };
    body.onclick = function (ev) {
      var btn = ev.target.closest('[data-remove]');
      if (!btn) return;
      var idx = Number(btn.getAttribute('data-remove'));
      readFilterBuilderFromDom();
      filterBuilderState.criteria.splice(idx, 1);
      renderFilterBuilderTableAndBind();
      scheduleFilterPreview();
    };
  }

  function renderFilterBuilderTable() {
    var body = document.getElementById('filter-builder-body');
    if (!body) return;
    body.innerHTML = '';
    filterBuilderState.criteria.forEach(function (c, idx) {
      var meta = criterionMeta(c.field);
      var tr = document.createElement('tr');
      var op = c.op || 'gt';
      var setHtml;
      if (op === 'between') {
        setHtml =
          '<div class="filter-set-cell">' +
          opSelectHtml(idx, op) +
          '<input type="number" step="any" data-idx="' + idx + '" data-k="min" value="' + (c.min != null ? c.min : '') + '" />' +
          '<span>–</span>' +
          '<input type="number" step="any" data-idx="' + idx + '" data-k="max" value="' + (c.max != null ? c.max : '') + '" />' +
          '</div>';
      } else {
        setHtml =
          '<div class="filter-set-cell">' +
          opSelectHtml(idx, op) +
          '<input type="number" step="any" data-idx="' + idx + '" data-k="value" value="' + (c.value != null ? c.value : '') + '" />' +
          '</div>';
      }
      tr.innerHTML =
        '<td class="field">' + escapeHtml(c.field) + '</td>' +
        '<td class="wrap">' + escapeHtml(meta.meaning || '') + '</td>' +
        '<td class="set-to">' + setHtml + '</td>' +
        '<td class="wrap"><input type="text" data-idx="' + idx + '" data-k="why" value="' + escapeAttr(c.why || '') + '" /></td>' +
        '<td><button type="button" class="btn-remove-field" data-remove="' + idx + '" aria-label="Remove">×</button></td>';
      body.appendChild(tr);
    });
  }

  function opSelectHtml(idx, op) {
    var opts = [
      ['gt', '>'], ['gte', '≥'], ['lt', '<'], ['lte', '≤'], ['between', 'between']
    ];
    return '<select data-idx="' + idx + '" data-k="op" class="fb-op">' +
      opts.map(function (o) {
        return '<option value="' + o[0] + '"' + (op === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
      }).join('') + '</select>';
  }

  function escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  }

  async function loadActions() {
    await refreshSchwabUi();
    if (!hostBusy) loadHostCard();
  }

  function hostChildLabel(name) {
    if (name === 'loop') return 'Trader loop';
    if (name === 'dashboard') return 'Dashboard';
    if (name === 'tunnel') return 'Cloudflare tunnel';
    return name;
  }

  function renderHostCard(data) {
    var card = document.getElementById('host-deploy-card');
    if (!card) return;
    card.hidden = false;
    var pill = document.getElementById('host-status-pill');
    var detail = document.getElementById('host-status-detail');
    var list = document.getElementById('host-proc-list');
    var gitLine = document.getElementById('host-git-line');
    var pullBtn = document.getElementById('host-pull-btn');
    var restartBtn = document.getElementById('host-restart-btn');
    var alive = !!(data && data.supervisor);
    if (pill) {
      pill.textContent = alive ? (data.busy ? 'Busy' : 'Running') : 'Offline';
      pill.className = 'pill ' + (alive ? (data.busy ? 'warn' : 'ok') : 'bad');
    }
    if (detail) {
      detail.textContent = alive
        ? 'Pull from GitHub on this machine, then restart the three processes. Crashes restart on their own.'
        : ((data && data.hint) || 'Supervisor is not running on this machine.');
    }
    var children = (data && data.children) || {};
    if (list) {
      list.innerHTML = ['loop', 'dashboard', 'tunnel'].map(function (name) {
        var c = children[name] || {};
        var state = c.skipped ? 'skipped' : (c.running ? 'running' : 'stopped');
        var extra = c.pid ? (' pid ' + c.pid) : '';
        var restarts = c.restarts ? (' · restarts ' + c.restarts) : '';
        return '<li><span>' + hostChildLabel(name) + '</span><span class="host-proc-state">' +
          state + extra + restarts + '</span></li>';
      }).join('');
    }
    var git = (data && data.git) || {};
    if (gitLine) {
      gitLine.textContent = 'Git: ' + (git.branch || '—') + ' @ ' + (git.sha || '—') +
        (git.dirty ? ' (local changes)' : '');
    }
    var disable = hostBusy || !alive;
    if (pullBtn) pullBtn.disabled = disable;
    if (restartBtn) restartBtn.disabled = disable;
  }

  async function loadHostCard() {
    var card = document.getElementById('host-deploy-card');
    if (!card) return;
    if (currentUser && !currentUser.is_admin) {
      card.hidden = true;
      return;
    }
    try {
      var data = await fetchJson('/api/host');
      renderHostCard(data);
    } catch (e) {
      if (currentUser && currentUser.is_admin) {
        renderHostCard({ supervisor: false, hint: 'Could not read host status.' });
      } else {
        card.hidden = true;
      }
    }
  }

  function sleepMs(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  async function waitForHostCommand(cmdId) {
    var deadline = Date.now() + 180000;
    var sawDown = false;
    while (Date.now() < deadline) {
      await sleepMs(2000);
      try {
        var data = await fetchJson('/api/host');
        var last = data.last_command || {};
        if (cmdId && last.id === cmdId && last.finished_at && !data.busy) {
          return data;
        }
        if (sawDown && data.supervisor && !data.busy) {
          return data;
        }
        renderHostCard(data);
      } catch (e) {
        sawDown = true;
        setActionFeedback('host-feedback', 'Dashboard restarting…', true);
      }
    }
    throw new Error('Timed out waiting for the host (3 min). Check logs/supervisor.log on the Dell.');
  }

  async function runHostAction(action) {
    var confirmMsg = action === 'pull'
      ? 'Pull the latest GitHub commit onto this machine and restart the trader, dashboard, and tunnel? The site will drop for a few seconds.'
      : 'Restart the trader loop, dashboard, and tunnel on this machine? The site will drop for a few seconds.';
    if (!window.confirm(confirmMsg)) return;
    hostBusy = true;
    var pullBtn = document.getElementById('host-pull-btn');
    var restartBtn = document.getElementById('host-restart-btn');
    if (pullBtn) pullBtn.disabled = true;
    if (restartBtn) restartBtn.disabled = true;
    setActionFeedback(
      'host-feedback',
      action === 'pull' ? 'Queuing git pull…' : 'Queuing restart…',
      true
    );
    try {
      var path = action === 'pull' ? '/api/host/pull' : '/api/host/restart';
      var queued = await postJson(path, {});
      setActionFeedback('host-feedback', queued.message || 'Queued — waiting for supervisor…', true);
      var data = await waitForHostCommand(queued.id);
      var last = (data && data.last_command) || {};
      var ok = last.ok !== false;
      setActionFeedback('host-feedback', last.message || (ok ? 'Done.' : 'Failed'), ok);
      renderHostCard(data);
    } catch (e) {
      setActionFeedback('host-feedback', e.message || 'Host command failed', false);
    }
    hostBusy = false;
    loadHostCard();
  }

  function setSchwabFeedback(msg, ok) {
    var el = document.getElementById('schwab-feedback');
    if (!el) return;
    if (!msg) {
      el.hidden = true;
      el.textContent = '';
      el.className = 'action-feedback';
      return;
    }
    el.hidden = false;
    el.textContent = msg;
    el.className = 'action-feedback ' + (ok ? 'ok' : 'bad');
  }

  function openSchwabLogin() {
    var url = (lastSchwabStatus && lastSchwabStatus.authorize_url) || '';
    if (!url) {
      setSchwabFeedback('Authorize URL not loaded yet — refresh and try again.', false);
      return;
    }
    // Don't pass "noopener" to window.open — many browsers then return null even when
    // the tab opened, which falsely triggers the "popup blocked" message.
    var w = window.open(url, '_blank');
    if (w) {
      try { w.opener = null; } catch (e) {}
      setSchwabFeedback('Complete login in the new tab, then paste the redirect URL here (~30s).', true);
    } else {
      setSchwabFeedback(
        'Popup blocked — use Copy login link, or allow popups for this site.',
        false
      );
    }
  }

  async function copySchwabLoginLink() {
    var url = (lastSchwabStatus && lastSchwabStatus.authorize_url) || '';
    if (!url) {
      setSchwabFeedback('Authorize URL not loaded yet.', false);
      return;
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(url);
        setSchwabFeedback('Login link copied — open it, then paste the redirect URL here.', true);
      } else {
        window.location.href = url;
      }
    } catch (e) {
      window.open(url, '_blank', 'noopener,noreferrer');
      setSchwabFeedback('Opened login link in a new tab.', true);
    }
  }

  async function submitSchwabCallback() {
    var input = document.getElementById('schwab-callback-input');
    var submit = document.getElementById('schwab-submit');
    var redirect = (lastSchwabStatus && lastSchwabStatus.redirect_uri) || 'https://127.0.0.1';
    var url = input ? input.value.trim() : '';
    if (!looksLikeSchwabCallback(url, redirect)) {
      setSchwabFeedback('Paste the full https://127.0.0.1/?code=… URL from the address bar.', false);
      return;
    }
    if (submit) submit.disabled = true;
    setSchwabFeedback('Exchanging code…', true);
    try {
      var result = await postJson('/api/schwab/auth', { callback_url: url });
      clearSchwabSnooze();
      if (input) input.value = '';
      var stage = result.onboarding_stage || 'settings';
      setSchwabFeedback(
        (result.message || 'Schwab connected') + ' — next, set your floors and buy size.',
        true
      );
      await refreshSchwabUi();
      if (stage === 'settings' || stage === 'schwab') {
        scrollToActionCard('account-setup-card');
      }
    } catch (e) {
      setSchwabFeedback(e.message || 'Token exchange failed — open login again and paste quickly.', false);
    } finally {
      if (input && submit) {
        submit.disabled = !looksLikeSchwabCallback(input.value, redirect);
      }
    }
  }

  (function wireSchwabActions() {
    var openBtn = document.getElementById('schwab-open-login');
    var copyBtn = document.getElementById('schwab-copy-link');
    var submitBtn = document.getElementById('schwab-submit');
    var input = document.getElementById('schwab-callback-input');
    var yesBtn = document.getElementById('schwab-banner-yes');
    var noBtn = document.getElementById('schwab-banner-no');

    if (openBtn) openBtn.addEventListener('click', openSchwabLogin);
    if (copyBtn) copyBtn.addEventListener('click', copySchwabLoginLink);
    if (submitBtn) submitBtn.addEventListener('click', submitSchwabCallback);
    if (input) {
      input.addEventListener('input', function () {
        var redirect = (lastSchwabStatus && lastSchwabStatus.redirect_uri) || 'https://127.0.0.1';
        if (submitBtn) {
          submitBtn.disabled = !looksLikeSchwabCallback(input.value, redirect);
        }
      });
      input.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          submitSchwabCallback();
        }
      });
    }
    if (yesBtn) {
      yesBtn.addEventListener('click', function () {
        location.hash = 'actions';
        var card = document.getElementById('schwab-reconnect-card');
        if (card && card.scrollIntoView) {
          setTimeout(function () { card.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 50);
        }
      });
    }
    if (noBtn) {
      noBtn.addEventListener('click', function () {
        var hours = (lastSchwabStatus && lastSchwabStatus.snooze_hours) || 4;
        setSchwabSnooze(hours);
        applySchwabChrome(lastSchwabStatus, lastOnboardingStage);
      });
    }
  })();

  function populateFilterFieldPick(filterText) {
    var pick = document.getElementById('filter-field-pick');
    if (!pick) return;
    var used = {};
    filterBuilderState.criteria.forEach(function (c) { used[c.field] = true; });
    var q = String(filterText || '').toLowerCase().trim();
    var opts = (filterBuilderState.catalog || []).filter(function (f) {
      if (used[f.field]) return false;
      if (!q) return true;
      return (f.field + ' ' + (f.meaning || '')).toLowerCase().indexOf(q) >= 0;
    });
    pick.innerHTML = '<option value="">Choose field…</option>' +
      opts.map(function (f) {
        return '<option value="' + escapeAttr(f.field) + '">' +
          escapeHtml(f.field) + '</option>';
      }).join('');
    pick.hidden = false;
    var search = document.getElementById('filter-field-search');
    if (search) search.hidden = false;
  }

  (function wireOnboardingBanner() {
    var cta = document.getElementById('onboarding-banner-cta');
    var dismiss = document.getElementById('onboarding-banner-dismiss');
    if (cta) {
      cta.addEventListener('click', function () {
        var target = cta.getAttribute('data-target') || 'schwab-reconnect-card';
        scrollToActionCard(target);
      });
    }
    if (dismiss) {
      dismiss.addEventListener('click', function () {
        setOnboardingGoLiveSnooze(4);
        applyOnboardingBanner(lastOnboardingStage);
      });
    }
  })();

  (function wireSetupAlgorithmFilter() {
    var setupSave = document.getElementById('setup-save-btn');
    var setupSchwab = document.getElementById('setup-goto-schwab');
    var algoRun = document.getElementById('algo-run-btn');
    var algoPause = document.getElementById('algo-pause-btn');
    var algoLive = document.getElementById('algo-live-btn');
    var filterAdd = document.getElementById('filter-add-field');
    var filterSave = document.getElementById('filter-save-btn');
    var fieldSearch = document.getElementById('filter-field-search');
    var fieldPick = document.getElementById('filter-field-pick');

    if (setupSave) {
      setupSave.addEventListener('click', async function () {
        setActionFeedback('setup-feedback', 'Saving…', true);
        var payload = {
          minimum_cash: Number((document.getElementById('setup-min-cash') || {}).value),
          minimum_liquidation_value: Number((document.getElementById('setup-min-liq') || {}).value),
          order_amount_dollars: Number((document.getElementById('setup-order-amt') || {}).value),
          finish: true,
        };
        try {
          var res = await postJson('/api/account/setup', payload);
          renderAccountSetup(res.setup || res, res.onboarding_stage);
          setActionFeedback(
            'setup-feedback',
            (res.setup && res.setup.setup_complete)
              ? 'Setup complete — next: pick a filter and press Run.'
              : (res.error || 'Saved.'),
            !!(res.setup && res.setup.setup_complete) || !!res.ok
          );
          await refreshSchwabUi();
          if (res.setup && res.setup.setup_complete) {
            scrollToActionCard('algorithm-control-card');
          }
        } catch (e) {
          setActionFeedback('setup-feedback', e.message || 'Save failed', false);
          refreshSchwabUi();
        }
      });
    }
    if (setupSchwab) {
      setupSchwab.addEventListener('click', function () {
        scrollToActionCard('schwab-reconnect-card');
      });
    }

    async function algoAction(action) {
      setActionFeedback('algo-feedback', 'Working…', true);
      var filter = (document.getElementById('algo-filter-select') || {}).value || null;
      try {
        var res = await postJson('/api/algorithm', { action: action, filter: filter });
        renderAlgorithmControl(res.algorithm || res, res.onboarding_stage);
        var msg = action === 'run'
          ? 'Baseline set — loops will run in dry-run until Go live.'
          : (action === 'pause' ? 'Paused (dry-run on).' : 'LIVE orders enabled.');
        if (res.algorithm && res.algorithm.needs_first_run === false && action === 'run') {
          msg = res.algorithm.algorithm_start
            ? ('Start marked at ' + res.algorithm.algorithm_start +
              ' — loops run in dry-run until Go live.')
            : msg;
        }
        setActionFeedback('algo-feedback', msg, true);
        await refreshSchwabUi();
      } catch (e) {
        setActionFeedback('algo-feedback', e.message || 'Action failed', false);
        refreshSchwabUi();
      }
    }
    if (algoRun) algoRun.addEventListener('click', function () { algoAction('run'); });
    if (algoPause) algoPause.addEventListener('click', function () { algoAction('pause'); });
    if (algoLive) {
      algoLive.addEventListener('click', function () {
        if (!window.confirm('Go live? Real Schwab orders will be placed when the bot trades.')) return;
        algoAction('go_live');
      });
    }

    if (filterAdd) {
      filterAdd.addEventListener('click', function () {
        if (filterBuilderState.criteria.length >= 15) {
          setActionFeedback('filter-builder-feedback', 'Max 15 fields', false);
          return;
        }
        populateFilterFieldPick('');
      });
    }
    if (fieldSearch) {
      fieldSearch.addEventListener('input', function () {
        populateFilterFieldPick(fieldSearch.value);
      });
    }
    if (fieldPick) {
      fieldPick.addEventListener('change', function () {
        var field = fieldPick.value;
        if (!field) return;
        if (filterBuilderState.criteria.length >= 15) {
          setActionFeedback('filter-builder-feedback', 'Max 15 fields', false);
          return;
        }
        filterBuilderState.criteria.push({
          field: field,
          op: 'gt',
          value: 0,
          why: '',
        });
        fieldPick.hidden = true;
        fieldPick.value = '';
        if (fieldSearch) {
          fieldSearch.hidden = true;
          fieldSearch.value = '';
        }
        renderFilterBuilderTableAndBind();
        scheduleFilterPreview();
        setActionFeedback('filter-builder-feedback', '', true);
      });
    }
    if (filterSave) {
      filterSave.addEventListener('click', async function () {
        readFilterBuilderFromDom();
        var name = ((document.getElementById('filter-save-name') || {}).value || '').trim();
        setActionFeedback('filter-builder-feedback', 'Saving…', true);
        try {
          var res = await postJson('/api/filters/save', {
            name: name,
            criteria: filterBuilderState.criteria,
          });
          setActionFeedback(
            'filter-builder-feedback',
            'Saved "' + ((res.filter && res.filter.name) || name) + '" (' +
              (res.count != null ? res.count : '—') +
              ' matches). Selected as active filter.',
            true
          );
          var algo = await fetchJson('/api/algorithm');
          renderAlgorithmControl(algo, algo.onboarding_stage);
          var sel = document.getElementById('algo-filter-select');
          if (sel && res.filter && res.filter.name) sel.value = res.filter.name;
        } catch (e) {
          setActionFeedback('filter-builder-feedback', e.message || 'Save failed', false);
        }
      });
    }
  })();

  (function wireHostActions() {
    var pullBtn = document.getElementById('host-pull-btn');
    var restartBtn = document.getElementById('host-restart-btn');
    if (pullBtn) pullBtn.addEventListener('click', function () { runHostAction('pull'); });
    if (restartBtn) restartBtn.addEventListener('click', function () { runHostAction('restart'); });
  })();

  var refreshLog = document.getElementById('btn-refresh-log');
  if (refreshLog) refreshLog.addEventListener('click', function () { refreshLogHead(); });

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
          { field: 'peg_ratio', meaning: 'P/E divided by expected earnings growth (growth at a reasonable price)', set_to: '0.0 – 2.0', why: 'Prefer growth that is not overpaying; PEG data is required' },
          { field: 'analyst_upside', meaning: 'Implied upside from current price to consensus analyst target', set_to: '> 10%', why: 'Even “safe” names should still have meaningful expected appreciation' },
          { field: 'recommendation_mean', meaning: 'Analyst consensus score (1=Strong Buy … 5=Sell)', set_to: '≤ 2.0', why: 'Bias toward Buy / Strong Buy; recommendation data is required' },
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
    { title: 'No buy-and-sell the same day', set_to: 'No market sell or broker STOP_LIMIT until the next ET calendar day', why: 'FINRA day trade = same trading day (not 24h). Buy Mon 1pm / sell Tue 8am is fine; stops are deferred so a same-day fill cannot create a round trip' },
    { title: 'Cash floor', set_to: 'config.MINIMUM_CASH (live API fills amount)', why: 'No buy may leave cash (minus pending buys) below this floor' },
    { title: 'Account size floor', set_to: 'Liquidation value ≥ $25,000', why: 'Trading is blocked entirely while account value is under this threshold' },
    { title: 'Buy only from the active filter / watchlist', set_to: "Filter 'safe'", why: 'New buys must be on the current watchlist — no random tickers' },
    { title: 'Fixed buy size', set_to: '~$1,000 per new name', why: 'Keeps position sizing consistent and cash-floor math simple' },
    { title: 'Re-buy debounce after a sell', set_to: '≤ last sell price − 5%', why: 'Avoid immediately chasing the same name after a sell' },
    { title: 'Trailing stop arms after a gain', set_to: 'Arm at +10% peak unrealized', why: 'Protect winners once they have moved enough; ignore noise before that' },
    { title: 'Trail buffer below peak', set_to: '5% on watchlist · 3% once off', why: 'Stop ratchets up with new highs; tighter once the thesis (watchlist) breaks' },
    { title: 'Hard stop vs cost', set_to: '−10% on watchlist · −5% once off', why: 'Catastrophe floor when a trail never armed, or thesis is gone' },
    { title: 'Resting broker STOP_LIMIT', set_to: 'Arm after next ET day · limit 0.50% below stop · GOOD_TILL_CANCEL', why: 'Protective sell sits at Schwab once past the purchase day; never armed same day so a stop fill cannot create a PDT round trip' },
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
      var stage = data.onboarding_stage || 'done';
      applyOnboardingBanner(stage);
      applyActionsBadge(data);
      if (data.schwab) applySchwabChrome(data.schwab, stage);
      var y = data.yahoo || {};
      var filterPayload = data.watchlist_filter || null;
      setupFilterPanel(filterPayload);
      renderTradingRules(data.trading_rules);

      var activeFilter = findFilter(filterState.active);
      pills.innerHTML = '';
      if (stage === 'schwab') {
        pills.appendChild(pill('Setup: link Schwab', 'bad'));
      } else if (stage === 'settings') {
        pills.appendChild(pill('Setup: floors & buy size', 'warn'));
      } else if (stage === 'algorithm') {
        pills.appendChild(pill('Algorithm not started', 'warn'));
      } else if (stage === 'go_live') {
        pills.appendChild(pill('Dry-run', 'warn'));
      } else {
        pills.appendChild(pill(data.trade_dry_run ? 'TRADE_DRY_RUN on' : 'LIVE trades', data.trade_dry_run ? 'ok' : 'warn'));
      }
      if (activeFilter && (stage === 'done' || stage === 'go_live' || stage === 'algorithm')) {
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

  var positionsRows = [];
  // Default: Total G/L [%] high → low; click any header to change.
  var positionsSort = { key: 'open_pct', dir: 'desc' };
  var positionsSortBound = false;

  function sortPositions(rows, key, dir) {
    var mult = dir === 'asc' ? 1 : -1;
    var textKeys = { ticker: 1, status_label: 1, status: 1 };
    return rows.slice().sort(function (a, b) {
      var av = a[key];
      var bv = b[key];
      if (key === 'status_label') {
        av = a.status_label || a.status || '';
        bv = b.status_label || b.status || '';
      }
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (textKeys[key]) {
        return String(av).localeCompare(String(bv)) * mult;
      }
      var an = Number(av);
      var bn = Number(bv);
      if (isNaN(an) && isNaN(bn)) return 0;
      if (isNaN(an)) return 1;
      if (isNaN(bn)) return -1;
      if (an === bn) return 0;
      return an < bn ? -mult : mult;
    });
  }

  function updatePositionsSortHeaders() {
    var table = document.getElementById('positions-table');
    if (!table) return;
    table.querySelectorAll('th[data-sort]').forEach(function (th) {
      var key = th.getAttribute('data-sort');
      var active = key === positionsSort.key;
      th.classList.toggle('sorted', active);
      th.classList.toggle('desc', active && positionsSort.dir === 'desc');
      th.setAttribute('aria-sort', active
        ? (positionsSort.dir === 'asc' ? 'ascending' : 'descending')
        : 'none');
    });
  }

  function renderPositionsTable() {
    var tbody = document.querySelector('#positions-table tbody');
    if (!tbody) return;
    updatePositionsSortHeaders();
    var sorted = sortPositions(positionsRows, positionsSort.key, positionsSort.dir);
    tbody.innerHTML = '';
    sorted.forEach(function (p) {
      var statusLabel = p.status_label || (
        p.trail_active ? 'trail' : (p.status === 'holding' ? 'holding' : '—')
      );
      var statusClass = 'pos-status';
      if (p.status === 'trail') statusClass += ' pos-status-trail';
      else if (p.status === 'floor') {
        // Color like G/L cells from total P/L; not bold like trail
        statusClass += ' pos-status-floor';
        var floorPl = Number(p.open_pl);
        if (!isNaN(floorPl) && floorPl < 0) statusClass += ' neg';
        else if (!isNaN(floorPl) && floorPl > 0) statusClass += ' pos';
      } else if (p.status === 'holding') statusClass += ' pos-status-holding';
      else if (p.status === 'tracking') statusClass += ' pos-status-tracking';
      else if (p.status === 'skipped' || p.status === 'not_enrolled') statusClass += ' pos-status-muted';
      var days = p.days_held;
      var daysText = (days === null || days === undefined || days === '') ? '—' : String(days);
      var tr = document.createElement('tr');
      tr.innerHTML =
        '<td>' + escapeHtml(p.ticker || '') + '</td>' +
        '<td class="num">' + escapeHtml(daysText) + '</td>' +
        '<td class="num">' + (p.shares_owned != null ? p.shares_owned : '—') + '</td>' +
        '<td class="num">' + money(p.market_value) + '</td>' +
        '<td class="' + plClass(p.day_pct) + '">' + pct(p.day_pct) + '</td>' +
        '<td class="' + plClass(p.open_pl) + '">' + money(p.open_pl) + '</td>' +
        '<td class="' + plClass(p.open_pct) + '">' + pct(p.open_pct) + '</td>' +
        '<td class="num">' + money(p.current_price) + '</td>' +
        '<td class="' + statusClass + '">' + escapeHtml(statusLabel) + '</td>';
      tbody.appendChild(tr);
    });
    if (!sorted.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty">No open positions in the database.</td></tr>';
    }
  }

  function bindPositionsSort() {
    if (positionsSortBound) return;
    var table = document.getElementById('positions-table');
    if (!table) return;
    positionsSortBound = true;
    table.querySelector('thead').addEventListener('click', function (ev) {
      var th = ev.target.closest('th[data-sort]');
      if (!th) return;
      var key = th.getAttribute('data-sort');
      if (positionsSort.key === key) {
        positionsSort.dir = positionsSort.dir === 'desc' ? 'asc' : 'desc';
      } else {
        positionsSort.key = key;
        // Percents/dollars default high→low; names A→Z
        positionsSort.dir = (key === 'ticker' || key === 'status_label') ? 'asc' : 'desc';
      }
      renderPositionsTable();
    });
  }

  var traderLoadingTimer = null;
  var traderLoadInFlight = false;

  function stopLoadingDots() {
    if (traderLoadingTimer != null) {
      clearInterval(traderLoadingTimer);
      traderLoadingTimer = null;
    }
  }

  function makeLoadingPill(baseText) {
    var span = document.createElement('span');
    span.className = 'pill';
    span.appendChild(document.createTextNode(baseText || 'Loading'));
    var dots = document.createElement('span');
    dots.className = 'loading-dots';
    dots.setAttribute('aria-hidden', 'true');
    span.appendChild(dots);
    stopLoadingDots();
    var frames = ['.', '..', '...', ''];
    var i = 0;
    function tick() {
      dots.textContent = frames[i];
      i = (i + 1) % frames.length;
    }
    tick();
    traderLoadingTimer = setInterval(tick, 400);
    return span;
  }

  async function loadTrader() {
    if (traderLoadInFlight) return;
    traderLoadInFlight = true;
    var pills = document.getElementById('trader-pills');
    var cards = document.getElementById('trader-cards');
    var algoPills = document.getElementById('algo-pills');
    var algoCards = document.getElementById('algo-cards');
    var tbody = document.querySelector('#positions-table tbody');
    var obody = document.querySelector('#orders-table tbody');
    loadPerformance();
    bindPositionsSort();
    if (pills && !pills.childElementCount) {
      pills.appendChild(makeLoadingPill('Loading'));
    }
    try {
      var data = await fetchJson('/api/portfolio');
      stopLoadingDots();
      var t = data.totals || {};
      var snap = data.account_snapshot || {};
      var algo = data.algorithm || {};
      var sc = algo.scorecard || {};
      pills.innerHTML = '';
      pills.appendChild(pill((data.positions || []).length + ' positions', 'ok'));
      // Cash: only real Schwab snapshots. Green within SCHWAB_SYNC_INTERVAL; orange after.
      if (snap.cash != null && snap.cash !== '') {
        var cashLabel = 'Cash ' + money(snap.cash);
        if (snap.stale) {
          if (snap.ts) cashLabel += ' · ' + timeAgo(snap.ts);
          pills.appendChild(pill(cashLabel, 'warn'));
        } else {
          pills.appendChild(pill(cashLabel, 'ok'));
        }
      } else {
        pills.appendChild(pill('Cash unavailable', 'warn'));
      }
      var pSync = data.positions_sync || {};
      if (pSync.stale || pSync.ok === false) {
        var syncLabel = marksSyncLabel(pSync);
        pills.appendChild(pill(syncLabel, 'warn'));
      } else if (pSync.synced_at) {
        pills.appendChild(pill('Schwab prices ' + timeAgo(pSync.synced_at), 'ok'));
      }
      if (useMock()) pills.appendChild(pill('Mock data', 'warn'));

      var cashVal = snap.cash;
      var mvVal = t.market_value;
      var accountVal = snap.liquidation_value;
      var posCount = (data.positions || []).length;
      // Prefer the Schwab snapshot. Only sum when both parts are known
      // (missing snapshot → "—" rather than $0 from empty positions).
      if (accountVal == null && mvVal != null && cashVal != null) {
        accountVal = Number(mvVal) + Number(cashVal);
      } else if (
        accountVal != null &&
        cashVal != null &&
        posCount === 0
      ) {
        // Empty book + equity >> cash is the leftover-other-account flash.
        var cashN = Number(cashVal);
        var accN = Number(accountVal);
        var mvN = mvVal == null ? 0 : Number(mvVal);
        if (
          isFinite(cashN) &&
          isFinite(accN) &&
          Math.abs(accN - (cashN + mvN)) > Math.max(50, Math.abs(cashN) * 0.05)
        ) {
          accountVal = cashN + mvN;
        }
      }
      var accountHint =
        'Market Value: ' + money(mvVal) + '\n' +
        'Cash Value: ' + money(cashVal) +
        (snap.stale && snap.ts ? ('\nCash/account snapshot · ' + timeAgo(snap.ts)) : '');

      cards.innerHTML =
        card('Account value', money(accountVal), accountHint) +
        card('Day G/L', money(t.day_pl), pct(t.day_pct), plTone(t.day_pl)) +
        card('Total G/L', money(t.open_pl), pct(t.open_pct), plTone(t.open_pl));

      if (algoPills && algoCards) {
        algoPills.innerHTML = '';
        if (algo.algorithm_start) {
          algoPills.appendChild(pill('Start ' + formatMdY(algo.algorithm_start), 'ok'));
        } else {
          algoPills.appendChild(pill('Algorithm start not set', 'warn'));
        }
        var books = algo.books || data.books || {};
        var algoBuys = books.algo_buys || [];
        var algoBook = books.algorithm || [];
        var buyCount = algoBuys.length || algoBook.length;
        algoPills.appendChild(pill(buyCount + ' positions', buyCount ? 'ok' : ''));

        var realized = sc.realized_pl != null ? Number(sc.realized_pl) : 0;
        var unreal = sc.unrealized_pl != null ? Number(sc.unrealized_pl) : 0;
        var totalPl = sc.total_pl != null ? Number(sc.total_pl) : realized + unreal;
        var tradeCount = sc.trade_count != null ? sc.trade_count : 0;
        algoCards.innerHTML =
          card('Realized G/L', money(realized), 'Closed positions only') +
          card('Unrealized G/L', money(unreal), 'Open positions') +
          card('Total G/L', money(totalPl), 'Realized + unrealized') +
          card(
            'Deployed',
            money(sc.buy_dollars),
            'Market Value ' + money(sc.open_market_value) + '\n' + tradeCount + ' trades'
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
            var when = t.when;
            if (!when) {
              var mins = t.minutes_until != null ? Number(t.minutes_until) : null;
              if (t.running) {
                var rm = t.running_minutes != null ? Number(t.running_minutes) : 0;
                when = rm <= 0 ? 'running' : (rm === 1 ? 'running 1 min' : ('running ' + rm + ' min'));
              } else if (t.due_now || (mins != null && mins <= 0)) {
                when = 'now';
              } else if (mins == null) {
                when = '—';
              } else if (mins === 1) {
                when = 'in 1 minute';
              } else if (mins > 60) {
                var hrs = Math.max(1, Math.round(mins / 60));
                when = hrs === 1 ? 'in 1 hour' : ('in ' + hrs + ' hours');
              } else {
                when = 'in ' + mins + ' minutes';
              }
            }
            var liClass = t.running ? 'due-now task-running' : (t.due_now ? 'due-now' : '');
            return (
              '<li class="' + liClass + '">' +
                '<span class="task-name">' + escapeHtml(title) + '</span>' +
                '<span class="task-when">' + escapeHtml(when) + '</span>' +
              '</li>'
            );
          }).join('');
        }
      }

      positionsRows = data.positions || [];
      renderPositionsTable();

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
      stopLoadingDots();
      if (e && e.staleUser) return;
      pills.innerHTML = '';
      pills.appendChild(pill('API error: ' + e.message, 'bad'));
      if (algoPills) algoPills.innerHTML = '';
      if (algoCards) algoCards.innerHTML = '';
      positionsRows = [];
      tbody.innerHTML = '<tr><td colspan="9" class="empty">Could not load portfolio.</td></tr>';
      obody.innerHTML = '';
    } finally {
      traderLoadInFlight = false;
    }
  }

  // Four viewer tags. Activity (Buy/Sell/Watchlist) is the default view; Tasks is ops noise.
  var LOG_CATEGORIES = [
    { id: 'buy', label: 'Buy', meaning: 'PLACING BUY → BOUGHT (fills). Money in.' },
    { id: 'sell', label: 'Sell', meaning: 'PLACING MARKET SELL / STOP LIVE → SOLD (hard or trail). Money out.' },
    { id: 'watchlist', label: 'Watchlist', meaning: 'Watchlist refresh from the filter (adds/removes).' },
    { id: 'task', label: 'Tasks', meaning: 'Scheduler noise: task start/finish, Yahoo batches, Schwab sync, account snapshots.' }
  ];

  var LOG_PAGE_SIZE = 50;
  // Default: activity only (not Tasks)
  var logFilterSelected = { buy: true, sell: true, watchlist: true };
  var logEventsCache = [];
  var logHasMore = false;
  var logLoadingMore = false;
  var logLoadInFlight = false;

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

  function logEventsQuery(extra) {
    var keys = Object.keys(logFilterSelected);
    var q = '/api/events?limit=' + LOG_PAGE_SIZE;
    if (keys.length) q += '&categories=' + encodeURIComponent(keys.join(','));
    if (extra && extra.before_id) q += '&before_id=' + encodeURIComponent(String(extra.before_id));
    return q;
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
        loadLog({ reset: true });
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

  function logDayKey(ts) {
    var s = String(ts || '');
    var m = s.match(/^(\d{4}-\d{2}-\d{2})/);
    return m ? m[1] : '';
  }

  function logDayLabel(dayKey) {
    if (!dayKey) return 'Unknown date';
    var parts = dayKey.split('-');
    var d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
    if (isNaN(d.getTime())) return dayKey;
    var now = new Date();
    var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    var dayStart = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    if (dayStart.getTime() === today.getTime()) return 'Today';
    if (dayStart.getTime() === yesterday.getTime()) return 'Yesterday';
    return d.toLocaleDateString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    });
  }

  function logTimeOnly(ts) {
    var s = String(ts || '');
    var m = s.match(/T(\d{2}:\d{2}:\d{2})/);
    if (m) return m[1];
    m = s.match(/\s(\d{2}:\d{2}:\d{2})/);
    if (m) return m[1];
    return s;
  }

  function mergeLogHead(fresh) {
    if (!Array.isArray(fresh) || !fresh.length) {
      if (!logEventsCache.length) logEventsCache = [];
      return;
    }
    var minFresh = Infinity;
    fresh.forEach(function (e) {
      var id = Number(e.id);
      if (!isNaN(id) && id < minFresh) minFresh = id;
    });
    var older = logEventsCache.filter(function (e) {
      return Number(e.id) < minFresh;
    });
    var seen = {};
    var out = [];
    fresh.concat(older).forEach(function (e) {
      var id = e && e.id;
      if (id == null || seen[id]) return;
      seen[id] = true;
      out.push(e);
    });
    logEventsCache = out;
  }

  function renderLogMore() {
    var foot = document.getElementById('log-more');
    if (!foot) return;
    foot.hidden = false;
    foot.innerHTML = '';
    if (!logEventsCache.length) {
      foot.hidden = true;
      return;
    }
    if (logLoadingMore) {
      var loading = document.createElement('span');
      loading.className = 'log-end';
      loading.textContent = 'Loading…';
      foot.appendChild(loading);
      return;
    }
    if (!logHasMore) {
      var end = document.createElement('span');
      end.className = 'log-end';
      end.textContent = 'End of log';
      foot.appendChild(end);
      return;
    }
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-ghost';
    btn.textContent = 'Load older';
    btn.addEventListener('click', loadLogOlder);
    foot.appendChild(btn);
  }

  function renderLogEvents() {
    var box = document.getElementById('event-list');
    if (!box) return;
    updateLogFilterHint();
    var events = logEventsCache.map(function (ev) {
      return Object.assign({}, ev, { _cat: normalizeLogCategory(ev) });
    });
    if (!logEventsCache.length) {
      box.innerHTML = '<div class="empty">No events yet. Run the trader or Yahoo refresh to generate log rows.</div>';
      renderLogMore();
      return;
    }
    if (!events.length) {
      box.innerHTML = '<div class="empty">No events match the selected filters.</div>';
      renderLogMore();
      return;
    }
    var html = [];
    var lastDay = null;
    events.forEach(function (ev) {
      var day = logDayKey(ev.ts);
      if (day && day !== lastDay) {
        html.push(
          '<div class="log-day-break" role="separator">' +
            escapeHtml(logDayLabel(day)) +
          '</div>'
        );
        lastDay = day;
      }
      var lvl = String(ev.level || 'info').toLowerCase();
      if (lvl === 'warning') lvl = 'warn';
      var rowClass = 'event';
      if (lvl === 'error' || lvl === 'issue') rowClass += ' error';
      else if (lvl === 'warn') rowClass += ' warn';
      var lvlLabel = 'INFO';
      if (lvl === 'error') lvlLabel = 'ERROR';
      else if (lvl === 'issue') lvlLabel = 'ISSUE';
      else if (lvl === 'warn') lvlLabel = 'WARN';
      var lvlClass = '';
      if (lvl === 'warn' || lvl === 'error' || lvl === 'issue') {
        lvlClass = ' ' + (lvl === 'issue' ? 'error' : lvl);
      }
      var tsText = day ? logTimeOnly(ev.ts) : String(ev.ts || '');
      html.push(
        '<div class="' + rowClass + '">' +
          '<div class="ts">' + escapeHtml(tsText) + '</div>' +
          '<div class="lvl' + lvlClass + '">' + lvlLabel + '</div>' +
          '<div class="cat">' + escapeHtml(logCategoryLabel(ev._cat)) + '</div>' +
          '<div>' + escapeHtml(ev.message || '') + '</div>' +
        '</div>'
      );
    });
    box.innerHTML = html.join('');
    renderLogMore();
  }

  async function loadLog(opts) {
    var reset = !opts || opts.reset !== false;
    var box = document.getElementById('event-list');
    ensureLogFilters();
    if (logLoadInFlight) return;
    logLoadInFlight = true;
    try {
      var data = await fetchJson(logEventsQuery());
      var events = data.events || [];
      if (!Array.isArray(events)) events = [];
      if (reset) {
        logEventsCache = events;
        logHasMore = !!data.has_more;
      } else {
        mergeLogHead(events);
        // has_more only from reset / load older
      }
      renderLogEvents();
    } catch (e) {
      if (box && (!logEventsCache || !logEventsCache.length)) {
        box.innerHTML = '<div class="empty">Could not load events: ' + escapeHtml(e.message) + '</div>';
      }
      renderLogMore();
    } finally {
      logLoadInFlight = false;
    }
  }

  async function refreshLogHead() {
    ensureLogFilters();
    if (logLoadInFlight || logLoadingMore) return;
    logLoadInFlight = true;
    try {
      var data = await fetchJson(logEventsQuery());
      var events = data.events || [];
      if (!Array.isArray(events)) events = [];
      if (!logEventsCache.length) {
        logEventsCache = events;
        logHasMore = !!data.has_more;
      } else {
        mergeLogHead(events);
      }
      renderLogEvents();
    } catch (e) {
      // Keep existing list on poll failure
    } finally {
      logLoadInFlight = false;
    }
  }

  async function loadLogOlder() {
    if (logLoadingMore || !logHasMore || !logEventsCache.length) return;
    var oldest = null;
    logEventsCache.forEach(function (e) {
      var id = Number(e.id);
      if (isNaN(id)) return;
      if (oldest == null || id < oldest) oldest = id;
    });
    if (oldest == null) return;
    logLoadingMore = true;
    renderLogMore();
    try {
      var data = await fetchJson(logEventsQuery({ before_id: oldest }));
      var events = data.events || [];
      if (!Array.isArray(events)) events = [];
      var seen = {};
      logEventsCache.forEach(function (e) { seen[e.id] = true; });
      events.forEach(function (e) {
        if (e && e.id != null && !seen[e.id]) {
          seen[e.id] = true;
          logEventsCache.push(e);
        }
      });
      logHasMore = !!data.has_more;
      renderLogEvents();
    } catch (e) {
      logLoadingMore = false;
      renderLogMore();
      return;
    }
    logLoadingMore = false;
    renderLogMore();
  }

  function pill(text, kind) {
    var span = document.createElement('span');
    span.className = 'pill' + (kind ? ' ' + kind : '');
    span.textContent = text;
    return span;
  }

  function plTone(v) {
    if (v === null || v === undefined || v === '') return '';
    var n = Number(v);
    if (isNaN(n) || n === 0) return '';
    return n > 0 ? 'pos' : 'neg';
  }

  function marksSyncLabel(pSync) {
    var reason = (pSync && pSync.reason) || '';
    var ago = pSync && pSync.synced_at ? timeAgo(pSync.synced_at) : '';
    if (reason === 'schwab_unavailable') {
      return ago
        ? 'Schwab prices outdated — Schwab offline · last ' + ago
        : 'Schwab prices outdated — Schwab offline';
    }
    if (reason === 'sync_failed') {
      return ago
        ? 'Schwab prices outdated — sync failed · last ' + ago
        : 'Schwab prices outdated — sync failed';
    }
    if (reason === 'database_locked') {
      return ago
        ? 'Schwab prices updating soon — DB busy · last ' + ago
        : 'Schwab prices updating soon — DB busy';
    }
    if (ago) return 'Schwab prices outdated · last updated ' + ago;
    return 'Schwab prices outdated — not synced yet';
  }

  function card(label, value, hint, valueClass) {
    var tone = valueClass ? ' ' + valueClass : '';
    return (
      '<div class="card"><div class="label">' + escapeHtml(label) + '</div>' +
      '<div class="value' + tone + '">' + escapeHtml(value) + '</div>' +
      (hint ? '<div class="hint">' + escapeHtml(hint) + '</div>' : '') +
      '</div>'
    );
  }

  function possessiveAccountLabel(name) {
    var n = String(name || '').trim();
    if (!n) return 'Account';
    if (/s$/i.test(n)) return n + "' Account";
    return n + "'s Account";
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
    var h = (location.hash || '#trader').replace('#', '');
    if (h === 'home') h = 'about'; // old bookmark
    if (['about', 'trader', 'log', 'actions'].indexOf(h) < 0) h = 'trader';
    showPage(h);
  }
  window.addEventListener('hashchange', fromHash);
  document.querySelectorAll('nav.side button[data-page]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      location.hash = btn.getAttribute('data-page');
    });
  });

  function resetTraderView() {
    stopLoadingDots();
    traderLoadInFlight = false;
    lastContentRev = null;
    logEventsCache = [];
    positionsRows = [];
    var pills = document.getElementById('trader-pills');
    var cards = document.getElementById('trader-cards');
    var algoPills = document.getElementById('algo-pills');
    var algoCards = document.getElementById('algo-cards');
    var tbody = document.querySelector('#positions-table tbody');
    var obody = document.querySelector('#orders-table tbody');
    var nextList = document.getElementById('algo-next-tasks-list');
    if (pills) pills.innerHTML = '';
    if (cards) cards.innerHTML = '';
    if (algoPills) algoPills.innerHTML = '';
    if (algoCards) algoCards.innerHTML = '';
    if (tbody) tbody.innerHTML = '';
    if (obody) obody.innerHTML = '';
    if (nextList) nextList.innerHTML = '';
    var perfEmpty = document.getElementById('perf-empty');
    var perfReturns = document.getElementById('perf-returns');
    if (perfEmpty) {
      perfEmpty.hidden = true;
      perfEmpty.textContent = '';
    }
    if (perfReturns) perfReturns.innerHTML = '';
    if (perfChart) {
      try { perfChart.destroy(); } catch (e) {}
      perfChart = null;
    }
  }

  var logoutBtn = document.getElementById('nav-logout');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', function () {
      logoutBtn.disabled = true;
      resetTraderView();
      document.body.classList.remove('authed');
      var navigated = false;
      function goLogin() {
        if (navigated) return;
        navigated = true;
        if (window.traderClearSessionToken) window.traderClearSessionToken();
        window.location.href = '/login';
      }
      postJson('/api/logout', {}).catch(function () {}).then(goLogin);
      // Never leave Sign out looking dead if the API stalls.
      setTimeout(goLogin, 2000);
    });
  }

  window.addEventListener('pageshow', function (ev) {
    if (ev && ev.persisted) {
      window.location.reload();
    }
  });

  fetchJson('/api/me').then(function (me) {
    currentUser = (me && me.user) || null;
    if (!currentUser) {
      window.location.href = '/login';
      return;
    }
    document.body.classList.add('authed');
    var el = document.getElementById('brand-user');
    if (el) {
      var name = currentUser.display_name || currentUser.username || '';
      el.textContent = possessiveAccountLabel(name);
    }
    showWelcomeBanner(currentUser);
    loadHostCard();
    fromHash();
    refreshSchwabUi();
  }).catch(function () {
    if (window.traderClearSessionToken) window.traderClearSessionToken();
    window.location.href = '/login';
  });

  // Poll: skip when tab hidden.
  // About: always refresh (job due clocks).
  // Trader: always refresh so Price/Status stay downstream of Schwab position sync.
  // Actions: refresh Schwab auth status / urgent card.
  // Log: only when dashboard_rev changes.
  setInterval(function () {
    if (typeof document !== 'undefined' && document.hidden) return;
    var active = document.querySelector('.page.active');
    if (!active) return;
    if (active.id === 'page-about') {
      loadAbout();
      return;
    }
    if (active.id === 'page-actions') {
      loadActions();
      return;
    }
    if (active.id === 'page-trader') {
      loadTrader();
      refreshSchwabUi();
      return;
    }
    fetchJson('/api/status').then(function (data) {
      var stage = data.onboarding_stage || 'done';
      applyOnboardingBanner(stage);
      applyActionsBadge(data);
      if (data.schwab) applySchwabChrome(data.schwab, stage);
      var rev = data.dashboard_rev;
      if (rev === lastContentRev) return;
      lastContentRev = rev;
      if (active.id === 'page-log') refreshLogHead();
    }).catch(function () {});
  }, 15000);
})();
