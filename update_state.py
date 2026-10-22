"""Short-lived installation exclusion; no calendar reads or process killing."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os

APP_MUTEX = "Local\\DesktopCalendarWidgetV2"
UPDATE_MUTEX = "Local\\DesktopCalendarInstallV1"


def _kernel():
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def update_in_progress() -> bool:
    if os.name != "nt":
        return False
    kernel = _kernel()
    handle = kernel.OpenMutexW(0x00100000, False, UPDATE_MUTEX)
    if handle:
        kernel.CloseHandle(handle)
        return True
    error = kernel.GetLastError()
    if error not in (0, 2):
        raise OSError(error, "无法确认安装更新状态，请稍后重试。")
    return False


@contextmanager
def reserve_installation():
    """Hold the original app mutex too, so already-released apps are excluded.

    This is a named-object *lifetime* guard, matching SingleInstance's existing
    protocol (CreateMutex(False), ERROR_ALREADY_EXISTS), not mutex ownership.
    Close every handle before starting the replacement app.
    """
    if os.name != "nt":
        yield
        return
    kernel, handles = _kernel(), []
    try:
        for name in (UPDATE_MUTEX, APP_MUTEX):
            kernel.SetLastError(0)
            handle = kernel.CreateMutexW(None, False, name)
            if not handle:
                raise OSError(kernel.GetLastError(), "无法取得更新安装保护，已取消更新。")
            handles.append(handle)
            if kernel.GetLastError() == 183:
                raise RuntimeError("另一个月历或更新器仍在运行，更新尚未完成，已取消覆盖安装。")
        yield
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)
