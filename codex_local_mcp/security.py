"""Per-project filesystem boundaries and private cross-platform state."""
import os
import re
import stat
from pathlib import Path, PureWindowsPath

from .platforms import IS_WINDOWS, canonical_system_path


class BridgeError(Exception):
    # This table is the public error-code registry. The defaults describe an
    # identical call, not a new operation id or a changed human decision.
    CODES = {
        "bridge_error": ("Other rejected bridge operation", False, False, ("Inspect codex_status and the message",)),
        "broker_identity_mismatch": ("Running broker uses different configuration or bridge identity", False, False, ("Use matching identity inputs, or stop the old broker with its configuration before reconnecting",)),
        "broker_identity_unchecked": ("Running broker cannot provide comparable health identity", False, False, ("Use the same bridge release to obtain a read-only broker health report",)),
        "unsupported_task_operation": ("MCP task operation is not supported", False, False, ("Use tasks/get, tasks/update or tasks/cancel for a returned execution id",)),
        "mcp_capability_required": ("Required per-request MCP capability is absent", False, False, ("Declare the required capability on this request or use ordinary tools",)),
        "mcp_version_unsupported": ("Per-request MCP version is unsupported", False, False, ("Select a supported version from server/discover",)),
        "incident_preview_mismatch": ("Incident changed or preview confirmation is missing", False, False, ("Preview the incident again and confirm its exact preview_id",)),
        "internal_error": ("Unexpected internal failure; side effects cannot be ruled out", False, True, ("Inspect codex_status, codex_get and codex_events before repeating a mutation",)),
        "invalid_arguments": ("Invalid tool arguments or action fields", False, False, ("Correct the arguments before calling again",)),
        "invalid_response": ("Unsupported or unsafe approval/answer shape", False, False, ("Read codex_review_request and supply a supported response",)),
        "not_found": ("Unknown task, request or project id", False, False, ("Refresh codex_list, codex_requests or codex_projects",)),
        "state_conflict": ("Task state does not allow this control", False, False, ("Inspect codex_get and codex_history",)),
        "stale_turn": ("Expected turn is no longer the active turn", False, False, ("Refresh codex_get before controlling the current turn",)),
        "stale_request": ("Request is resolved, expired, disconnected or already answering", False, False, ("Refresh codex_requests",)),
        "stale_proposal": ("Reviewed file proposal hash is missing, stale or unknown", False, False, ("Fetch codex_review_request and review the current proposal_hash",)),
        "human_review_required": ("Positive decision needs human review or configured approval_relay", False, False, ("Use codex_review_request or the local review command",)),
        "user_request_required": ("Goal mutation lacks an explicit user request", False, False, ("Obtain an explicit user request before changing a native goal",)),
        "scope_unverified": ("Native identity, sandbox, cwd or project boundary cannot be verified", False, False, ("Use codex_resume and inspect codex_history before continuing",)),
        "workspace_busy": ("Another family owns workspace or capacity claims", False, False, ("Inspect codex_get workspace_claims",)),
        "workspace_read_unavailable": ("Optional native read API prevents proving workspace inactive", False, True, ("Install a tested Codex version and restart; use codex_resume to re-check",)),
        "operation_conflict": ("Operation key was used with different arguments", False, False, ("Inspect the original operation; choose a new operation_id for different work",)),
        "operation_uncertain": ("Persisted operation has no known final outcome", False, True, ("Inspect codex_get and codex_events before starting more work",)),
        "optional_native_capability_unavailable": ("Native optional method is unavailable", False, False, ("Use another supported control or install a tested Codex version and restart",)),
        "native_protocol_incompatible": ("Native protocol or version cannot be used safely", False, True, ("Inspect codex_status and codex_history before resuming",)),
        "native_rejected": ("Native RPC explicitly rejected the call", False, False, ("Inspect the message and current task before changing the call",)),
        "native_timeout": ("Native acknowledgement did not arrive", False, True, ("Inspect codex_get and codex_history; do not blindly retry",)),
        "transport_unavailable": ("Broker/native connection changed or disconnected", False, True, ("Reconnect and inspect codex_get; use codex_resume to reconcile",)),
        "result_too_large": ("Requested page exceeds response budget", False, False, ("Request a smaller page",)),
        "state_invalid": ("Stored state, revision or migration ledger is invalid", False, False, ("Keep the state directory and backup; inspect with a compatible release",)),
        "state_newer_schema": ("Database layout belongs to a newer release", False, False, ("Use a newer release or restore a backup in a separate directory",)),
        "state_migration_failed": ("Backup or transactional migration failed", False, False, ("Keep the database and backup; retry with a fixed release",)),
        "duplicate_thread_id": ("Native thread is assigned to multiple task records", False, False, ("Keep the database and backup; repair the conflicting identities",)),
        "operation_expired": ("Operation result was pruned; the key remains reserved", False, False, ("Inspect the task and use a new operation_id only for new work",)),
        "cursor_expired": ("Journal, request or outcome cursor expired or belongs to another scope or database", False, False, ("Follow resume_after or restart observation without its cursor",)),
        "maintenance_busy": ("Pruning requires an offline broker", False, False, ("Stop the broker with the same --config, then retry",)),
        "prune_plan_invalid": ("Prune plan is unknown, expired, committed or changed", False, False, ("Create and inspect a new dry-run plan",)),
        "artifact_not_found": ("Artifact is absent or belongs to another task", False, False, ("Read codex_get view=result for retained references",)),
        "artifact_version_mismatch": ("Artifact bytes no longer match their captured hash", False, False, ("Inspect current evidence; never use stale artifact content",)),
        "artifact_too_large": ("Artifact exceeds the capture limit", False, False, ("Use smaller evidence files",)),
        "result_expired": ("Turn evidence expired under an operator retention policy", False, False, ("Inspect native history; do not treat missing evidence as complete",)),
        "maintenance_failed": ("State maintenance could not finish", False, False, ("Keep the state directory; inspect the applied flag and create a new dry-run plan",)),
        "person_review_required": ("Redacted file acceptance requires a person, including with approval_relay", False, False, ("Use codex_review_request or the local terminal review and inspect the redaction warning",)),
        "prune_plan_limit": ("Unexpired prune plans exhaust the count or storage budget", False, False, ("Apply a reviewed plan, wait for expiry, or select less data",)),
        "backup_destination_exists": ("Backup destination already exists", False, False, ("Choose a new backup file path",)),
        "decision_expired": ("A typed decision or unsent delegation contract passed its expiry", False, False, ("Reply with plain text, or use a new operation_id for a new delegation",)),
        "decision_mismatch": ("Decision binding, offered option or scoped evidence does not match", False, False, ("Refresh the pending request or scoped result/artifact references",)),
        "budget_exceeded": ("Family resource budget refuses new execution", False, False, ("Use codex_settings on the root task to raise or clear budget",)),
        "worktrees_disabled": ("Managed worktree actions are disabled", False, False, ("Set worktrees = true in the settings file and restart the broker",)),
        "worktree_plan_mismatch": ("Worktree plan no longer matches its inputs", False, False, ("Call codex_worktrees action=plan again and review its plan_id",)),
        "worktree_interrupted": ("Worktree operation was interrupted and may have changed Git state", False, True, ("Inspect codex_worktrees action=inspect before any new mutation",)),
        "worktree_not_git": ("Repository is not a supported Git working tree", False, False, ("Choose an existing non-bare Git repository",)),
        "worktree_base_unknown": ("Base does not identify a known commit", False, False, ("Choose a locally available commit or reference",)),
        "worktree_name_invalid": ("Worktree name cannot form a safe branch or folder", False, False, ("Choose another worktree name",)),
        "worktree_target_exists": ("Target or receipt already exists", False, False, ("Inspect the existing path; do not repeat creation blindly",)),
        "worktree_branch_exists": ("Proposed codex branch already exists", False, False, ("Choose another worktree name",)),
        "worktree_unsafe_config": ("Git configuration, paths or helper metadata cannot be used safely", False, False, ("Inspect the worktree and helper receipt before retrying",)),
        "worktree_filters_configured": ("Git filters would execute configured programs", False, False, ("Prepare this worktree manually",)),
        "worktree_dirty": ("Worktree contains unsaved or ignored files", False, False, ("Preserve the changes before requesting cleanup",)),
        "worktree_unmerged_commits": ("Worktree has commits outside its base", False, False, ("Keep the branch with keep_branch=true or retain the worktree",)),
        "worktree_not_managed": ("Worktree ownership cannot be verified", False, False, ("Inspect the path and its private helper receipt",)),
        "worktree_locked": ("A worktree or repository lock prevents this action", False, False, ("Inspect the lock owner and retry with a new operation_id after it finishes",)),
        "worktree_git_failed": ("Git operation failed or exceeded its limits", False, True, ("Inspect the worktree before any new mutation",)),
        "worktree_git_missing": ("Git is unavailable on absolute PATH entries", False, False, ("Install Git and include its absolute directory in PATH",)),
        "worktree_git_too_old": ("Git is older than the supported minimum", False, False, ("Install Git 2.36.0 or newer",)),
    }

    def __init__(self, message, *, code=None, retryable=None, uncertain=None, next_steps=None, ids=None, details=None):
        self.code = code or getattr(type(self), "code", "bridge_error")
        if self.code not in self.CODES:
            raise ValueError("Unregistered bridge error code")
        _, repeat, unknown, steps = self.CODES[self.code]
        self.retryable = repeat if retryable is None else retryable
        self.uncertain = unknown if uncertain is None else uncertain
        self.next_steps = list(steps if next_steps is None else next_steps)
        self.ids = dict(ids or {})
        self.details = dict(details or {})
        super().__init__(message)

    def as_dict(self):
        return redact({**self.details, "code": self.code, "message": str(self)[:16384], "retryable": self.retryable,
                       "uncertain": self.uncertain, "next_steps": self.next_steps, "ids": self.ids})

    @classmethod
    def from_dict(cls, data):
        return cls(data["message"], code=data["code"], retryable=data["retryable"],
                   uncertain=data["uncertain"], next_steps=data["next_steps"], ids=data.get("ids"),
                   details={k: v for k, v in data.items() if k not in {"code", "message", "retryable", "uncertain", "next_steps", "ids"}})


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


def project_path(config, value, create=False, *, allow_missing=False):
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
    if allow_missing and not create:
        existing = path
        while not os.path.lexists(existing):
            existing = existing.parent
        checked_directory(existing)
        return resolved
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
_SECRET_PREFIXES = ("sk-", "bearer", "eyj", "api_key", "access_token", "refresh_token", "password", "client_secret")


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if _SECRET_KEY.match(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        # Case-insensitive ASCII regex letters include dotted/dotless i,
        # long s and Kelvin sign. This filter may overmatch; re still decides.
        folded = value.lower() if value.isascii() else value.casefold().replace("\u0307", "").replace("\u0131", "i")
        if not any(prefix in folded for prefix in _SECRET_PREFIXES):
            return value
        for pattern in _SECRETS:
            value = pattern.sub("[REDACTED]", value)
    return value
