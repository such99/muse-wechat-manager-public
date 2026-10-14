"""Small offline fixtures: no saved instances, browser, or account required."""
import os
import shutil

import pytest


def node_executable():
    """Use an explicit executable or a normal Node.js installation on PATH."""
    configured = os.environ.get('NODE_BINARY')
    executable = shutil.which(configured or 'node')
    if not executable:
        pytest.fail('Node.js is required for adapter tests; install it or set NODE_BINARY.')
    return executable


def cookies(value):
    return {key: value for key in (
        'hatch_sess', 'hatch_gw', 'hatch_vml', 'hatch_native_auth_device')}


def service_settings(runtime):
    return {
        'instance': 'fixture', 'site': 'https://muse.ai/', 'model': 'muse-spark',
        'muse_port': 18611, 'bridge_port': 18612, 'cdp_port': 18613,
        'runtime': runtime, 'logging': {'enabled': False},
        'routing': {'mode': 'native_main', 'forward_all': True},
    }


class FakeEngine:
    def __init__(self):
        self._last_http_renew = {}

    def stop(self):
        pass
