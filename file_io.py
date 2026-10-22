"""Bounded JSON mailbox reads that permit atomic replacement on Windows."""
import json
import os
import sys


def replace_file(source, target):
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        replace = kernel.ReplaceFileW
        replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                            wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
        replace.restype = wintypes.BOOL
        if replace(str(target), str(source), None, 0, None, None):
            return
        code = ctypes.get_last_error()
        if code not in (2, 3):
            raise ctypes.WinError(code)
        # First publication has no destination to replace. Both paths are in
        # the same directory/volume, so rename remains atomic.
    os.replace(source, target)


def open_shared_binary(path):
    if sys.platform != "win32":
        return open(path, "rb")
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    close = kernel.CloseHandle
    close.argtypes, close.restype = [ctypes.c_void_p], wintypes.BOOL
    # FILE_SHARE_DELETE is required for os.replace while this reader is open.
    handle = create(str(path), 0x80000000, 1 | 2 | 4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    try:
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def read_json_shared(path, max_bytes=65536):
    with open_shared_binary(path) as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("Oversized mailbox message")
    return json.loads(raw.decode("utf-8"))
