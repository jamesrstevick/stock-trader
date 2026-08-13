"""
Local web dashboard for stock-trader.

Serves portable static UI from web/static/ and JSON APIs for About / Trader / Log.
In-app username/password login (session cookie ~30 days). No Cloudflare required.

Usage:
  python web_app.py
"""

from __future__ import print_function

import os
import threading
from typing import Optional

import load_config
import config
import host_control as host
import stock_trader as st
import user_context as uc

try:
    from fastapi import FastAPI, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError as e:
    raise SystemExit(
        "Dashboard requires fastapi and uvicorn. Install with:\n"
        "  pip install -r requirements.txt\n"
        "Original error: %s" % e
    )


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web', 'static')

app = FastAPI(title='Jame Trader Dashboard', docs_url=None, redoc_url=None)

_cors = getattr(config, 'WEB_CORS_ORIGINS', '') or ''
_origins = [o.strip() for o in _cors.split(',') if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=['GET', 'POST', 'OPTIONS'],
        allow_headers=['*', 'Authorization'],
        expose_headers=['X-Trader-User-Id'],
    )


def _asset_mtime(name: str) -> int:
    path = os.path.join(STATIC_DIR, name)
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


def _session_token(request: Request) -> Optional[str]:
    """Prefer per-tab Bearer token so two windows can be two users."""
    auth = (request.headers.get('authorization') or '').strip()
    if len(auth) >= 7 and auth[:7].lower() == 'bearer ':
        tok = auth[7:].strip()
        if tok:
            return tok
    return request.cookies.get(uc.SESSION_COOKIE)


def _cookie_token(request: Request) -> Optional[str]:
    return request.cookies.get(uc.SESSION_COOKIE)


def _current_user(request: Request):
    return uc.user_from_session_token(_session_token(request))


def _require_admin(request: Request):
    user = getattr(request.state, 'user', None)
    if not user or not user.get('is_admin'):
        return JSONResponse({'ok': False, 'error': 'admin_required'}, status_code=403)
    return None


def _request_is_https(request: Request) -> bool:
    forwarded = (request.headers.get('x-forwarded-proto') or '').split(',')[0].strip().lower()
    if forwarded:
        return forwarded == 'https'
    host = (request.headers.get('host') or '').split(':')[0].lower()
    if host and host not in ('127.0.0.1', 'localhost', '::1'):
        return True
    return (request.url.scheme or '').lower() == 'https'


def _set_session_cookie(response, token: str, request: Optional[Request] = None) -> None:
    max_age = uc.session_days() * 24 * 3600
    secure = _request_is_https(request) if request is not None else False
    response.set_cookie(
        key=uc.SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite='lax',
        secure=secure,
        max_age=max_age,
        path='/',
    )


PUBLIC_PATHS = frozenset([
    '/api/login',
    '/api/logout',
    '/api/me',
    '/login',
])


@app.middleware('http')
async def auth_and_cache(request: Request, call_next):
    path = request.url.path or ''

    # Auth gate for APIs (mock mode is client-side only; live API always requires login)
    if path.startswith('/api/') and path not in PUBLIC_PATHS:
        user = _current_user(request)
        if not user:
            return JSONResponse({'ok': False, 'error': 'login_required'}, status_code=401)
        request.state.user = user
    response = await call_next(request)

    if path.startswith('/api/'):
        response.headers['Cache-Control'] = 'private, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        user = getattr(request.state, 'user', None)
        if user is None and path in PUBLIC_PATHS:
            user = _current_user(request)
        if user and user.get('id') is not None:
            response.headers['X-Trader-User-Id'] = str(user['id'])
    elif path == '/' or path == '/login' or path.endswith('.html') or path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.on_event('startup')
def _startup():
    """
    Bind HTTP immediately. Heavy market_data.db init runs in a daemon thread so a
    locked DB (main.py --loop) cannot leave Cloudflare returning 502 while uvicorn
    is stuck in "Waiting for application startup".
    """
    st.setup_file_logging()

    def _bg_init():
        try:
            # Fail fast on locks; schema is already present when the trader loop runs.
            st.init_database(
                timeout=3.0,
                busy_timeout_ms=2000,
                init_schwab=True,
            )
        except Exception as e:
            print('Warning: database init on startup: %s' % e)
            try:
                # Still mark usable if the trader already owns a live schema.
                st.mark_database_ready_if_present()
            except Exception:
                pass
        try:
            st.log_event('web', 'Dashboard web_app started')
        except Exception as e:
            print('Warning: startup event not written: %s' % e)

    threading.Thread(target=_bg_init, name='web-db-init', daemon=True).start()


@app.get('/api/me')
def api_me(request: Request):
    user = _current_user(request)
    if not user:
        return JSONResponse({'ok': False, 'authenticated': False}, status_code=401)
    return {
        'ok': True,
        'authenticated': True,
        'user': {
            'id': user['id'],
            'username': user['username'],
            'display_name': user.get('display_name'),
            'is_admin': bool(user.get('is_admin')),
        },
    }


@app.post('/api/login')
async def api_login(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = (body or {}).get('username') or ''
    password = (body or {}).get('password') or ''
    new_password = (body or {}).get('new_password') or ''
    # Unused username + password "newuser" + Set password → create account and sign in.
    if str(password).strip().lower() == uc.NEW_USER_LOGIN_TOKEN:
        try:
            created = uc.register_from_login(str(username), str(new_password))
        except Exception as e:
            return JSONResponse(
                {'ok': False, 'error': 'Could not create account — try again (%s)' % e},
                status_code=503,
            )
        if not created.get('ok'):
            return JSONResponse(
                {'ok': False, 'error': created.get('error') or 'Could not create account'},
                status_code=400,
            )
        user = created.get('user')
        try:
            token = uc.create_session(int(user['id']))
        except Exception as e:
            return JSONResponse(
                {'ok': False, 'error': 'Account created but session failed — try signing in (%s)' % e},
                status_code=503,
            )
        resp = JSONResponse({
            'ok': True,
            'created': True,
            'token': token,
            'user': {
                'id': user['id'],
                'username': user['username'],
                'display_name': user.get('display_name'),
            },
        })
        _set_session_cookie(resp, token, request)
        return resp
    try:
        user = uc.authenticate(str(username), str(password))
    except Exception as e:
        return JSONResponse(
            {'ok': False, 'error': 'Sign in temporarily unavailable — try again (%s)' % e},
            status_code=503,
        )
    if not user:
        return JSONResponse(
            {'ok': False, 'error': 'Invalid username or password'},
            status_code=401,
        )
    try:
        token = uc.create_session(int(user['id']))
    except Exception as e:
        return JSONResponse(
            {'ok': False, 'error': 'Could not create session — try again (%s)' % e},
            status_code=503,
        )
    resp = JSONResponse({
        'ok': True,
        'token': token,
        'user': {
            'id': user['id'],
            'username': user['username'],
            'display_name': user.get('display_name'),
        },
    })
    _set_session_cookie(resp, token, request)
    return resp


@app.post('/api/me/password')
async def api_me_password(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    # Wrong current password is 400, not 401 — 401 would bounce the UI to /login.
    result = uc.change_own_password(
        int(request.state.user['id']),
        (body or {}).get('current_password') or '',
        (body or {}).get('new_password'),
    )
    if not result.get('ok'):
        return JSONResponse(result, status_code=400)
    return result


@app.post('/api/logout')
def api_logout(request: Request):
    # Destroy this tab's session. Only clear the shared cookie when this tab
    # was using it — another window may still be signed in as someone else.
    presented = _session_token(request)
    cookie_tok = _cookie_token(request)
    try:
        uc.destroy_session(presented)
    except Exception as e:
        print('Warning: logout session cleanup: %s' % e)
    resp = JSONResponse({'ok': True})
    if not presented or presented == cookie_tok:
        resp.delete_cookie(
            uc.SESSION_COOKIE,
            path='/',
            secure=_request_is_https(request),
            httponly=True,
            samesite='lax',
        )
    return resp


@app.get('/api/status')
def api_status(request: Request):
    with uc.use_user(int(request.state.user['id'])):
        return st.get_dashboard_status()


@app.get('/api/account/setup')
def api_account_setup_get(request: Request):
    with uc.use_user(int(request.state.user['id'])):
        return st.get_account_setup_status()


@app.post('/api/account/setup')
async def api_account_setup_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    with uc.use_user(int(request.state.user['id'])):
        result = st.save_account_setup(body if isinstance(body, dict) else {})
    status = 200 if result.get('ok') else 400
    return JSONResponse(result, status_code=status)


@app.get('/api/algorithm')
def api_algorithm_get(request: Request):
    with uc.use_user(int(request.state.user['id'])):
        return st.get_algorithm_control_status()


@app.post('/api/algorithm')
async def api_algorithm_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body or {}).get('action')
    filter_name = (body or {}).get('filter')
    with uc.use_user(int(request.state.user['id'])):
        result = st.run_algorithm_action(
            str(action or ''),
            filter_name=str(filter_name) if filter_name else None,
        )
    status = 200 if result.get('ok') else 400
    return JSONResponse(result, status_code=status)


@app.get('/api/filters/catalog')
def api_filters_catalog(request: Request):
    import filter_builder as fb
    with uc.use_user(int(request.state.user['id'])):
        custom = fb.get_user_custom_filter(int(request.state.user['id']))
        return {
            'fields': fb.field_catalog_for_api(),
            'starter_criteria': fb.starter_criteria(),
            'max_fields': fb.MAX_CUSTOM_FIELDS,
            'custom': custom,
        }


@app.post('/api/filters/preview')
async def api_filters_preview(request: Request):
    import filter_builder as fb
    try:
        body = await request.json()
    except Exception:
        body = {}
    criteria = (body or {}).get('criteria') or []
    if not isinstance(criteria, list):
        return JSONResponse({'ok': False, 'error': 'criteria must be a list'}, status_code=400)
    return fb.preview_match_count(criteria)


@app.post('/api/filters/save')
async def api_filters_save(request: Request):
    import filter_builder as fb
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = (body or {}).get('name') or ''
    criteria = (body or {}).get('criteria') or []
    uid = int(request.state.user['id'])
    with uc.use_user(uid):
        result = fb.save_user_custom_filter(uid, str(name), criteria if isinstance(criteria, list) else [])
        if result.get('ok'):
            uc.set_user_active_filter(uid, str(name).strip())
    status = 200 if result.get('ok') else 400
    return JSONResponse(result, status_code=status)


@app.get('/api/schwab/auth')
def api_schwab_auth_get(request: Request):
    with uc.use_user(int(request.state.user['id'])):
        return st.get_schwab_auth_status()


@app.post('/api/schwab/auth')
async def api_schwab_auth_post(request: Request):
    """
    Complete Schwab OAuth paste-back for the logged-in user.
    Body: { "callback_url": "https://127.0.0.1/?code=..." }
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    callback_url = (body or {}).get('callback_url')
    if not callback_url or not isinstance(callback_url, str):
        return JSONResponse(
            {'ok': False, 'error': 'callback_url required'},
            status_code=400,
        )
    uid = int(request.state.user['id'])
    with uc.use_user(uid):
        result = st.complete_schwab_oauth(callback_url.strip(), user_id=uid)
    status = 200 if result.get('ok') else 400
    return JSONResponse(result, status_code=status)


@app.get('/api/portfolio')
def api_portfolio(request: Request):
    with uc.use_user(int(request.state.user['id'])):
        return st.get_dashboard_portfolio()


@app.get('/api/performance')
def api_performance(
    request: Request,
    range_key: str = Query('1M', alias='range'),
):
    with uc.use_user(int(request.state.user['id'])):
        return st.get_performance_comparison(range_key=range_key)


@app.get('/api/events')
def api_events(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    categories: Optional[str] = Query(
        None,
        description='Comma-separated: buy,sell,watchlist,task',
    ),
    before_id: Optional[int] = Query(
        None,
        ge=1,
        description='Return events with id < before_id (older page)',
    ),
):
    cats = None
    if categories:
        cats = [p.strip() for p in str(categories).split(',') if p.strip()]
    with uc.use_user(int(request.state.user['id'])):
        return st.get_dashboard_events(
            limit=limit,
            categories=cats,
            before_id=before_id,
        )


@app.get('/api/log')
def api_log(request: Request, lines: int = Query(200, ge=1, le=2000)):
    return {
        'path': getattr(config, 'WEB_LOG_PATH', 'logs/trader.log'),
        'text': st.get_file_log_tail(lines=lines),
    }


@app.get('/api/host')
def api_host_get(request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    return host.get_host_status()


@app.post('/api/host/pull')
def api_host_pull(request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    result = host.queue_host_command('pull')
    status = 200 if result.get('ok') else 409
    return JSONResponse(result, status_code=status)


@app.post('/api/host/restart')
def api_host_restart(request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    result = host.queue_host_command('restart')
    status = 200 if result.get('ok') else 409
    return JSONResponse(result, status_code=status)


@app.post('/api/commands')
async def api_commands(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    command = (body or {}).get('command')
    payload = (body or {}).get('payload')
    if not command:
        return JSONResponse({'ok': False, 'error': 'command required'}, status_code=400)
    with uc.use_user(int(request.state.user['id'])):
        result = st.enqueue_web_command(
            str(command), payload if isinstance(payload, dict) else None
        )
    status = 200 if result.get('ok') else 400
    return JSONResponse(result, status_code=status)


def _read_html(name: str) -> str:
    path = os.path.join(STATIC_DIR, name)
    with open(path, 'r', encoding='utf-8') as f:
        html = f.read()
    replacements = {
        '/static/styles.css': '/static/styles.css?v=%d' % _asset_mtime('styles.css'),
        '/static/app.js': '/static/app.js?v=%d' % _asset_mtime('app.js'),
        '/static/config.js': '/static/config.js?v=%d' % _asset_mtime('config.js'),
        '/static/login.js': '/static/login.js?v=%d' % _asset_mtime('login.js'),
    }
    for old, new in replacements.items():
        html = html.replace('href="%s"' % old, 'href="%s"' % new)
        html = html.replace("href='%s'" % old, "href='%s'" % new)
        html = html.replace('src="%s"' % old, 'src="%s"' % new)
        html = html.replace("src='%s'" % old, "src='%s'" % new)
    return html


@app.get('/login')
def login_page():
    return HTMLResponse(_read_html('login.html'))


@app.get('/')
def index(request: Request):
    # Always serve the shell. Auth is cookie and/or per-tab Bearer; app.js
    # sends 401 → /login. Server-side redirect would loop when only Bearer
    # is set (other window overwrote the shared cookie).
    return HTMLResponse(_read_html('index.html'))


if os.path.isdir(STATIC_DIR):
    app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')


def main(host: Optional[str] = None, port: Optional[int] = None):
    host = host or getattr(config, 'WEB_HOST', '127.0.0.1')
    port = int(port or getattr(config, 'WEB_PORT', 8787))
    print('Dashboard: http://%s:%s/' % (host, port))
    print('Static dir: %s' % STATIC_DIR)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level='info',
        proxy_headers=True,
        forwarded_allow_ips='*',
    )


if __name__ == '__main__':
    main()
