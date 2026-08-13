(function () {
  'use strict';

  var NEW_USER_TOKEN = 'newuser';

  function apiBase() {
    return (window.TRADER_API_BASE || '').replace(/\/$/, '');
  }

  function authHeaders() {
    return window.traderAuthHeaders ? window.traderAuthHeaders() : {};
  }

  // This tab already has a session? Go home.
  // Cookie-only (another window's login) stays here so this tab can sign in
  // as a different account.
  var tabToken = window.traderGetSessionToken ? window.traderGetSessionToken() : '';
  if (tabToken) {
    fetch(apiBase() + '/api/me', {
      credentials: 'include',
      cache: 'no-store',
      headers: authHeaders(),
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && data.authenticated) {
          window.location.replace('/');
        }
      })
      .catch(function () {});
  }

  var form = document.getElementById('login-form');
  var errEl = document.getElementById('login-error');
  var submit = document.getElementById('login-submit');
  var passwordEl = document.getElementById('login-password');
  var newWrap = document.getElementById('login-new-wrap');
  var newHint = document.getElementById('login-new-hint');
  var newPassEl = document.getElementById('login-new-password');

  function isNewUserFlow() {
    return (passwordEl.value || '').trim().toLowerCase() === NEW_USER_TOKEN;
  }

  function syncNewUserFields() {
    var on = isNewUserFlow();
    if (newWrap) {
      newWrap.hidden = !on;
      if (on) newWrap.removeAttribute('hidden');
      else newWrap.setAttribute('hidden', '');
    }
    if (newHint) {
      newHint.hidden = !on;
      if (on) newHint.removeAttribute('hidden');
      else newHint.setAttribute('hidden', '');
    }
    if (newPassEl) {
      newPassEl.required = on;
      if (!on) newPassEl.value = '';
    }
    if (submit) submit.textContent = on ? 'Go' : 'Sign in';
    if (on && newPassEl && document.activeElement === passwordEl) {
      setTimeout(function () { newPassEl.focus(); }, 0);
    }
  }

  if (passwordEl) {
    passwordEl.addEventListener('input', syncNewUserFields);
    passwordEl.addEventListener('change', syncNewUserFields);
  }
  syncNewUserFields();

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    errEl.hidden = true;
    var username = document.getElementById('login-username').value;
    var password = passwordEl.value;
    var creating = isNewUserFlow();
    var newPassword = creating ? (newPassEl.value || '') : '';
    if (creating && !newPassword.trim()) {
      errEl.textContent = 'Enter a password in Set password';
      errEl.hidden = false;
      if (newPassEl) newPassEl.focus();
      return;
    }
    submit.disabled = true;
    var prevLabel = submit.textContent;
    submit.textContent = creating ? 'Creating…' : 'Signing in…';
    var finished = false;

    function fail(msg) {
      if (finished) return;
      finished = true;
      errEl.textContent = msg || (creating ? 'Could not create account' : 'Sign in failed');
      errEl.hidden = false;
      submit.disabled = false;
      submit.textContent = prevLabel || (creating ? 'Go' : 'Sign in');
    }

    var timer = setTimeout(function () {
      fail(
        creating
          ? 'Creating the account is taking too long — check that the dashboard is running, then try again.'
          : 'Sign in is taking too long — check that web_app.py is running, then try again.'
      );
    }, 15000);

    var body = { username: username, password: password };
    if (creating) body.new_password = newPassword;

    fetch(apiBase() + '/api/login', {
      method: 'POST',
      credentials: 'include',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { ok: r.ok, data: data };
        }).catch(function () {
          return { ok: false, data: { error: creating ? 'Create failed (bad response)' : 'Sign in failed (bad response)' } };
        });
      })
      .then(function (res) {
        clearTimeout(timer);
        if (finished) return;
        if (!res.ok || !res.data.ok) {
          fail((res.data && res.data.error) || (creating ? 'Could not create account' : 'Sign in failed'));
          return;
        }
        finished = true;
        if (res.data.token && window.traderSetSessionToken) {
          window.traderSetSessionToken(res.data.token);
        }
        window.location.replace('/');
      })
      .catch(function () {
        clearTimeout(timer);
        fail('Could not reach the server');
      });
  });
})();
