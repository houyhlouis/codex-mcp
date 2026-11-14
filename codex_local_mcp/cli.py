import argparse
import asyncio
import json
import os
import sys

from . import __version__
from .broker import ensure_broker, rpc, run_broker, stop_broker
from .config import load_config
from .mcp import MCP, review_schema, fetch_review_request
from .security import BridgeError, redact


async def review(config, request_id):
    if not sys.stdin.isatty():
        raise BridgeError("Human review requires an interactive terminal; use the explicitly configured relay for automation")
    request = await fetch_review_request(config, request_id, exchange=rpc)
    print(json.dumps(request, indent=2, ensure_ascii=False))
    if request["kind"] == "user_input":
        if any(q.get("isSecret") for q in request["params"]["questions"]):
            raise BridgeError("Secret answers are not accepted by the bridge; use native Codex authentication or the provider's browser flow")
        response = {"answers": {}}
        for question in request["params"]["questions"]:
            prompt = question["question"] + "\n> "
            value = input(prompt)
            response["answers"][question["id"]] = {"answers": [value]}
    else:
        schema, convert = review_schema(request)
        content = {}
        for key, prop in schema.get("properties", {}).items():
            if prop.get("type") == "string":
                value = input(f"{key} {prop.get('enum', '')} (blank leaves pending): ")
            else:
                value = input(f"{key} as JSON (blank leaves pending): ")
                if value:
                    value = json.loads(value)
            if value == "":
                return {"status": "pending", "request_id": request_id}
            content[key] = value
        from .tools import validate
        validate(content, schema)
        response = convert(content)
    args = {"request_id": request_id, "response": response, "review_source": "local_terminal"}
    if request["kind"] == "file_approval" and response.get("decision") == "accept":
        args["proposal_hash"] = request.get("proposal_hash")
    return await rpc(config, "review", args=args)


async def execute(args):
    if args.command == "worktree":
        from pathlib import Path
        from . import worktrees
        try:
            if args.action in ("plan", "create"):
                if not args.repo or not args.name:
                    raise worktrees.WorktreeError("name_invalid", "worktree plan/create requires --repo and --name.")
                target = Path(args.path).expanduser().absolute() if args.path else None
                if target is not None and target.name != args.name:
                    raise worktrees.WorktreeError("name_invalid", "The --path folder name must equal --name.")
                proposed = worktrees.plan(args.repo, args.name, args.base, root=target.parent if target else None)
                if args.plan_id and args.plan_id != proposed["plan_id"]:
                    raise worktrees.WorktreeError("unsafe_config", "The worktree plan changed; inspect a new plan before creating it.")
                locks = args.lock_dir or str(Path(proposed["common_dir"]) / worktrees.METADATA / "locks")
                result = proposed if args.action == "plan" else worktrees.create(proposed, lock_dir=locks)
            else:
                if not args.path:
                    raise worktrees.WorktreeError("not_managed", "worktree inspect/handoff/cleanup requires --path.")
                if args.action == "inspect":
                    result = worktrees.inspect(args.path)
                elif args.action == "handoff":
                    result = worktrees.handoff(args.path, max_bytes=args.max_bytes)
                else:
                    _, receipt = worktrees._load(args.path)
                    locks = args.lock_dir or str(Path(receipt["common_dir"]) / worktrees.METADATA / "locks")
                    result = worktrees.cleanup(args.path, lock_dir=locks, keep_branch=args.keep_branch)
            print(json.dumps(worktrees.json_result(result), indent=2, ensure_ascii=True) if args.json else worktrees.render(result))
            return 0
        except worktrees.WorktreeError as exc:
            message = json.dumps({"error": exc.as_dict()}, indent=2, ensure_ascii=True) if args.json else str(exc)
            if not args.json and exc.details:
                message += "\n" + worktrees.render(exc.details)
            print(message, file=sys.stderr)
            return 1
    if args.command in ("prune", "backup"):
        from contextlib import closing
        from .retention import Retention, backup, duration, offline, render
        from .store import Store
        config = load_config(args.config, legacy_stop=True)
        if args.command == "backup":
            result = backup(config, args.destination)
        else:
            if args.apply:
                if not args.plan_id or args.older_than is not None or args.max_bytes is not None:
                    raise BridgeError("prune --apply requires --plan-id and does not accept policy changes", code="invalid_arguments")
            elif args.plan_id or args.compact or args.older_than is None:
                raise BridgeError("Dry-run prune requires --older-than; --plan-id and --compact require --apply", code="invalid_arguments")
            with offline(config), closing(Store(config.state_dir)) as store:
                retention = Retention(store)
                result = retention.commit(args.plan_id, compact=args.compact) if args.apply else retention.plan(duration(args.older_than), args.max_bytes)
        print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else render(result))
        return
    if args.command == "incident-export":
        from .incidents import readonly_store, export_incident, preview, write_export
        config = load_config(args.config, legacy_stop=True)
        with readonly_store(config) as store:
            fixture = export_incident(store, args.task_id, after=args.after, limit=args.limit)
        report = write_export(fixture, args.output, args.confirm_preview) if args.output else preview(fixture)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(f"Structural incident: {report['tasks']} tasks, {report['events']} events, {report['requests']} requests; {report['bytes']} bytes")
            print("Content omitted; no upload. " + ("Sanitized fixture written." if report.get("written") else "Preview only; no file was written."))
            print("preview_id: " + report["preview_id"])
            if not report.get("written"):
                print(json.dumps(report["fixture"], indent=2, ensure_ascii=False))
        return 0
    if args.command == "doctor":
        from .diagnostics import diagnose, render
        report = await diagnose(args.config, start_broker=args.start_broker, schema=args.schema)
        print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render(report))
        return report["exit_code"]
    config = load_config(args.config, legacy_stop=args.command == "stop")
    if args.command == "serve":
        await MCP(config).run()
        return
    if args.command == "broker":
        await run_broker(config)
        return
    if args.command == "stop":
        result = await stop_broker(config)
    else:
        await ensure_broker(config)
        if args.command == "start":
            result = await rpc(config, "ping")
        elif args.command == "status":
            result = await rpc(config, "tool", name="codex_status")
        elif args.command == "review":
            result = await review(config, args.request_id)
        else:
            values = json.loads(sys.stdin.read() if args.args == "-" else args.args)
            result = await rpc(config, "tool", name=args.tool, args=values)
    print(json.dumps(redact(result), indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Local persistent Codex subagent MCP server")
    parser.add_argument("--version", action="version", version=f"codex-local-mcp {__version__}")
    parser.add_argument("--config", help="Optional operator-owned TOML settings; work directories are supplied by the main agent")
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {
        "serve": "Run the MCP stdio server (this is what MCP clients launch)",
        "broker": "Run the background broker in the foreground (normally started automatically)",
        "start": "Start the background broker if it is not running and print its PID",
        "stop": "Stop the background broker and wait until it has exited",
        "status": "Show the Codex version, sign-in type and pending requests",
    }
    for name, text in commands.items():
        sub.add_parser(name, help=text, description=text)
    cmd = sub.add_parser("call", help="Call an allowlisted tool for diagnosis")
    cmd.add_argument("tool")
    cmd.add_argument("--args", default="{}", help="JSON object; '-' reads stdin (use for sensitive input)")
    cmd = sub.add_parser("review", help="Review one request in an interactive terminal")
    cmd.add_argument("request_id")
    cmd = sub.add_parser("doctor", help="Check local installation and IPC without inference or automatic startup")
    cmd.add_argument("--json", action="store_true", help="Print the versioned diagnostic JSON report")
    cmd.add_argument("--start-broker", action="store_true", help="Allow broker/Codex startup for sign-in and capability status; no model calls")
    cmd.add_argument("--schema", action="store_true", help="Generate native schemas and run available offline protocol checks")
    cmd = sub.add_parser("prune", help="Plan offline bridge-state retention; nothing is removed without --apply")
    cmd.add_argument("--older-than", help="Eligible age: 30d, 12h, 60m, 30s, 2w; bare numbers mean days")
    cmd.add_argument("--max-bytes", type=int, help="Retained managed payload target; protected and younger data can exceed it")
    cmd.add_argument("--apply", action="store_true", help="Commit an unchanged, unexpired plan")
    cmd.add_argument("--plan-id", help="Plan returned by a dry run; expires after one hour")
    cmd.add_argument("--compact", action="store_true", help="Run VACUUM after applying; never implicit")
    cmd.add_argument("--json", action="store_true", help="Print the maintenance JSON report")
    cmd = sub.add_parser("backup", help="Make and verify a private SQLite backup, including while a broker runs")
    cmd.add_argument("--destination", help="New absolute file in a private directory; never a project directory")
    cmd.add_argument("--json", action="store_true", help="Print the maintenance JSON report")
    cmd = sub.add_parser("incident-export", help="Preview selected incident structure; content is always omitted; never uploads")
    cmd.add_argument("--task-id", action="append", required=True, help="Select a task; repeat up to eight times")
    cmd.add_argument("--after", type=int, default=0, help="Journal cursor before the selected events")
    cmd.add_argument("--limit", type=int, default=200, help="Maximum events (1..1000; default 200)")
    cmd.add_argument("--json", action="store_true", help="Print the preview/report as JSON")
    cmd.add_argument("--output", help="Write to a new file only after confirming its preview_id")
    cmd.add_argument("--confirm-preview", help="Exact preview_id from the preceding preview")
    cmd = sub.add_parser("worktree", help="Prepare and inspect operator-managed Git worktrees; never merges or pushes")
    cmd.add_argument("action", choices=("plan", "create", "inspect", "handoff", "cleanup"))
    cmd.add_argument("--repo", help="Existing non-bare repository for plan/create")
    cmd.add_argument("--name", help="Worktree name; the branch is codex/NAME")
    cmd.add_argument("--base", default="HEAD", help="Base commit or revision for plan/create (default: HEAD)")
    cmd.add_argument("--path", help="Worktree path; for plan/create its folder name must equal --name")
    cmd.add_argument("--plan-id", help="Require this exact plan_id for plan/create")
    cmd.add_argument("--lock-dir", help="Private lock folder; defaults to helper metadata in the common Git directory")
    cmd.add_argument("--max-bytes", type=int, default=1024 * 1024, help="Handoff patch byte limit (default: 1048576)")
    cmd.add_argument("--keep-branch", action="store_true", help="Cleanup only: keep the branch, including its unmerged commits")
    cmd.add_argument("--json", action="store_true", help="Print JSON; handoff patch bytes use base64")
    args = parser.parse_args()
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    os.umask(0o077)
    try:
        code = asyncio.run(execute(args))
        if code:
            raise SystemExit(code)
    except (BridgeError, OSError, ValueError) as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
