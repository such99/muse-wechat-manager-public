"""Per-Windows-user DPAPI storage. Plaintext never falls back to disk."""
import ctypes
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from ctypes import wintypes
from contextlib import contextmanager


class VaultError(ValueError):
    pass


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def protect(raw, purpose, decrypt=False):
    if os.name != 'nt':
        raise VaultError('加密凭据仅支持 Windows；没有明文保存方式。')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    def blob(value):
        buf = ctypes.create_string_buffer(value)
        return buf, Blob(len(value), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    source_buf, source = blob(raw)
    entropy_buf, entropy = blob(('MuseWeChatManager/v1/' + purpose).encode())
    out = Blob()
    if not fn(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(out)):
        raise VaultError('凭据无法解密或保存；请使用原 Windows 用户，或重新登录此实例。')
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        kernel.LocalFree(out.data)


class Vault:
    def __init__(self, root):
        self.directory = Path(root).resolve() / 'secrets'
        self.lock = threading.RLock()

    def path(self, purpose):
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,30}\.(muse|wechat|mimo)', purpose):
            raise VaultError('Invalid credential scope')
        if self.directory.is_symlink() or self.directory.is_junction():
            raise VaultError('Credential directory must not be a link')
        return self.directory / (purpose + '.dpapi')

    def read(self, purpose):
        with self.lock:
            path = self.path(purpose)
            if not path.exists():
                return None
            if path.is_symlink() or path.stat().st_size > 262144:
                raise VaultError('Invalid encrypted credential file')
            try:
                item = json.loads(protect(path.read_bytes(), purpose, decrypt=True))
                if item['scope'] != purpose or not isinstance(item['data'], dict):
                    raise ValueError()
                return item['data']
            except VaultError:
                raise
            except Exception:
                raise VaultError('加密凭据损坏，请重新登录对应实例。') from None

    def write(self, purpose, data):
        with self.lock, self.owner_lock():
            path = self.path(purpose)
            category = purpose.rsplit('.', 1)[1]
            identity_key = {'muse': 'account_owner', 'wechat': 'user_id'}.get(category)
            identity = data.get(identity_key) if identity_key else None
            if identity:
                # Serialize cross-process claims: the same login cannot be bound
                # to two manager instances, even when they authenticate together.
                if self.directory.exists():
                    for other in self.directory.glob('*.' + category + '.dpapi'):
                        if other == path: continue
                        existing = self.read(other.name.removesuffix('.dpapi'))
                        if existing and existing.get(identity_key) == identity:
                            raise VaultError('此账号已属于另一组实例，请使用不同账号或先解除原实例。')
            raw = json.dumps({'scope': purpose, 'data': data}, ensure_ascii=False, allow_nan=False).encode()
            if len(raw) > 131072:
                raise VaultError('Credential size limit')
            encrypted = protect(raw, purpose)
            self.directory.mkdir(parents=True, exist_ok=True)
            pending = path.with_suffix('.pending')
            pending.write_bytes(encrypted)
            os.replace(pending, path)

    @contextmanager
    def owner_lock(self):
        if os.name != 'nt': raise VaultError('Credential storage requires Windows')
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        api.CreateMutexW.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.ReleaseMutex.argtypes = [wintypes.HANDLE]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        name = 'Local\\MuseCredentials_' + hashlib.sha256(str(self.directory).lower().encode()).hexdigest()
        handle = api.CreateMutexW(None, False, name)
        if not handle: raise VaultError('无法建立凭据写入锁。')
        acquired = False
        try:
            acquired = api.WaitForSingleObject(handle, 10000) in (0, 128)
            if not acquired: raise VaultError('凭据正在更新，请稍后重试。')
            yield
        finally:
            if acquired: api.ReleaseMutex(handle)
            api.CloseHandle(handle)

    def delete(self, purpose):
        with self.lock:
            self.path(purpose).unlink(missing_ok=True)
