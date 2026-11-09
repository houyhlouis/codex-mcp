import argparse
import asyncio
import json
import os
import sys

from . import __version__
from .broker import ensure_broker, rpc, run_broker, stop_broker
from .config import load_config
from .mcp import MCP, review_schema
from .security import BridgeError, redact


async def review(config, request_id):
    if not sys.stdin.isatty():
        raise BridgeError("Human review requires an interactive terminal; use the explicitly configured relay for automation")
    request = await rpc(config, "tool", name="codex_review_request", args={"request_id": request_id})
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
    return await rpc(config, "review", args={"request_id": request_id, "response": response})


async def execute(args):
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
    args = parser.parse_args()
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    os.umask(0o077)
    try:
        asyncio.run(execute(args))
    except (BridgeError, OSError, ValueError) as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
