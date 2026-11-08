"""Per-project filesystem boundaries and private cross-platform state."""
import os
import re
import stat
from pathlib import Path, PureWindowsPath

from .platforms import IS_WINDOWS, canonical_system_path


class BridgeError(Exception):
    pass


def within(path, root):
    return path == root or root in path.parents


def same_path(left, right):
    try:
        return os.path.samefile(left, right)
    except OSError:
        return Path(left).resolve() == Path(right).resolve()


def validate_windows_path(value):
    """Lexical restrictions, also testable on a non-Windows development host."""
    path = PureWindowsPath(value)
    if not path.is_absolute() or path.drive.startswith("\\\\") or value.startswith(("\\\\?\\", "\\\\.\\")):
        raise BridgeError("Use an absolute local drive path, not UNC, device or drive-relative paths")
    for part in path.parts[1:]:
        if (part in (".", "..") or ":" in part or part.endswith((".", " "))
                or re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", part, re.I)):
            raise BridgeError("Invalid Windows project path component")


def checked_directory(value, create=False):
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise BridgeError("A work directory must be an absolute path without '..'")
    path = canonical_system_path(path)
    if IS_WINDOWS:
        validate_windows_path(str(path))
        from .windows import directory_chain
        try:
            with directory_chain(path, create=create):
                return path.resolve(strict=True)
        except OSError as exc:
            raise BridgeError("Directory must be accessible without symlinks or junctions") from exc
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except OSError as exc:
        raise BridgeError("Directory is inaccessible, missing, or contains a symlink") from exc
    finally:
        os.close(fd)
    return path.resolve(strict=True)


def project_path(config, value, create=False):
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise BridgeError("The main agent must supply an absolute workdir")
    resolved = path.resolve()
    if within(Path.home().resolve(), resolved):
        raise BridgeError("Select a dedicated project directory, not the home directory or a filesystem root")
    private_dirs = [config.state_dir, Path.home() / ".ssh", Path.home() / ".aws"]
    if any(within(resolved, p.resolve()) or within(p.resolve(), resolved) for p in private_dirs):
        raise BridgeError("Project directory overlaps private state or credential storage")
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()
    protected_files = [codex_home / "auth.json"] + ([config.path] if config.path else [])
    if any(within(p.resolve(), resolved) for p in protected_files):
        raise BridgeError("Project directory must not contain the operator configuration or Codex credentials")
    return checked_directory(path, create=create)


def private_dir(path):
    path = Path(path)
    if within(Path.home().resolve(), path.resolve()):
        raise BridgeError("Private state must use its own directory")
    checked_directory(path, create=True)
    if IS_WINDOWS:
        from .windows import protect_directory
        try:
            protect_directory(path)
        except OSError as exc:
            raise BridgeError("Cannot secure this Windows state directory for the current user") from exc
    else:
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise BridgeError("State directory must be owned by this user with mode 0700")


def private_file(path, flags):
    if IS_WINDOWS:
        from .windows import open_fd
        try:
            return open_fd(Path(path), flags, private=True)
        except OSError as exc:
            raise BridgeError(f"Expected a private user-owned file: {Path(path).name}") from exc
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
        os.close(fd)
        raise BridgeError(f"Expected a private, user-owned regular file: {Path(path).name}")
    return fd


def read_config_file(path):
    if IS_WINDOWS:
        from .windows import read_config
        try:
            return read_config(path)
        except OSError as exc:
            raise BridgeError("Configuration must be user-owned and not writable by other Windows accounts") from exc
    checked_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise BridgeError("Configuration must be owned by this user and not writable by group/others")
        return stream.read()


def read_project_file(project, relative, maximum):
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.drive:
        raise BridgeError("Input files must be relative paths inside this project")
    if IS_WINDOWS:
        from .windows import open_fd
        target = Path(project) / rel
        validate_windows_path(str(target))
        try:
            data_fd = open_fd(target, os.O_RDONLY)
        except OSError as exc:
            raise BridgeError("Input must be a regular file without symlinks or junctions") from exc
    else:
        fd = os.open(canonical_system_path(Path(project)), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in rel.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            data_fd = os.open(rel.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except OSError as exc:
            raise BridgeError("Input file must exist without symlink components") from exc
        finally:
            os.close(fd)
    with os.fdopen(data_fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum or info.st_nlink != 1:
            raise BridgeError("Input must be a regular, singly linked file within the size limit")
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise BridgeError("Input file grew beyond the size limit")
        return data


_SECRET_KEY = re.compile(r"^(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|password|encrypted_content)$", re.I)
_SECRETS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"(?i)\b(?:api_key|access_token|refresh_token|password|client_secret)\s*[:=]\s*[^\s,;]+"),
]


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if _SECRET_KEY.match(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for pattern in _SECRETS:
            value = pattern.sub("[REDACTED]", value)
    return value
