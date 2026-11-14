#!/usr/bin/env python3
"""Build and inspect public wheel/sdist artifacts offline; never publishes.

python3 scripts/release_check.py --require-build --require-install --dist-dir dist
Build dependencies must already be installed; build and pip use no isolation or
network. Without build/pip, optional checks print SKIP unless explicitly required.
The separately maintained transfer ZIP is not a public release artifact.
"""
import argparse
import ast
import configparser
import email.parser
import importlib.util
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from codex_local_mcp import __version__
from codex_local_mcp.diagnostics import validate_report


def expected_modules(root=REPO):
    return {path.relative_to(root).as_posix() for path in (root / "codex_local_mcp").rglob("*.py")}


def release_version(root=REPO):
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    match = re.search(r"^##\s+\[?(\d+\.\d+\.\d+)", (root / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
    if project["name"] != "codex-local-mcp" or project["version"] != __version__ or not match or match[1] != __version__:
        raise ValueError("Project, package and newest changelog versions must agree")
    if project.get("dependencies"):
        raise ValueError("Runtime dependencies must remain empty")
    return __version__


def safe_member(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("Unsafe archive member: " + name)
    return path


def module_version(source):
    tree = ast.parse(source)
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__" for target in statement.targets):
            return ast.literal_eval(statement.value)
    raise ValueError("Artifact package version is missing")


def secret_member(member):
    parts = [part.lower() for part in member.parts]
    return (any(part.startswith((".", "secret", "credentials")) or part in {"state", "__pycache__"} for part in parts)
            or member.name.lower() in {"auth.json", "token", "tokens.json"}
            or member.suffix.lower() in {".pem", ".key", ".db", ".sqlite", ".sqlite3", ".bak", ".pyc"})


def inspect_wheel(path, version, modules=None):
    modules = expected_modules() if modules is None else modules
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Wheel contains duplicate paths")
        for name in names:
            member = safe_member(name)
            lower = [part.lower() for part in member.parts]
            if secret_member(member) or any(part in {"tests", "fixtures"} for part in lower):
                raise ValueError("Wheel contains excluded/secret-looking content: " + name)
        packaged = {name for name in names if name.startswith("codex_local_mcp/") and name.endswith(".py")}
        if modules != packaged:
            raise ValueError("Wheel package modules differ from source: " + ", ".join(sorted(modules ^ packaged)))
        if module_version(archive.read("codex_local_mcp/__init__.py")) != version:
            raise ValueError("Wheel package version is incorrect")
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        entries = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(metadata) != 1 or len(entries) != 1:
            raise ValueError("Wheel metadata/console entry point is missing or ambiguous")
        info = email.parser.BytesParser().parsebytes(archive.read(metadata[0]))
        if info["Name"] != "codex-local-mcp" or info["Version"] != version or info.get_all("Requires-Dist"):
            raise ValueError("Wheel name/version/runtime dependency metadata is incorrect")
        if not any(PurePosixPath(name).name == "LICENSE" for name in names):
            raise ValueError("Wheel LICENSE is missing")
        parser = configparser.ConfigParser()
        parser.read_string(archive.read(entries[0]).decode())
        if parser.get("console_scripts", "codex-local-mcp", fallback=None) != "codex_local_mcp.cli:main":
            raise ValueError("Wheel console entry point is incorrect")
    print(f"OK wheel {path.name}: version {version}, LICENSE, entry point, {len(modules)} modules, no excluded content")


def inspect_sdist(path, version, modules=None):
    modules = expected_modules() if modules is None else modules
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        roots = {safe_member(name).parts[0] for name in names}
        if len(roots) != 1 or len(names) != len(set(names)):
            raise ValueError("Sdist has ambiguous roots or duplicate paths")
        if any(member.issym() or member.islnk() or not (member.isfile() or member.isdir()) for member in members):
            raise ValueError("Sdist contains links or special files")
        if any(secret_member(safe_member(name)) for name in names):
            raise ValueError("Sdist contains secret-looking or state files")
        prefix = next(iter(roots)) + "/"
        contents = {name.removeprefix(prefix) for name in names}
        required = {"pyproject.toml", "README.md", "LICENSE", "PKG-INFO"} | modules
        if not required <= contents:
            raise ValueError("Sdist is missing build inputs: " + ", ".join(sorted(required - contents)))
        if module_version(archive.extractfile(prefix + "codex_local_mcp/__init__.py").read()) != version:
            raise ValueError("Sdist package version is incorrect")
        info = email.parser.BytesParser().parsebytes(archive.extractfile(prefix + "PKG-INFO").read())
        if info["Name"] != "codex-local-mcp" or info["Version"] != version:
            raise ValueError("Sdist metadata version/name is incorrect")
        project = tomllib.loads(archive.extractfile(prefix + "pyproject.toml").read().decode())["project"]
        if project["version"] != version or project.get("dependencies"):
            raise ValueError("Sdist project metadata is incorrect")
    print(f"OK sdist {path.name}: version {version}, LICENSE, metadata and complete build inputs")


def console_entry(target, *, windows=os.name == "nt"):
    name = "codex-local-mcp.exe" if windows else "codex-local-mcp"
    candidates = [target / folder / name for folder in ("bin", "Scripts")]
    found = [path for path in candidates if path.is_file()]
    if len(found) != 1:
        raise ValueError("Installed console script is missing or ambiguous")
    return found[0]


def install_check(artifact, directory, version):
    target, state = directory / "target", directory / "state-root"
    target.mkdir()
    state.mkdir(mode=0o700)
    env = dict(os.environ, PIP_NO_INDEX="1", PYTHONPATH=str(target), PYTHONDONTWRITEBYTECODE="1",
               XDG_STATE_HOME=str(state), LOCALAPPDATA=str(state), CODEX_HOME=str(directory / "codex-home"))
    env.pop("PYTHONHOME", None)
    env.pop("CODEX_PROFILE", None)
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-build-isolation", "--no-deps", "--no-compile",
                    "--target", str(target), str(artifact)], check=True, cwd=directory, env=env, timeout=120, stdout=subprocess.DEVNULL)
    entry = console_entry(target)
    output = subprocess.run([str(entry), "--version"], check=True, capture_output=True, text=True, encoding="utf-8", cwd=directory, env=env, timeout=15)
    if output.stdout.strip() != "codex-local-mcp " + version:
        raise ValueError("Installed console version is incorrect")
    doctor = subprocess.run([str(entry), "doctor", "--json"], capture_output=True, text=True, encoding="utf-8", cwd=directory, env=env, timeout=40)
    report = json.loads(doctor.stdout)
    validate_report(report)
    if (doctor.returncode not in (0, 1, 2) or report.get("format") != 1 or report.get("bridge_version") != version
            or report.get("exit_code") != doctor.returncode or not isinstance(report.get("findings"), list)):
        raise ValueError("Installed doctor returned an invalid report")
    print(f"OK install {artifact.name}: console --version and doctor --json (diagnostic exit {doctor.returncode})")


def check(root=REPO, *, dist_dir=None, require_build=False, require_install=False):
    version = release_version(root)
    if importlib.util.find_spec("build") is None:
        print("SKIP build: optional development module 'build' is not installed")
        return 1 if require_build else 0
    with tempfile.TemporaryDirectory(prefix="codex-release-") as temporary:
        directory = Path(temporary).resolve()
        output = Path(dist_dir).resolve() if dist_dir else directory / "dist"
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            raise ValueError("Release output directory must be empty; choose a new directory")
        env = dict(os.environ, PIP_NO_INDEX="1")
        subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--outdir", str(output), str(root)],
                       check=True, env=env, timeout=180)
        wheels, sdists = list(output.glob("*.whl")), list(output.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1 or len(list(output.iterdir())) != 2:
            raise ValueError("Expected exactly one wheel and one sdist")
        inspect_wheel(wheels[0], version, expected_modules(root))
        inspect_sdist(sdists[0], version, expected_modules(root))
        if importlib.util.find_spec("pip") is None:
            print("SKIP install: optional development module 'pip' is not installed")
            return 1 if require_install else 0
        for index, artifact in enumerate([*wheels, *sdists]):
            install_dir = directory / f"install-{index}"
            install_dir.mkdir()
            install_check(artifact, install_dir, version)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, help="Keep verified wheel/sdist in a new empty directory")
    parser.add_argument("--require-build", action="store_true", help="Fail when build is unavailable (CI)")
    parser.add_argument("--require-install", action="store_true", help="Fail when pip is unavailable (CI)")
    args = parser.parse_args()
    try:
        return check(dist_dir=args.dist_dir, require_build=args.require_build, require_install=args.require_install)
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print("FAIL release check: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
