/* API base URL.
 * Empty string = same origin (local web_app / Cloudflare Tunnel).
 * For a future GitHub Pages host, set e.g. "https://trader.example.com"
 * or use ?api=https://... on the page URL.
 */
window.TRADER_API_BASE = window.TRADER_API_BASE || '';

// Per-tab session (sessionStorage). Lets one computer keep jame in one window
// and lizstevick in another. Cookie still covers a new tab / mom's computer.
window.TRADER_SESSION_TOKEN_KEY = 'traderSessionToken';

window.traderGetSessionToken = function () {
  try {
    return sessionStorage.getItem(window.TRADER_SESSION_TOKEN_KEY) || '';
  } catch (e) {
    return '';
  }
};

window.traderSetSessionToken = function (token) {
  try {
    if (token) sessionStorage.setItem(window.TRADER_SESSION_TOKEN_KEY, token);
    else sessionStorage.removeItem(window.TRADER_SESSION_TOKEN_KEY);
  } catch (e) {}
};

window.traderClearSessionToken = function () {
  try {
    sessionStorage.removeItem(window.TRADER_SESSION_TOKEN_KEY);
  } catch (e) {}
};

window.traderAuthHeaders = function () {
  var h = {};
  var t = window.traderGetSessionToken();
  if (t) h.Authorization = 'Bearer ' + t;
  return h;
};

(function () {
  try {
    var params = new URLSearchParams(window.location.search);
    var api = params.get('api');
    if (api) {
      window.TRADER_API_BASE = api.replace(/\/$/, '');
    }
    if (params.get('mock') === '1') {
      window.TRADER_USE_MOCK = true;
    }
  } catch (e) { /* ignore */ }
})();
