"""Native Win32 local pipes and access checks, loaded only on Windows.

Uses documented Win32 APIs. Pipes reject remote connections and verify both
process identities. JSON bytes are used throughout; no pickle deserialization.
"""
import asyncio
import ctypes
import functools
import json
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
TRUSTED_SIDS = {"S-1-5-18", "S-1-5-32-544", "S-1-3-0", "S-1-3-4"}
SYNCHRONIZE, PROCESS_QUERY_LIMITED_INFORMATION = 0x00100000, 0x1000
WAIT_TIMEOUT = 0x102
ERROR_ACCESS_DENIED, ERROR_NO_DATA, ERROR_PIPE_CONNECTED = 5, 232, 535
ERROR_IO_PENDING, ERROR_IO_INCOMPLETE, ERROR_OPERATION_ABORTED, ERROR_NOT_FOUND = 997, 996, 995, 1168
FILE_FLAG_OVERLAPPED = 0x40000000
PIPE_WORKERS, PIPE_CLIENTS, PIPE_IO_LIMIT = 32, 32, 128
WRITE_TIMEOUT, ACK_TIMEOUT, CANCEL_GRACE, SHUTDOWN_GRACE = 5.0, 3.0, 1.0, 1.0
POLL_INTERVAL = 0.02


class AdmissionExhausted(ConnectionError):
    pass


class Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]


class AdmissionBudget:
    """Nonblocking admission; tokens are released exactly once by their owner."""
    def __init__(self, limit, message):
        if type(limit) is not int or not 1 <= limit <= 252:
            raise ValueError("Named-pipe budget must be between 1 and 252")
        self.limit, self.message, self.active = limit, message, 0
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            if self.active >= self.limit:
                raise AdmissionExhausted(self.message)
            self.active += 1
        released = False
        def release():
            nonlocal released
            with self.lock:
                if released:
                    raise RuntimeError("Named-pipe admission released twice")
                released = True
                self.active -= 1
        return release


CLIENT_BUDGET = AdmissionBudget(PIPE_CLIENTS, "Local named-pipe client budget exhausted; retry after active clients finish")
IO_BUDGET = AdmissionBudget(PIPE_IO_LIMIT, "Local named-pipe I/O budget exhausted; retry after pending cancellations finish")


class OwnedPipe:
    def __init__(self, raw, close=None):
        self.raw, self._close = raw, close or api().kernel.CloseHandle
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            self._close(self.raw)


def wait_io(operation, timeout, cancelled=None, *, phase="operation", clock=time.monotonic):
    """Plain Python deadline/cancellation state machine; operations own buffers."""
    deadline = clock() + timeout if timeout is not None else None
    try:
        while True:
            reason = (ConnectionAbortedError("Local named-pipe operation cancelled") if cancelled and cancelled.is_set()
                      else TimeoutError(f"Local named-pipe {phase} deadline exceeded") if deadline is not None and clock() >= deadline
                      else None)
            if reason:
                operation.cancel()
                drain_deadline = clock() + CANCEL_GRACE
                while clock() < drain_deadline:
                    try:
                        if operation.poll() is not None:
                            break
                    except OSError:
                        break
                    operation.wait(min(POLL_INTERVAL, max(0, drain_deadline - clock())))
                if not operation.done:
                    operation.abandon()
                raise reason
            result = operation.poll()
            if result is not None:
                return result
            operation.wait(POLL_INTERVAL if deadline is None else min(POLL_INTERVAL, max(0, deadline - clock())))
    finally:
        if operation.abandoned:
            pass  # The completion reaper owns the event, buffer and I/O token.
        elif operation.done:
            operation.close()
        else:
            try:
                operation.cancel()
            finally:
                operation.abandon()


_PENDING, _PENDING_LOCK = set(), threading.Lock()


def _retain_cancelled(operation):
    # Close the pipe to release its instance, but never free an in-flight
    # OVERLAPPED/buffer. The bounded I/O token stays held until completion.
    operation.pipe.close()
    with _PENDING_LOCK:
        _PENDING.add(operation)

    def reap():
        while api().kernel.WaitForSingleObject(operation.overlapped.hEvent, 100) != 0:
            time.sleep(POLL_INTERVAL)
        operation.done = True
        operation.close()
        with _PENDING_LOCK:
            _PENDING.discard(operation)

    threading.Thread(target=reap, daemon=True, name="mcp-pipe-cancel").start()


class NativeOperation:
    def __init__(self, pipe, kind, data=None, size=65536):
        self.pipe, self.done, self.abandoned = pipe, False, False
        self.release = IO_BUDGET.acquire()
        self.overlapped, self.buffer, self.result = Overlapped(), None, None
        try:
            self.overlapped.hEvent = api().handle(api().kernel.CreateEventW(None, True, False, None))
            self.kind = kind
            if kind == "connect":
                ok = api().kernel.ConnectNamedPipe(pipe.raw, ctypes.byref(self.overlapped))
            else:
                self.buffer = ctypes.create_string_buffer(data, len(data)) if kind == "write" else ctypes.create_string_buffer(size)
                call = api().kernel.WriteFile if kind == "write" else api().kernel.ReadFile
                ok = call(pipe.raw, self.buffer, len(self.buffer), None, ctypes.byref(self.overlapped))
            error = 0 if ok else ctypes.get_last_error()
            if kind == "connect" and (ok or error == ERROR_PIPE_CONNECTED):
                self.done, self.result = True, 0
            elif ok:
                self.poll()
            elif error != ERROR_IO_PENDING:
                raise ctypes.WinError(error)
        except BaseException:
            self.done = True
            self.close()
            raise

    def _result(self, count):
        return self.buffer.raw[:count] if self.kind == "read" else count

    def poll(self):
        if self.done:
            return self.result
        count = w.DWORD()
        if api().kernel.GetOverlappedResult(self.pipe.raw, ctypes.byref(self.overlapped), ctypes.byref(count), False):
            self.done, self.result = True, self._result(count.value)
            return self.result
        error = ctypes.get_last_error()
        if error == ERROR_IO_INCOMPLETE:
            return None
        self.done = True
        raise ctypes.WinError(error)

    def wait(self, seconds):
        result = api().kernel.WaitForSingleObject(self.overlapped.hEvent, max(1, int(seconds * 1000)))
        if result not in (0, WAIT_TIMEOUT):
            raise ctypes.WinError(ctypes.get_last_error())

    def cancel(self):
        if not self.done and not api().kernel.CancelIoEx(self.pipe.raw, ctypes.byref(self.overlapped)):
            if ctypes.get_last_error() != ERROR_NOT_FOUND:
                raise ctypes.WinError(ctypes.get_last_error())

    def abandon(self):
        if not self.abandoned:
            self.abandoned = True
            _retain_cancelled(self)

    def close(self):
        if self.overlapped.hEvent:
            api().kernel.CloseHandle(self.overlapped.hEvent)
            self.overlapped.hEvent = None
        if self.release:
            self.release()
            self.release = None


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", w.BOOL)]


class ACL(ctypes.Structure):
    _fields_ = [("revision", w.BYTE), ("sbz1", w.BYTE), ("size", w.WORD), ("count", w.WORD), ("sbz2", w.WORD)]


class FileInfo(ctypes.Structure):
    _fields_ = [("attributes", w.DWORD), ("created", w.FILETIME), ("accessed", w.FILETIME),
                ("written", w.FILETIME), ("volume", w.DWORD), ("size_high", w.DWORD),
                ("size_low", w.DWORD), ("links", w.DWORD), ("index_high", w.DWORD), ("index_low", w.DWORD)]


class FileBasicInfo(ctypes.Structure):
    _fields_ = [("created", ctypes.c_int64), ("accessed", ctypes.c_int64),
               ("written", ctypes.c_int64), ("changed", ctypes.c_int64), ("attributes", w.DWORD)]


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
        self.bind(self.kernel, "CreateEventW", [ctypes.POINTER(SecurityAttributes), w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE)
        self.bind(self.kernel, "CancelIoEx", [w.HANDLE, ctypes.POINTER(Overlapped)], w.BOOL)
        self.bind(self.kernel, "GetOverlappedResult", [w.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(w.DWORD), w.BOOL], w.BOOL)
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
        self.bind(self.kernel, "GetFileInformationByHandleEx", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL)
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
            # OWNER RIGHTS (Python's Windows mkdir(0700)) applies only to the
            # owner already verified above, never to another account.
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


def file_change_time(path):
    """True NT change time; Python 3.11 st_ctime is the creation time."""
    handle = open_handle(path, share=7, access=0x80)
    try:
        info = FileBasicInfo()
        api().check(api().kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)))
        return info.changed
    finally:
        api().kernel.CloseHandle(handle)


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


def io_remaining(deadline, cancelled, phase):
    if cancelled and cancelled.is_set():
        raise ConnectionAbortedError("Local named-pipe operation cancelled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"Local named-pipe {phase} deadline exceeded")
    return remaining


def read_line(handle, maximum, timeout, cancelled=None):
    deadline, data = time.monotonic() + timeout, bytearray()
    while True:
        remaining = io_remaining(deadline, cancelled, "read")
        part = wait_io(NativeOperation(handle, "read", size=min(65536, maximum + 1 - len(data))),
                       remaining, cancelled, phase="read")
        if not part:
            raise BrokenPipeError("Local named-pipe peer disconnected")
        data.extend(part)
        if len(data) > maximum:
            raise ValueError("Named-pipe frame exceeds size limit")
        if b"\n" in data:
            line, extra = bytes(data).split(b"\n", 1)
            if extra.strip():
                raise ValueError("Only one request per pipe connection is accepted")
            return line


def write_line(handle, payload, timeout=WRITE_TIMEOUT, cancelled=None, *, phase="write"):
    data, sent, deadline = payload + b"\n", 0, time.monotonic() + timeout
    while sent < len(data):
        remaining = io_remaining(deadline, cancelled, phase)
        part = data[sent:sent + 65536]
        count = wait_io(NativeOperation(handle, "write", data=part), remaining,
                        cancelled, phase=phase)
        if not count:
            raise BrokenPipeError("Pipe write made no progress")
        sent += count


def connect_pipe(address, timeout=2, cancelled=None):
    a, deadline = api(), time.monotonic() + timeout
    while True:
        if cancelled and cancelled.is_set():
            raise ConnectionAbortedError("Local named-pipe operation cancelled")
        handle = a.kernel.CreateFileW(address, GENERIC_READ | GENERIC_WRITE, 0, None, 3, FILE_FLAG_OVERLAPPED, None)
        if handle not in (None, 0, INVALID_HANDLE):
            try:
                verify_peer(handle, server=True)
                return handle
            except BaseException:
                a.kernel.CloseHandle(handle)
                raise
        error = ctypes.get_last_error()
        if error in (2, 3):
            raise FileNotFoundError("Local broker pipe is not running")
        if error != 231:
            raise ctypes.WinError(error)
        if time.monotonic() >= deadline:
            raise ConnectionError("Local named-pipe admission budget exhausted or broker busy; retry after active clients finish")
        a.kernel.WaitNamedPipeW(address, 20)


async def exchange(address, payload, maximum, timeout):
    release = CLIENT_BUDGET.acquire()
    loop, cancelled, finished = asyncio.get_running_loop(), threading.Event(), threading.Event()
    future = loop.create_future()

    def finish(result=None, error=None):
        if not future.done():
            future.set_exception(error) if error else future.set_result(result)

    def work():
        handle = None
        deadline = time.monotonic() + timeout
        try:
            handle = OwnedPipe(connect_pipe(address, min(2, timeout), cancelled))
            write_line(handle, payload, min(WRITE_TIMEOUT, max(0, deadline - time.monotonic())), cancelled)
            result = read_line(handle, maximum, max(0, deadline - time.monotonic()), cancelled)
            write_line(handle, b"ACK", min(ACK_TIMEOUT, max(0, deadline - time.monotonic())), cancelled, phase="ACK")
            if not loop.is_closed():
                loop.call_soon_threadsafe(finish, result)
        except BaseException as exc:
            if not loop.is_closed():
                loop.call_soon_threadsafe(finish, None, exc)
        finally:
            if handle is not None:
                handle.close()
            release()
            finished.set()

    try:
        threading.Thread(target=work, daemon=True, name="mcp-pipe-client").start()
    except BaseException:
        release()
        raise
    try:
        return await asyncio.wait_for(future, timeout + CANCEL_GRACE + 1)
    finally:
        cancelled.set()
        deadline = time.monotonic() + CANCEL_GRACE + 1
        while not finished.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
        if not finished.is_set():
            raise TimeoutError("Local named-pipe client cancellation did not complete")


class PipeServer:
    def __init__(self, address, handler, maximum, *, worker_limit=PIPE_WORKERS, write_timeout=WRITE_TIMEOUT):
        self.address, self.handler, self.maximum = address, handler, maximum
        self.write_timeout = write_timeout
        self.budget = AdmissionBudget(worker_limit, "Local named-pipe worker budget exhausted; retry after active clients finish")
        self.reject_budget = AdmissionBudget(1, self.budget.message)
        self.loop = asyncio.get_running_loop()
        self.stopped, self.cancelled = threading.Event(), threading.Event()
        self.workers, self.workers_lock = set(), threading.Lock()
        api().process_sid(require_unrestricted=True)
        self.listener = self.make_pipe(first=True)
        self.thread = threading.Thread(target=self.accept, daemon=True, name="mcp-pipe-listener")

    def make_pipe(self, first=False):
        with api().descriptor() as attributes:
            # Security remains local, owner-restricted and first-instance fenced.
            raw = api().handle(api().kernel.CreateNamedPipeW(self.address,
                3 | FILE_FLAG_OVERLAPPED | (0x80000 if first else 0), 0x8,
                self.budget.limit + 2, 65536, 65536, 1000, ctypes.byref(attributes)))
            return OwnedPipe(raw)

    def start(self):
        try:
            self.thread.start()
        except BaseException:
            self.listener.close()
            raise

    def accept(self):
        handle = self.listener
        try:
            while not self.stopped.is_set():
                try:
                    wait_io(NativeOperation(handle, "connect"), None, self.stopped, phase="connect")
                except AdmissionExhausted:
                    # A cancellation reaper may temporarily own every I/O slot.
                    # Keep the listener alive until a slot is released or stopped.
                    self.stopped.wait(POLL_INTERVAL)
                    continue
                except OSError as exc:
                    if self.stopped.is_set():
                        return
                    if getattr(exc, "winerror", None) in (ERROR_NO_DATA, 109, ERROR_OPERATION_ABORTED):
                        api().kernel.DisconnectNamedPipe(handle.raw)
                        continue
                    if isinstance(exc, PermissionError):
                        api().kernel.DisconnectNamedPipe(handle.raw)
                        continue
                    return
                if self.stopped.is_set():
                    return
                try:
                    verify_peer(handle.raw)
                except OSError:
                    # A client may exit between accept and identity lookup.
                    api().kernel.DisconnectNamedPipe(handle.raw)
                    continue
                rejected = False
                try:
                    release = self.budget.acquire()
                except ConnectionError:
                    rejected = True
                    try:
                        release = self.reject_budget.acquire()
                    except ConnectionError:
                        api().kernel.DisconnectNamedPipe(handle.raw)
                        continue
                try:
                    next_handle = self.make_pipe()
                except OSError:
                    release()
                    api().kernel.DisconnectNamedPipe(handle.raw)
                    continue
                worker = threading.Thread(target=self.serve, args=(handle, release, rejected), daemon=True, name="mcp-pipe-request")
                with self.workers_lock:
                    self.workers.add(worker)
                try:
                    worker.start()
                except BaseException:
                    with self.workers_lock:
                        self.workers.discard(worker)
                    release()
                    next_handle.close()
                    raise
                handle = self.listener = next_handle
        finally:
            handle.close()

    def serve(self, handle, release, rejected=False):
        future = None
        try:
            request = read_line(handle, self.maximum, 2 if rejected else 10, self.cancelled)
            if rejected:
                result = json.dumps({"error": {"code": "pipe_budget_exhausted", "message": self.budget.message}}).encode()
            else:
                future = asyncio.run_coroutine_threadsafe(self.handler(request), self.loop)
                deadline = time.monotonic() + 120
                while True:
                    if self.cancelled.is_set():
                        raise ConnectionAbortedError("Local named-pipe operation cancelled")
                    try:
                        result = future.result(timeout=POLL_INTERVAL)
                        break
                    except TimeoutError:
                        if future.done():
                            raise
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Local named-pipe handler deadline exceeded")
            write_line(handle, result, self.write_timeout, self.cancelled)
            if read_line(handle, 16, ACK_TIMEOUT, self.cancelled) != b"ACK":
                raise ValueError("Local named-pipe acknowledgement is invalid")
        except Exception:
            pass
        finally:
            if future is not None and not future.done():
                future.cancel()
            if not handle.closed:
                api().kernel.DisconnectNamedPipe(handle.raw)
            handle.close()
            release()
            with self.workers_lock:
                self.workers.discard(threading.current_thread())

    def close(self):
        self.stopped.set()
        if self.thread.ident is None:
            self.listener.close()

    async def wait_closed(self):
        started = time.monotonic()
        while time.monotonic() - started < SHUTDOWN_GRACE + CANCEL_GRACE + 1:
            if time.monotonic() - started >= SHUTDOWN_GRACE:
                self.cancelled.set()
            with self.workers_lock:
                busy = bool(self.workers)
            if not busy and not self.thread.is_alive():
                return
            await asyncio.sleep(POLL_INTERVAL)
        self.cancelled.set()
        raise TimeoutError("Local named-pipe shutdown did not complete")
