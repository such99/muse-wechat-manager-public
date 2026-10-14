"""Local deployment policy. No upstream config files or credential stores loaded."""
import json
import logging
import os
import re
import secrets
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get('MUSE_APP_ROOT') or CODE_ROOT).resolve()


def load_instance(name):
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', name):
        raise ValueError('Invalid instance name')
    settings = json.loads((ROOT / 'instances' / (name + '.json')).read_text(encoding='utf-8'))
    if settings['instance'] != name or settings['site'] != 'https://muse.ai/' or settings['model'] != 'muse-spark':
        raise ValueError('Instance settings rejected')
    if settings.get('routing',{}).get('mode','branch_replay') not in ('branch_replay','native_main'):
        raise ValueError('Unknown routing mode')
    ports = [settings[k] for k in ('muse_port', 'bridge_port', 'cdp_port')]
    if len(set(ports)) != 3 or any(type(p) is not int or not 1024 <= p < 65536 for p in ports):
        raise ValueError('Invalid ports')
    for path in (ROOT / 'instances').glob('*.json'):
        if path.stem == name:
            continue
        other = json.loads(path.read_text(encoding='utf-8'))
        if set(ports) & {other[k] for k in ('muse_port', 'bridge_port', 'cdp_port')}:
            raise ValueError('Instance port collision')
    settings['runtime'] = ROOT / 'runtime' / name
    settings['runtime'].mkdir(parents=True, exist_ok=True)
    return settings


def quiet():
    # Upstream exceptions/logs can include URLs, replies and raw protocol messages.
    logging.disable(logging.CRITICAL)


def browser_environment():
    # Do not send API credentials or inherited application-specific Python paths into Chrome.
    return {k: v for k, v in os.environ.items()
            if not any(x in k.upper() for x in ('TOKEN', 'COOKIE', 'SECRET', 'KEY', 'PASSWORD', 'PYTHONPATH'))}


def persist_routing(settings,mode):
    """Only ordinary routing configuration persists, never native/session identity."""
    if mode not in ('native_main','branch_replay'):
        raise ValueError('Unknown routing mode')
    path=ROOT/'instances'/(settings['instance']+'.json')
    data=json.loads(path.read_text(encoding='utf-8'))
    if any(data[k]!=settings[k] for k in ('instance','muse_port','bridge_port','cdp_port')):
        raise ValueError('Instance configuration changed; restart before activation')
    data['routing']={**data.get('routing',{}),'mode':mode}
    pending=path.with_suffix('.json.new')
    pending.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(pending,path)
    settings['routing']=data['routing']


def secret_matches(actual,expected):
    try:return secrets.compare_digest(actual.encode('utf-8'),expected.encode('utf-8'))
    except (AttributeError,UnicodeError):return False


class LocalBodyLimit:
    """Bound local control requests while receiving, including chunked bodies."""
    def __init__(self,app,limit=65536):self.app,self.limit=app,limit

    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or not scope.get('path','').startswith('/local/') or scope['method'] in ('GET','HEAD'):
            return await self.app(scope,receive,send)
        from fastapi.responses import JSONResponse
        chunks=[];size=0
        while True:
            message=await receive()
            if message['type']=='http.disconnect':return
            chunk=message.get('body',b'');size+=len(chunk)
            if size>self.limit:
                return await JSONResponse({'detail':'请求过大。'},status_code=413)(scope,receive,send)
            chunks.append(chunk)
            if not message.get('more_body',False):break
        replayed=False
        async def replay():
            nonlocal replayed
            if replayed:return await receive()
            replayed=True
            return {'type':'http.request','body':b''.join(chunks),'more_body':False}
        await self.app(scope,replay,send)


def secure_local(app, port):
    """Origin + custom-header CSRF guard; no UI credential cookie to persist."""
    from fastapi.responses import JSONResponse
    origin = f'http://127.0.0.1:{port}'
    app.add_middleware(LocalBodyLimit)

    @app.middleware('http')
    async def boundary(request, call_next):
        if request.headers.get('host') != f'127.0.0.1:{port}':
            return JSONResponse({'error': 'Invalid host'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'error': 'Cross-site access denied'}, status_code=403)
        if request.url.path.startswith('/local/') and request.method != 'GET':
            if (request.headers.get('origin') != origin or request.headers.get('x-local-action') != '1'):
                return JSONResponse({'error': 'Local session required'}, status_code=403)
        try:
            response = await call_next(request)
        except Exception:
            return JSONResponse({'error': 'Operation failed; no diagnostic secrets returned'}, status_code=500)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'"
        return response


class OneAccount:
    """Exactly one identity per process. Renewal replaces values in memory only."""
    def __init__(self, identity, environment_name=None):
        self.identity = identity
        self.environment_name = environment_name
        self.cookies = {}
        self.expires = {}
        self.available = False

    def inject(self, cookies, expires=None):
        required = ('hatch_sess', 'hatch_gw', 'hatch_vml', 'hatch_native_auth_device')
        if not isinstance(cookies, dict) or not all(isinstance(cookies.get(k), str) and cookies[k] for k in required):
            raise ValueError('Four required Muse cookies are missing')
        if any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9_]+', k) or
               not isinstance(v, str) or any(c in v for c in ('\r', '\n', ';')) for k, v in cookies.items()):
            raise ValueError('Invalid cookies')
        self.cookies = dict(cookies)
        self.expires = dict(expires or {})
        self.available = True
        if self.environment_name:
            os.environ[self.environment_name] = json.dumps(self.cookies, separators=(',', ':'))

    def renew(self, result):
        self.inject(result['cookies'], result.get('cookies_exp', self.expires))

    def invalidate(self):
        self.available = False

    def clear(self):
        self.cookies.clear()
        self.expires.clear()
        self.available = False
        if self.environment_name:
            os.environ.pop(self.environment_name, None)

