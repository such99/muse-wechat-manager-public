"""Use the requested Muse2API engine, but never its disk store or unsafe browser fallback."""
import os
import json
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import psutil
from common import ROOT, CODE_ROOT, browser_environment

sys.path.insert(0, str(CODE_ROOT / 'vendor' / 'muse2api-live'))
from cdp import CDP
from engine import MuseEngine, MuseAuthError, MuseGenerationError


def authentication_failure_code(error):
    """Return only allowlisted diagnostics; never return upstream exception text."""
    message = str(error)
    for fragment, code in (
        ('Known essential Muse Cookie expired', 'essential_cookie_expired'),
        ('/api/session HTTP 401', 'session_http_401'),
        ('Required Cookie disappeared', 'required_cookie_missing'),
        ('会话已被 muse.ai 登出', 'browser_login_required'),
    ):
        if fragment in message:
            return code
    return 'account_authentication_failed'


class StrictEngine(MuseEngine):
    def __init__(self, instance, headless=False):
        self.instance = instance
        self.headless = headless
        self.browser_verified = False
        chrome = os.getenv('MUSE_CHROMIUM', r'C:\Program Files\Google\Chrome\Application\chrome.exe')
        root = instance['runtime']
        cfg = SimpleNamespace(chromium=chrome, cdp_port=instance['cdp_port'],
            profile_dir=str(root / 'blank-browser-profile'), data_dir=str(root),
            download_dir=str(root / 'downloads-disabled'), media_dir=str(root),
            site_url=instance['site'], login_wait=120, chat_timeout=180)
        super().__init__(cfg)

    def start(self):
        if self.proc and self.proc.poll() is None and self.browser:
            return
        if not os.path.isfile(self.cfg.chromium):
            raise RuntimeError('Chrome executable missing')
        # Never attach to someone else's existing browser/CDP session.
        with socket.socket() as check:
            check.bind(('127.0.0.1', self.cfg.cdp_port))
        args = [self.cfg.chromium, '--incognito', '--disable-background-networking',
            '--no-first-run', '--no-default-browser-check', '--disable-sync',
            '--disable-crash-reporter', '--disable-breakpad', '--disable-logging',
            '--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={self.cfg.cdp_port}',
            f'--remote-allow-origins=http://127.0.0.1:{self.cfg.cdp_port}',
            f'--user-data-dir={self.cfg.profile_dir}', 'about:blank']
        if self.headless:
            args.insert(1, '--headless=new')
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=browser_environment())
        with httpx.Client(trust_env=False, verify=False, timeout=1) as client:
            for _ in range(100):
                if self.proc.poll() is not None:
                    break
                try:
                    version = client.get(self._debug_url()).json()
                    self.browser = self._connect(version['webSocketDebuggerUrl'])
                    self.verify_browser()
                    return
                except Exception:
                    time.sleep(.1)
        self.stop()
        raise RuntimeError('Isolated Chrome did not start')

    @staticmethod
    def _connect(url):
        connection = CDP(url, timeout=30)
        connection.ws.settimeout(1)  # let CDP command deadlines take effect
        return connection

    def verify_browser(self):
        if not self.proc or self.proc.poll() is not None:
            raise RuntimeError('Instance browser is not running')
        owner = psutil.Process(self.proc.pid)
        expected = '--user-data-dir=' + self.cfg.profile_dir
        if expected not in owner.cmdline():
            raise RuntimeError('Instance browser profile mismatch')
        owned = {owner.pid} | {p.pid for p in owner.children(recursive=True)}
        listeners = [c for c in psutil.net_connections(kind='tcp')
            if c.status == psutil.CONN_LISTEN and c.laddr.port == self.cfg.cdp_port]
        if not listeners or any(c.laddr.ip not in ('127.0.0.1', '::1') or c.pid not in owned for c in listeners):
            self.browser_verified = False
            raise RuntimeError('CDP listener is not loopback or belongs to another process')
        self.browser_verified = True

    def stop(self):
        # Ask Chrome to close all contexts gracefully; parent upstream termination is a fallback.
        if self.browser:
            try:
                self.browser.ws.settimeout(2)
                self.browser.send('Browser.close', timeout=2)
            except Exception:
                pass
            # Browser.close tears down the socket. Never send dispose commands over
            # that dead socket: upstream CDP would wait its full 180-second timeout.
            self.browser.close()
            self.browser = None
            self._active_contexts.clear()
            if self.page:
                self.page.close()
                self.page = None
            self.current_context_id = None
        if self.proc:
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass
        super().stop()
        self.browser_verified = False

    def _open_page(self):
        # Deliberately no ordinary-tab fallback. BrowserContext is off the record.
        if not self.browser:
            raise RuntimeError('Browser not ready')
        self.verify_browser()
        ctx = self.browser.send('Target.createBrowserContext', {'disposeOnDetach': True})['result']['browserContextId']
        self.current_context_id = ctx
        self._active_contexts.add(ctx)
        try:
            self.browser.send('Browser.setDownloadBehavior', {'behavior': 'deny', 'browserContextId': ctx})
            tid = self.browser.send('Target.createTarget', {'url': 'about:blank',
                'browserContextId': ctx, 'newWindow': True})['result']['targetId']
            page = self._connect(f'ws://127.0.0.1:{self.cfg.cdp_port}/devtools/page/{tid}')
            for method in ('Network.enable', 'Page.enable', 'Runtime.enable'):
                page.send(method)
            self.target_id = tid
            return page
        except Exception:
            self._close_page()
            raise RuntimeError('Off-record context failed; normal tab fallback prohibited') from None

    def login_window(self):
        self.start()
        self._close_page()
        self.page = self._open_page()
        self.page.send('Page.navigate', {'url': self.cfg.site_url})

    def capture(self):
        self.verify_browser()
        # OAuth may finish in another target in the SAME incognito context.
        targets = self.browser.send('Target.getTargets')['result']['targetInfos']
        for target in targets:
            if (target.get('browserContextId') == self.current_context_id and
                    target.get('url', '').startswith('https://muse.ai/') and target.get('type') == 'page'):
                self.page.close()
                self.page = self._connect(f"ws://127.0.0.1:{self.cfg.cdp_port}/devtools/page/{target['targetId']}")
                break
        if not self.page or not self.page.js("!!document.querySelector('textarea')"):
            raise RuntimeError('Finish login in the isolated Muse window first')
        result = self.read_cookies()
        self.current_acc_id = self.instance['instance']
        self._last_http_renew[self.current_acc_id] = time.time()
        return ({k: v['value'] for k, v in result.items()},
                {k: v['expires'] for k, v in result.items() if v['expires'] > 0})

    def _apply_cookies(self, page, cookies, expires=None):
        if not self.current_context_id:
            raise RuntimeError('Off-record context missing')
        jar = []
        for name, value in cookies.items():
            if not value:
                continue
            entry = {'name': name, 'value': value, 'domain': '.muse.ai', 'path': '/', 'secure': True}
            expiry = (expires or {}).get(name)
            if expiry and float(expiry) > 0:
                if float(expiry) <= time.time():
                    if name in ('hatch_sess', 'hatch_gw', 'hatch_vml', 'hatch_native_auth_device'):
                        raise MuseAuthError('Known essential Muse Cookie expired; authenticate this account again')
                    continue  # Expired optional cookies do not establish account failure.
                entry['expires'] = float(expiry)
            jar.append(entry)
        self.browser.send('Storage.clearCookies', {'browserContextId': self.current_context_id})
        self.browser.send('Storage.setCookies', {'browserContextId': self.current_context_id, 'cookies': jar})

    def read_cookies(self):
        if not self.current_context_id or not self.browser:
            return {}
        result = self.browser.send('Storage.getCookies', {'browserContextId': self.current_context_id})
        return {c['name']: {'value': c['value'], 'expires': int(c.get('expires', -1))}
            for c in result['result']['cookies'] if c.get('domain', '').lstrip('.') == 'muse.ai'}

    def renew_for_account(self, account):
        result = super().renew_session_http(account.cookies, account.expires, wake_vm=True)
        account.renew(result)
        self._last_http_renew[account.identity] = time.time()
        if self.page:
            self._apply_cookies(self.page, account.cookies, account.expires)

    def _debug_dump(self, tag):
        pass  # Never persist page bodies, account details or replies on failure.

    def reset_thread(self, for_chat=False):
        # Each OpenAI request supplies its own explicit history. Avoid hidden extra
        # context accumulating in a reused webpage thread beyond that history.
        self.page.send('Page.navigate', {'url': 'https://muse.ai/thread/new'})
        deadline = time.time() + 25
        while time.time() < deadline:
            if self.page.js("document.readyState==='complete' && !!document.querySelector('textarea') && document.querySelectorAll('div[class*=hatch-chat-groupable-bubble]').length===0"):
                if not self._wait_ws_ready(self.page, timeout=15):
                    raise MuseGenerationError('Muse connection not ready; message not submitted')
                return
            time.sleep(.15)
        raise MuseGenerationError('Fresh Muse thread not ready; message not submitted')

    def _send(self, prompt):
        # Exactly one submission. No Ctrl+Enter/Enter fallback after an uncertain click.
        self.verify_browser()
        length = self.page.js("""(function(t){let a=document.querySelector('textarea');
            if(!a)return -1;a.focus();let s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
            s.call(a,t);a.dispatchEvent(new Event('input',{bubbles:true}));
            a.dispatchEvent(new Event('change',{bubbles:true}));return a.value.length})(%s)""" % json.dumps(prompt))
        # JavaScript counts UTF-16 code units, including two units for emoji.
        if length != len(prompt.encode('utf-16-le')) // 2:
            raise MuseGenerationError('Composer rejected or truncated input; not submitted')
        deadline = time.time() + 5
        while time.time() < deadline:
            outcome = self.page.js("""(function(){let b=[...document.querySelectorAll('button,[role=button]')]
                .filter(x=>x.offsetParent!==null).find(x=>/发送|send/i.test(x.getAttribute('aria-label')||'')
                ||/发送|send/i.test(x.getAttribute('data-testid')||''));
                if(!b||b.disabled)return false;b.click();return true})()""")
            if outcome is True:
                return 'clicked'
            time.sleep(.1)
        raise MuseGenerationError('Send button not ready; no keyboard fallback')

    def chat_stream(self, cookies, prompt, expires=None, timeout=None, account_id=None, stop_event=None):
        # Keep the requested upstream DOM parser, but require observed generation end.
        self.ensure_page(cookies, expires, account_id=account_id)
        self.reset_thread(for_chat=True)
        baseline, previous, _ = self._poll_chat()
        self._send(prompt)
        deadline = time.time() + (timeout or 180)
        stable, started = 0, False
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise MuseGenerationError('Generation cancelled')
            time.sleep(.1)
            count, current, has_stop = self._poll_chat()
            if not started and (not current or (count <= baseline and current == previous)):
                continue
            started = True
            if current != previous:
                previous, stable = current, 0
            else:
                stable += 1
                if current and not has_stop and stable >= 5:
                    yield current
                    return
        # A nonempty partial response while the Stop button remains is NOT success.
        raise MuseGenerationError('Generation completion not confirmed before timeout')

    def reply(self, account, prompt):
        if not account.available:
            raise MuseAuthError('This instance needs authentication')
        self.start()
        self.verify_browser()
        try:
            if time.time() - self._last_http_renew.get(account.identity, 0) > 600:
                self.renew_for_account(account)
            text = super().chat(account.cookies, prompt, account.expires, timeout=180,
                account_id=account.identity)
            updated = self.read_cookies()
            if updated:
                required = ('hatch_sess', 'hatch_gw', 'hatch_vml', 'hatch_native_auth_device')
                if any(not updated.get(k, {}).get('value') for k in required):
                    raise MuseAuthError('Required Cookie disappeared from this session')
                account.renew({'cookies': {k: v['value'] for k, v in updated.items()},
                    'cookies_exp': {k: v['expires'] for k, v in updated.items() if v['expires'] > 0}})
            if not text.strip():
                raise RuntimeError('Empty upstream reply')
            return text
        except MuseAuthError:
            account.invalidate()
            self._close_page()
            raise
