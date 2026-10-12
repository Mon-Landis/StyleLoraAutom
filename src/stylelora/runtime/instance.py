"""OS-owned product mutex and retained loopback socket."""
import ctypes
from ctypes import wintypes
import hashlib
import os
import socket
from .contracts import Failure


class InstanceLock:
    def __init__(self, name=None):
        if os.name != 'nt':
            raise Failure(503, 'dependency:unavailable')
        # GetUserNameEx is not sufficient: the mutex name is derived from the token SID.
        advapi, kernel = ctypes.WinDLL('advapi32', use_last_error=True), ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise OSError(ctypes.get_last_error())
        try:
            length = wintypes.DWORD()
            advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
            buffer = ctypes.create_string_buffer(length.value)
            if not advapi.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
                raise OSError(ctypes.get_last_error())
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            advapi.GetLengthSid.argtypes = [ctypes.c_void_p]
            sid_bytes = ctypes.string_at(sid, advapi.GetLengthSid(sid))
        finally:
            kernel.CloseHandle(token)
        self.kernel = kernel
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        descriptor = ctypes.c_void_p()
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:P(A;;GA;;;OW)', 1, ctypes.byref(descriptor), None):
            raise OSError(ctypes.get_last_error())
        class SecurityAttributes(ctypes.Structure):
            _fields_ = [('length', wintypes.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', wintypes.BOOL)]
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        try:
            self.handle = kernel.CreateMutexW(ctypes.byref(attributes), False, name or 'Local\\StyleLoraAutom-' + hashlib.sha256(sid_bytes).hexdigest()[:24])
        finally:
            kernel.LocalFree(descriptor)
        if not self.handle:
            raise OSError(ctypes.get_last_error())
        result = kernel.WaitForSingleObject(self.handle, 0)
        if result not in (0, 0x80):
            kernel.CloseHandle(self.handle)
            self.handle = None
            raise Failure(423, 'process:locked')
        self.abandoned = result == 0x80

    def close(self):
        if self.handle:
            self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def bind_socket(*, host='127.0.0.1', port=0, workers=1, reload=False):
    if host != '127.0.0.1' or workers != 1 or reload or type(port) is not int or not 0 <= port <= 65535:
        raise Failure(422, 'input:invalid')
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((host, port))
        sock.listen(128)
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise
