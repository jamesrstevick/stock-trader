/* API base URL.
 * Empty string = same origin (local web_app / Cloudflare Tunnel).
 * For a future GitHub Pages host, set e.g. "https://trader.example.com"
 * or use ?api=https://... on the page URL.
 */
window.TRADER_API_BASE = window.TRADER_API_BASE || '';

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
