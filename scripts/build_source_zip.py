#!/usr/bin/env python3
"""Build or verify a clean source ZIP without Git metadata or private runtime data."""

import argparse
import hashlib
import json
import re
import stat
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "codex-mcp/"
MANIFEST = "SOURCE-MANIFEST.json"
ROOT_FILES = {
    ".editorconfig", ".gitattributes", ".gitignore", "README.md", "README.zh-CN.md",
    "CONTRIBUTING.md", "SECURITY.md", "pyproject.toml",
    "config.example.toml", "server.py", "NOTICE",
}
TREES = {"codex_local_mcp", "docs", "examples", "scripts", "tests", ".github"}
PRIVATE_PARTS = {".git", ".codex", ".agents", ".aws", ".ssh", ".venv", "venv",
                 "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                 "node_modules", "dist", "build"}
PRIVATE_NAMES = {"auth.json", "credentials", "credentials.json", "config.local.toml", MANIFEST.lower()}
PRIVATE_SUFFIXES = (".pyc", ".pyo", ".log", ".sock", ".sqlite", ".sqlite3", ".db", ".zip", ".sha256")
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
WINDOWS_DEVICE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)


def public_source(relative):
    parts = relative.parts
    if any(p.lower() in PRIVATE_PARTS or p.lower().endswith(".egg-info") for p in parts):
        return False
    name = relative.name.lower()
    if (name in PRIVATE_NAMES or name == ".env" or name.startswith(".env.")
            or name.endswith(PRIVATE_SUFFIXES) or ".sqlite3-" in name or ".db-" in name):
        return False
    return (len(parts) == 1 and relative.name in ROOT_FILES) or (len(parts) > 1 and parts[0] in TREES)


def portable_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or "\\" in name or ":" in name
            or path.as_posix() != name or any(p in ("", ".", "..") for p in path.parts)):
        raise ValueError(f"Unsafe archive path: {name}")
    if any(p.endswith((".", " ")) or WINDOWS_DEVICE.match(p) for p in path.parts):
        raise ValueError(f"Not a portable Windows filename: {name}")


def collect_sources():
    candidates = [ROOT / name for name in ROOT_FILES if (ROOT / name).exists()]
    for tree in sorted(TREES):
        directory = ROOT / tree
        if directory.is_symlink():
            raise ValueError(f"Source directory must not be a symlink: {tree}")
        candidates.extend(directory.rglob("*"))
    sources, folded = {}, set()
    for path in sorted(candidates):
        relative = path.relative_to(ROOT)
        if not public_source(relative):
            continue
        if path.is_symlink():
            raise ValueError(f"Refusing a source symlink: {relative}")
        if not path.is_file():
            continue
        name = relative.as_posix()
        portable_name(name)
        if name.casefold() in folded:
            raise ValueError(f"Filename collision on Windows: {name}")
        folded.add(name.casefold())
        if path.stat().st_size > MAX_FILE:
            raise ValueError(f"Source file exceeds 16 MiB: {name}")
        sources[name] = path.read_bytes()
    if sum(map(len, sources.values())) > MAX_TOTAL:
        raise ValueError("Source package exceeds 128 MiB")
    required = {"README.md", "README.zh-CN.md", "pyproject.toml", "server.py", "codex_local_mcp/__init__.py", "tests/fake_codex.py"}
    if not required <= sources.keys():
        raise ValueError("Missing required distribution files: " + ", ".join(sorted(required - sources.keys())))
    return sources


def verify_archive(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [info.filename for info in entries]
        if len(names) > 2000 or len({name.casefold() for name in names}) != len(names):
            raise ValueError("Too many entries or duplicate/colliding names")
        if sum(info.file_size for info in entries) > MAX_TOTAL:
            raise ValueError("Archive exceeds the uncompressed size limit")
        for info in entries:
            portable_name(info.filename)
            if not info.filename.startswith(PREFIX) or info.is_dir():
                raise ValueError("Expected regular files under codex-mcp/")
            if stat.S_ISLNK(info.external_attr >> 16) or info.file_size > MAX_FILE:
                raise ValueError("Unexpected symlink or oversized file")
            relative = PurePosixPath(info.filename[len(PREFIX):])
            if relative.as_posix() != MANIFEST and not public_source(relative):
                raise ValueError(f"Excluded archive member: {relative}")
        metadata = json.loads(archive.read(PREFIX + MANIFEST))
        if metadata.get("schema_version") != 1 or not isinstance(metadata.get("files"), dict):
            raise ValueError("Unsupported source manifest")
        expected = {PREFIX + name for name in metadata["files"]} | {PREFIX + MANIFEST}
        if expected != set(names):
            raise ValueError("Manifest and ZIP membership differ")
        for name, record in metadata["files"].items():
            data = archive.read(PREFIX + name)
            if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ValueError(f"Source integrity check failed: {name}")
        project = tomllib.loads(archive.read(PREFIX + "pyproject.toml").decode("utf-8"))["project"]
        if project["version"] != metadata["version"]:
            raise ValueError("Manifest and project versions differ")
        return {"version": metadata["version"], "source_files": len(metadata["files"]),
                "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


def build_archive(output=None):
    sources = collect_sources()
    version = tomllib.loads(sources["pyproject.toml"].decode("utf-8"))["project"]["version"]
    if not re.fullmatch(r"[A-Za-z0-9.+_-]+", version):
        raise ValueError("Version is not safe for a source archive filename")
    output = Path(output).expanduser().resolve() if output else ROOT / "dist" / f"codex-mcp-{version}-source.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"schema_version": 1, "project": "codex-mcp", "version": version,
                "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in sorted(sources.items())}}
    members = {**sources, MANIFEST: (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")}
    with tempfile.NamedTemporaryFile(prefix="codex-mcp-", suffix=".zip.tmp", dir=output.parent, delete=False) as temporary:
        temp_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(members.items()):
                info = zipfile.ZipInfo(PREFIX + name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                executable = name == "server.py" or name == "tests/fake_codex.py" or (name.startswith("scripts/") and name.endswith(".py"))
                info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
                archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        result = verify_archive(temp_path)
        temp_path.replace(output)
        checksum = output.with_name(output.name + ".sha256")
        checksum.write_text(f"{result['sha256']}  {output.name}\n", encoding="ascii")
        return output, checksum, result
    finally:
        temp_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--output", type=Path, help="ZIP destination; defaults to dist/codex-mcp-VERSION-source.zip")
    group.add_argument("--verify", type=Path, help="Verify an existing ZIP and its embedded source manifest")
    args = parser.parse_args()
    if args.verify:
        result = verify_archive(args.verify)
        print(f"Verified {result['source_files']} source files (version {result['version']})")
        print(f"SHA256 {result['sha256']}")
    else:
        output, checksum, result = build_archive(args.output)
        print(f"ZIP: {output}")
        print(f"Checksum: {checksum}")
        print(f"Verified {result['source_files']} source files; {output.stat().st_size} bytes")


if __name__ == "__main__":
    main()
