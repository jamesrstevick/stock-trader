(function () {
  'use strict';

  function apiBase() {
    return (window.TRADER_API_BASE || '').replace(/\/$/, '');
  }

  // Already signed in? Go home.
  fetch(apiBase() + '/api/me', { credentials: 'same-origin' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (data) {
      if (data && data.authenticated) {
        window.location.replace('/');
      }
    })
    .catch(function () {});

  var form = document.getElementById('login-form');
  var errEl = document.getElementById('login-error');
  var submit = document.getElementById('login-submit');

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    errEl.hidden = true;
    submit.disabled = true;
    var prevLabel = submit.textContent;
    submit.textContent = 'Signing in…';
    var username = document.getElementById('login-username').value;
    var password = document.getElementById('login-password').value;
    var finished = false;

    function fail(msg) {
      if (finished) return;
      finished = true;
      errEl.textContent = msg || 'Sign in failed';
      errEl.hidden = false;
      submit.disabled = false;
      submit.textContent = prevLabel || 'Sign in';
    }

    var timer = setTimeout(function () {
      fail('Sign in is taking too long — check that web_app.py is running, then try again.');
    }, 15000);

    fetch(apiBase() + '/api/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username, password: password }),
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { ok: r.ok, data: data };
        }).catch(function () {
          return { ok: false, data: { error: 'Sign in failed (bad response)' } };
        });
      })
      .then(function (res) {
        clearTimeout(timer);
        if (finished) return;
        if (!res.ok || !res.data.ok) {
          fail((res.data && res.data.error) || 'Sign in failed');
          return;
        }
        finished = true;
        window.location.replace('/');
      })
      .catch(function () {
        clearTimeout(timer);
        fail('Could not reach the server');
      });
  });
})();
