"""Native Win32 local pipes and access checks, loaded only on Windows.

Uses documented Win32 APIs. Pipes reject remote connections and verify both
process identities. JSON bytes are used throughout; no pickle deserialization.
"""
import asyncio
import ctypes
import functools
import os
import stat
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes as w
from pathlib import Path

INVALID_HANDLE = ctypes.c_void_p(-1).value
READ_CONTROL = 0x00020000
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
REPARSE = 0x400
TRUSTED_SIDS = {"S-1-5-18", "S-1-5-32-544", "S-1-3-0"}
SYNCHRONIZE, PROCESS_QUERY_LIMITED_INFORMATION = 0x00100000, 0x1000
WAIT_TIMEOUT = 0x102
ERROR_ACCESS_DENIED, ERROR_NO_DATA, ERROR_PIPE_CONNECTED = 5, 232, 535


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", w.BOOL)]


class ACL(ctypes.Structure):
    _fields_ = [("revision", w.BYTE), ("sbz1", w.BYTE), ("size", w.WORD), ("count", w.WORD), ("sbz2", w.WORD)]


class FileInfo(ctypes.Structure):
    _fields_ = [("attributes", w.DWORD), ("created", w.FILETIME), ("accessed", w.FILETIME),
                ("written", w.FILETIME), ("volume", w.DWORD), ("size_high", w.DWORD),
                ("size_low", w.DWORD), ("links", w.DWORD), ("index_high", w.DWORD), ("index_low", w.DWORD)]


class API:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Win32 support is available only on Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        ptr = ctypes.c_void_p
        self.bind(self.kernel, "CloseHandle", [w.HANDLE], w.BOOL)
        self.bind(self.kernel, "LocalFree", [ptr], ptr)
        self.bind(self.kernel, "GetCurrentProcess", [], w.HANDLE)
        self.bind(self.kernel, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
        self.bind(self.kernel, "WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD)
        self.bind(self.advapi, "OpenProcessToken", [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL)
        self.bind(self.advapi, "GetTokenInformation", [w.HANDLE, ctypes.c_int, ptr, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL)
        self.bind(self.advapi, "IsTokenRestricted", [w.HANDLE], w.BOOL)
        self.bind(self.advapi, "ConvertSidToStringSidW", [ptr, ctypes.POINTER(w.LPWSTR)], w.BOOL)
        self.bind(self.advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [w.LPCWSTR, w.DWORD, ctypes.POINTER(ptr), ctypes.POINTER(w.DWORD)], w.BOOL)
        self.bind(self.advapi, "GetSecurityDescriptorDacl", [ptr, ctypes.POINTER(w.BOOL), ctypes.POINTER(ptr), ctypes.POINTER(w.BOOL)], w.BOOL)
        self.bind(self.advapi, "SetNamedSecurityInfoW", [w.LPWSTR, ctypes.c_int, w.DWORD, ptr, ptr, ptr, ptr], w.DWORD)
        self.bind(self.advapi, "GetSecurityInfo", [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.POINTER(ptr), ctypes.POINTER(ptr), ctypes.POINTER(ptr), ctypes.POINTER(ptr), ctypes.POINTER(ptr)], w.DWORD)
        self.bind(self.advapi, "GetAce", [ptr, w.DWORD, ctypes.POINTER(ptr)], w.BOOL)
        self.bind(self.kernel, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(SecurityAttributes), w.DWORD, w.DWORD, w.HANDLE], w.HANDLE)
        self.bind(self.kernel, "GetFileInformationByHandle", [w.HANDLE, ctypes.POINTER(FileInfo)], w.BOOL)
        self.bind(self.kernel, "CreateNamedPipeW", [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(SecurityAttributes)], w.HANDLE)
        self.bind(self.kernel, "ConnectNamedPipe", [w.HANDLE, ptr], w.BOOL)
        self.bind(self.kernel, "DisconnectNamedPipe", [w.HANDLE], w.BOOL)
        self.bind(self.kernel, "WaitNamedPipeW", [w.LPCWSTR, w.DWORD], w.BOOL)
        self.bind(self.kernel, "GetNamedPipeClientProcessId", [w.HANDLE, ctypes.POINTER(w.ULONG)], w.BOOL)
        self.bind(self.kernel, "GetNamedPipeServerProcessId", [w.HANDLE, ctypes.POINTER(w.ULONG)], w.BOOL)
        self.bind(self.kernel, "PeekNamedPipe", [w.HANDLE, ptr, w.DWORD, ptr, ctypes.POINTER(w.DWORD), ptr], w.BOOL)
        self.bind(self.kernel, "ReadFile", [w.HANDLE, ptr, w.DWORD, ctypes.POINTER(w.DWORD), ptr], w.BOOL)
        self.bind(self.kernel, "WriteFile", [w.HANDLE, ptr, w.DWORD, ctypes.POINTER(w.DWORD), ptr], w.BOOL)

    @staticmethod
    def bind(dll, name, args, result):
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, result

    @staticmethod
    def check(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def handle(self, value):
        if value in (None, 0, INVALID_HANDLE):
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    def sid_text(self, sid):
        text = w.LPWSTR()
        self.check(self.advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)))
        try:
            return text.value
        finally:
            self.kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    def process_sid(self, pid=None, require_unrestricted=False):
        process = self.kernel.GetCurrentProcess() if pid is None else self.handle(self.kernel.OpenProcess(0x1000, False, pid))
        token = w.HANDLE()
        try:
            self.check(self.advapi.OpenProcessToken(process, 0x8, ctypes.byref(token)))
            if require_unrestricted:
                container, returned = w.DWORD(), w.DWORD()
                self.check(self.advapi.GetTokenInformation(token, 29, ctypes.byref(container), ctypes.sizeof(container), ctypes.byref(returned)))
                if container.value or self.advapi.IsTokenRestricted(token):
                    raise PermissionError("Sandboxed/restricted processes cannot control the broker")
            size = w.DWORD()
            self.advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            self.check(self.advapi.GetTokenInformation(token, 1, buffer, size.value, ctypes.byref(size)))
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            return self.sid_text(sid)
        finally:
            if token:
                self.kernel.CloseHandle(token)
            if pid is not None:
                self.kernel.CloseHandle(process)

    @contextmanager
    def descriptor(self, directory=False):
        flags = "OICI" if directory else ""
        sddl = f"D:P(A;{flags};GA;;;{current_sid()})(A;{flags};GA;;;SY)"
        descriptor = ctypes.c_void_p()
        self.check(self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None))
        try:
            yield SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        finally:
            self.kernel.LocalFree(descriptor)

    def check_access(self, handle, private=True):
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        error = self.advapi.GetSecurityInfo(handle, 1, 0x5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        try:
            if self.sid_text(owner) not in (current_sid(), "S-1-5-32-544") or not dacl:
                raise PermissionError("Expected a user-owned object with an explicit Windows DACL")
            trusted = TRUSTED_SIDS | {current_sid()}
            for index in range(ctypes.cast(dacl, ctypes.POINTER(ACL)).contents.count):
                ace = ctypes.c_void_p()
                self.check(self.advapi.GetAce(dacl, index, ctypes.byref(ace)))
                kind = ctypes.c_ubyte.from_address(ace.value).value
                if kind == 1:  # ACCESS_DENIED_ACE only narrows rights.
                    continue
                if kind != 0:
                    raise PermissionError("Cannot verify this Windows ACL entry type")
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                sid = self.sid_text(ctypes.c_void_p(ace.value + 8))
                # Generic/file writes, ACL/owner changes, directory child deletion.
                write_bits = 0x500D0156
                if sid not in trusted and (private or mask & write_bits):
                    raise PermissionError("Windows permissions grant another account access to private state or write access to configuration")
        finally:
            self.kernel.LocalFree(descriptor)


@functools.lru_cache(maxsize=1)
def api():
    return API()


@functools.lru_cache(maxsize=1)
def current_sid():
    return api().process_sid()


def process_running(pid):
    """Return whether a process is still running, without signalling it.

    os.kill() on Windows terminates the target, so it cannot be used as a
    liveness probe. A process object is signalled only after the process has
    exited and the system has closed all of its handles.
    """
    a = api()
    handle = a.kernel.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means the process exists but belongs to someone else.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        return a.kernel.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        a.kernel.CloseHandle(handle)


_PROTECTED = set()


def protect_directory(path):
    """Narrow a dedicated, current-user-owned application directory's DACL."""
    a = api()
    handle = open_handle(path, directory=True, share=3)
    try:
        # Owner validation before modifying any existing object's permissions.
        owner, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
        error = a.advapi.GetSecurityInfo(handle, 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        try:
            if a.sid_text(owner) not in (current_sid(), "S-1-5-32-544"):
                raise PermissionError("Private state directory belongs to another Windows account")
        finally:
            a.kernel.LocalFree(descriptor)
        key = os.path.normcase(str(path))
        if key in _PROTECTED:
            # Already narrowed by this process. Re-verify on every call, but do
            # not rewrite the DACL: that propagates inherited entries through
            # the whole state tree on each broker request.
            try:
                a.check_access(handle)
                return
            except PermissionError:
                _PROTECTED.discard(key)
        with a.descriptor(directory=True) as attributes:
            present, defaulted, dacl = w.BOOL(), w.BOOL(), ctypes.c_void_p()
            a.check(a.advapi.GetSecurityDescriptorDacl(attributes.lpSecurityDescriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)))
            error = a.advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None)
            if error:
                raise ctypes.WinError(error)
        a.check_access(handle)
        _PROTECTED.add(key)
    finally:
        a.kernel.CloseHandle(handle)


def open_handle(path, directory=False, share=1, access=None, disposition=3, attributes=None):
    a = api()
    access = access if access is not None else (READ_CONTROL | 0x80 if directory else GENERIC_READ | READ_CONTROL)
    flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT, BACKUP_SEMANTICS
    handle = a.handle(a.kernel.CreateFileW(str(path), access, share, ctypes.byref(attributes) if attributes else None,
                                         disposition, flags, None))
    try:
        info = FileInfo()
        a.check(a.kernel.GetFileInformationByHandle(handle, ctypes.byref(info)))
        if info.attributes & REPARSE:
            raise PermissionError("Reparse points/junctions are not allowed")
        if directory != bool(info.attributes & 0x10):
            raise PermissionError("Unexpected file type")
        return handle
    except Exception:
        a.kernel.CloseHandle(handle)
        raise


@contextmanager
def directory_chain(path, create=False):
    """Hold non-reparse directory handles without write/delete sharing."""
    handles = []
    current = Path(path.anchor)
    try:
        handles.append(open_handle(current, directory=True))
        for part in path.parts[1:]:
            current /= part
            if create:
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
            handles.append(open_handle(current, directory=True))
        yield
    finally:
        for handle in reversed(handles):
            api().kernel.CloseHandle(handle)


def open_fd(path, flags, private=False):
    import msvcrt
    a = api()
    write = bool(flags & (os.O_WRONLY | os.O_RDWR))
    read = not (flags & os.O_WRONLY) or bool(flags & os.O_RDWR)
    access = READ_CONTROL | (GENERIC_READ if read else 0) | (GENERIC_WRITE if write else 0)
    disposition = 1 if flags & os.O_CREAT and flags & os.O_EXCL else 4 if flags & os.O_CREAT else 3
    if flags & os.O_TRUNC:
        disposition = 2 if flags & os.O_CREAT else 5
    with directory_chain(path.parent):
        with a.descriptor() as attributes:
            handle = open_handle(path, share=3 if private else 1, access=access, disposition=disposition,
                                 attributes=attributes if flags & os.O_CREAT else None)
    try:
        if private:
            a.check_access(handle)
        fd = msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
    except Exception:
        a.kernel.CloseHandle(handle)
        raise
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise PermissionError("Expected a regular file with one link")
    return fd


def read_config(path):
    import msvcrt
    fd = open_fd(path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as stream:
        api().check_access(msvcrt.get_osfhandle(stream.fileno()), private=False)
        return stream.read().decode("utf-8")


def verify_peer(handle, server=False):
    pid = w.ULONG()
    method = api().kernel.GetNamedPipeServerProcessId if server else api().kernel.GetNamedPipeClientProcessId
    api().check(method(handle, ctypes.byref(pid)))
    if api().process_sid(pid.value, require_unrestricted=True) != current_sid():
        raise PermissionError("Named-pipe peer belongs to another Windows account")


def read_line(handle, maximum, timeout, cancelled=None):
    a, deadline, data = api(), time.monotonic() + timeout, bytearray()
    while time.monotonic() < deadline:
        if cancelled and cancelled.is_set():
            raise ConnectionAbortedError("Pipe operation cancelled")
        available = w.DWORD()
        a.check(a.kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None))
        if not available.value:
            time.sleep(0.01)
            continue
        buffer = ctypes.create_string_buffer(min(available.value, 65536, maximum + 1 - len(data)))
        count = w.DWORD()
        a.check(a.kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None))
        data.extend(buffer.raw[:count.value])
        if len(data) > maximum:
            raise ValueError("Named-pipe frame exceeds size limit")
        if b"\n" in data:
            line, extra = bytes(data).split(b"\n", 1)
            if extra.strip():
                raise ValueError("Only one request per pipe connection is accepted")
            return line
    raise TimeoutError("Local named-pipe operation timed out")


def write_line(handle, payload):
    data = payload + b"\n"
    sent = 0
    while sent < len(data):
        part = data[sent:sent + 65536]
        count = w.DWORD()
        api().check(api().kernel.WriteFile(handle, ctypes.c_char_p(part), len(part), ctypes.byref(count), None))
        if not count.value:
            raise BrokenPipeError("Pipe write made no progress")
        sent += count.value


def connect_pipe(address, timeout=2):
    a, deadline = api(), time.monotonic() + timeout
    while True:
        handle = a.kernel.CreateFileW(address, GENERIC_READ | GENERIC_WRITE, 0, None, 3, 0, None)
        if handle not in (None, 0, INVALID_HANDLE):
            try:
                verify_peer(handle, server=True)
                return handle
            except Exception:
                a.kernel.CloseHandle(handle)
                raise
        error = ctypes.get_last_error()
        if error in (2, 3):
            raise FileNotFoundError("Local broker pipe is not running")
        if error != 231 or time.monotonic() >= deadline:
            raise ctypes.WinError(error)
        a.kernel.WaitNamedPipeW(address, 100)


async def exchange(address, payload, maximum, timeout):
    loop, cancelled = asyncio.get_running_loop(), threading.Event()
    future = loop.create_future()

    def finish(result=None, error=None):
        if not future.done():
            future.set_exception(error) if error else future.set_result(result)

    def work():
        handle = None
        try:
            handle = connect_pipe(address)
            write_line(handle, payload)
            result = read_line(handle, maximum, timeout, cancelled)
            write_line(handle, b"ACK")
            if not loop.is_closed():
                loop.call_soon_threadsafe(finish, result)
        except Exception as exc:
            if not loop.is_closed():
                loop.call_soon_threadsafe(finish, None, exc)
        finally:
            if handle is not None:
                api().kernel.CloseHandle(handle)

    threading.Thread(target=work, daemon=True, name="mcp-pipe-client").start()
    try:
        return await asyncio.wait_for(future, timeout + 3)
    finally:
        cancelled.set()


class PipeServer:
    def __init__(self, address, handler, maximum):
        self.address, self.handler, self.maximum = address, handler, maximum
        self.loop = asyncio.get_running_loop()
        self.stopped = threading.Event()
        self.workers = set()
        self.workers_lock = threading.Lock()
        api().process_sid(require_unrestricted=True)
        self.listener = self.make_pipe(first=True)
        self.thread = threading.Thread(target=self.accept, daemon=True, name="mcp-pipe-listener")

    def make_pipe(self, first=False):
        with api().descriptor() as attributes:
            # PIPE_REJECT_REMOTE_CLIENTS is mandatory. This is never an SMB or
            # TCP transport. FIRST_PIPE_INSTANCE rejects a pre-existing owner.
            return api().handle(api().kernel.CreateNamedPipeW(self.address, 3 | (0x80000 if first else 0),
                0x8, 255, 65536, 65536, 1000, ctypes.byref(attributes)))

    def start(self):
        self.thread.start()

    def accept(self):
        handle = self.listener
        try:
            while not self.stopped.is_set():
                if not api().kernel.ConnectNamedPipe(handle, None):
                    error = ctypes.get_last_error()
                    if error == ERROR_NO_DATA:
                        # The client connected and left before it was accepted
                        # (for example a cancelled or killed caller). Reset this
                        # instance and keep listening instead of going deaf.
                        api().kernel.DisconnectNamedPipe(handle)
                        continue
                    if error != ERROR_PIPE_CONNECTED:
                        return
                if self.stopped.is_set():
                    return
                try:
                    verify_peer(handle)
                except Exception:
                    api().kernel.DisconnectNamedPipe(handle)
                    continue
                try:
                    next_handle = self.make_pipe()
                except OSError:
                    # Out of pipe instances/resources: drop this caller and retry.
                    api().kernel.DisconnectNamedPipe(handle)
                    time.sleep(0.1)
                    continue
                worker = threading.Thread(target=self.serve, args=(handle,), daemon=True, name="mcp-pipe-request")
                with self.workers_lock:
                    self.workers.add(worker)
                worker.start()
                handle = self.listener = next_handle
        finally:
            # Exactly one close of the instance this thread still owns.
            api().kernel.CloseHandle(handle)

    def serve(self, handle):
        try:
            request = read_line(handle, self.maximum, 10, self.stopped)
            result = asyncio.run_coroutine_threadsafe(self.handler(request), self.loop).result(timeout=120)
            write_line(handle, result)
            # DisconnectNamedPipe discards unread bytes. Wait for the client's
            # acknowledgement even during a graceful broker stop.
            read_line(handle, 16, 3)
        except Exception:
            pass
        finally:
            api().kernel.DisconnectNamedPipe(handle)
            api().kernel.CloseHandle(handle)
            with self.workers_lock:
                self.workers.discard(threading.current_thread())

    def close(self):
        self.stopped.set()
        try:
            handle = connect_pipe(self.address, timeout=0.5)
            api().kernel.CloseHandle(handle)
        except OSError:
            pass

    async def wait_closed(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with self.workers_lock:
                busy = bool(self.workers)
            if not busy and not self.thread.is_alive():
                return
            await asyncio.sleep(0.02)
