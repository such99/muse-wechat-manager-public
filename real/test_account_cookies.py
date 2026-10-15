"""Login cookies accepted by capture, renewal and saved-session restoration."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OneAccount
from strict_engine import StrictEngine


def login_cookies():
    return dict.fromkeys(('hatch_sess', 'hatch_gw', 'hatch_vml',
                          'hatch_native_auth_device'), 'FAKE-COOKIE')


def test_prefixed_login_cookies_survive_capture_renewal_and_restore():
    cookies = login_cookies() | {'__Host-hatch_one_tap': 'FAKE-ONE-TAP',
                                 '__Secure-hatch_test': 'FAKE-SECURE'}
    account = OneAccount('one')
    account.inject(cookies)
    assert account.available and account.cookies == cookies
    account.renew({'cookies': cookies | {'__Host-hatch_one_tap': 'FAKE-RENEWED'}})
    restored = OneAccount('one')
    restored.inject(account.cookies, account.expires)
    assert restored.available
    assert restored.cookies['__Host-hatch_one_tap'] == 'FAKE-RENEWED'


@pytest.mark.parametrize('name', ['bad name', 'bad;name', 'bad=name',
                                  'bad\rname', 'bad\nname', '', '坏名称'])
def test_malformed_cookie_names_still_rejected(name):
    account = OneAccount('one')
    with pytest.raises(ValueError):
        account.inject(login_cookies() | {name: 'FAKE-VALUE'})
    assert not account.available


@pytest.mark.parametrize('value', ['bad\rvalue', 'bad\nvalue', 'bad;value'])
def test_cookie_value_injection_still_rejected(value):
    with pytest.raises(ValueError):
        OneAccount('one').inject(login_cookies() | {'__Host-hatch_one_tap': value})


def test_optional_prefixed_cookie_does_not_replace_required_authentication():
    cookies = login_cookies() | {'__Host-hatch_one_tap': 'FAKE-ONE-TAP'}
    del cookies['hatch_sess']
    with pytest.raises(ValueError):
        OneAccount('one').inject(cookies)


def test_restored_host_prefixed_cookies_keep_host_only_scope():
    calls = []
    engine = SimpleNamespace(current_context_id='FAKE-ISOLATED-CONTEXT',
                             browser=SimpleNamespace(send=lambda *args: calls.append(args)))
    StrictEngine._apply_cookies(engine, None,
                               login_cookies() | {'__Host-hatch_one_tap': 'FAKE-ONE-TAP'})
    assert [method for method, _ in calls] == ['Storage.clearCookies', 'Storage.setCookies']
    assert all(params['browserContextId'] == 'FAKE-ISOLATED-CONTEXT' for _, params in calls)
    cookies = {item['name']: item for item in calls[1][1]['cookies']}
    host = cookies['__Host-hatch_one_tap']
    assert 'domain' not in host
    assert host['url'] == 'https://muse.ai/' and host['path'] == '/' and host['secure']
    assert cookies['hatch_sess']['domain'] == '.muse.ai'
