"""Current-user DPAPI secrets. Plaintext is available only to transport internals."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re

from .config import atomic_json, config_lock, product_path, read_json
from .types import EndpointConfig, LlmFailure


def mask_secret(value: str) -> str:
    # No suffix: even very short credentials must not be disclosed.
    return '********' if value else ''


def environment_name(token_ref: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9_]+', token_ref):
        raise LlmFailure('llm:token_ref_invalid', outcome='not_sent')
    return 'STYLELORA_LLM_TOKEN_' + token_ref.upper()


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _windows():
    if os.name != 'nt':
        raise LlmFailure('llm:dpapi_unavailable', outcome='not_sent')


def _crypt(data: bytes, decrypt: bool = False) -> bytes:
    _windows()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _Blob(len(data), buffer)
    target = _Blob()
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN only; never CRYPTPROTECT_LOCAL_MACHINE.
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise LlmFailure('llm:secret_unavailable', outcome='not_sent')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def _restrict_file(path: Path) -> None:
    """Protected DACL: only the file owner receives access (OWNER RIGHTS SID)."""
    _windows()
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    set_security = advapi.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not convert('D:P(A;;FA;;;OW)', 1, ctypes.byref(descriptor), None):
        raise LlmFailure('llm:secret_permissions', outcome='not_sent')
    try:
        if not set_security(str(path), 0x80000004, descriptor):
            raise LlmFailure('llm:secret_permissions', outcome='not_sent')
    finally:
        kernel.LocalFree(descriptor)


class SecretStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else product_path('LOCALAPPDATA', 'secrets.json')

    def put_secret(self, token_ref: str, plaintext: str) -> None:
        key = environment_name(token_ref).removeprefix('STYLELORA_LLM_TOKEN_')
        if not plaintext:
            raise LlmFailure('llm:secret_empty', outcome='not_sent')
        encrypted = base64.b64encode(_crypt(plaintext.encode('utf-8'))).decode('ascii')
        try:
            with config_lock(self.path):
                document = read_json(self.path, {})
                if document.get(key) is not None:
                    previous = _crypt(base64.b64decode(document[key], validate=True), True)
                    if previous == plaintext.encode('utf-8'):
                        return
                document[key] = encrypted
                atomic_json(self.path, document, secure=_restrict_file)
        except (ValueError, TypeError, OSError):
            raise LlmFailure('llm:secret_unavailable', outcome='not_sent') from None

    def resolve_secret(self, token_ref: str) -> str:
        """Internal-only plaintext accessor; never serialize its return value."""
        env = environment_name(token_ref)
        if env in os.environ:
            if os.environ[env]:
                return os.environ[env]
            raise LlmFailure('llm:secret_empty', outcome='not_sent')
        try:
            encrypted = read_json(self.path, {}).get(env.removeprefix('STYLELORA_LLM_TOKEN_'))
            if not encrypted:
                raise LlmFailure('llm:secret_missing', outcome='not_sent')
            return _crypt(base64.b64decode(encrypted, validate=True), True).decode('utf-8')
        except (ValueError, TypeError, OSError):
            raise LlmFailure('llm:secret_unavailable', outcome='not_sent') from None

    def has_secret(self, token_ref: str) -> bool:
        try:
            return bool(self.resolve_secret(token_ref))
        except LlmFailure:
            return False

    def public_endpoint(self, endpoint: EndpointConfig) -> dict:
        return {'endpoint_id': endpoint.endpoint_id, 'endpoint': '********',
                'protocol': endpoint.protocol, 'revision': endpoint.revision,
                'has_secret': any(self.has_secret(ref) for ref in endpoint.token_refs),
                'models': [m.name for m in endpoint.models]}


def put_secret(token_ref: str, plaintext: str, *, path: Path | None = None) -> None:
    SecretStore(path).put_secret(token_ref, plaintext)


def resolve_secret(token_ref: str, *, path: Path | None = None) -> str:
    return SecretStore(path).resolve_secret(token_ref)
