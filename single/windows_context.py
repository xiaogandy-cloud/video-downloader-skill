"""Read the current token only; never alter privileges or relaunch automatically."""
import ctypes
import os
from ctypes import wintypes


def restricted_process():
    if os.name != 'nt':
        return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.IsTokenRestricted.argtypes = [wintypes.HANDLE]
    security.IsTokenRestricted.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return None
    try:
        ctypes.set_last_error(0)
        restricted = security.IsTokenRestricted(token)
        return bool(restricted) if restricted or not ctypes.get_last_error() else None
    finally:
        kernel.CloseHandle(token)
