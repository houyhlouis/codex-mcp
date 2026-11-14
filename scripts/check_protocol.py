#!/usr/bin/env python3
"""Check sample and actual fake-backend wire shapes against installed Codex schemas.

Outbound and inbound fake frames, including native RPC results, are fully
validated. Consumed-field contracts and guarded extensions are checked as well.
Optional development dependency: jsonschema. No inference, credentials or
configuration changes. Full generated schemas stay in a temporary directory;
Regenerate ALL pinned fixtures after a wire, tool-schema or adapter change:
  python3 scripts/check_protocol.py --codex /absolute/path/to/codex --update-fixtures
This refreshes native frames/schema, the shared MCP catalog, every revision,
the complete result/error registry snapshot and structural incident examples.
Only a tested Codex build may regenerate fixtures; each changed file is reported.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import jsonschema

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from codex_local_mcp.contract import (CODEX_TESTED_VERSIONS, MCP_VERSIONS, NATIVE_METHODS, NATIVE_NOTIFICATIONS,
    NATIVE_SERVER_REQUESTS, SERVER_RESPONSE_SCHEMAS, NATIVE_RESPONSE_SCHEMAS, INBOUND_FIELDS, INBOUND_ITEM_FIELDS, INBOUND_OPTIONAL_EXTENSIONS,
    codex_compatibility, parse_codex_version)
from tests.native_wire import capture_native_wire
from tests.mcp_wire import CATALOG_FIXTURE, capture_mcp_wire, revision_fixture
from codex_local_mcp.tools import TOOLS
from codex_local_mcp.security import BridgeError


def representative_samples():
    return {
        "InitializeParams": {"clientInfo": {"name": "codex_local_mcp", "title": "Codex Local MCP", "version": "0.1.1"}, "capabilities": {"experimentalApi": True}},
        "ThreadStartParams": {"cwd": "/tmp/project", "permissions": "local_mcp_write_test", "runtimeWorkspaceRoots": ["/tmp/project"],
            "approvalPolicy": "on-request", "model": "catalog-model", "ephemeral": False,
            "dynamicTools": [{"type": "function", "name": "ask_parent", "description": "Ask parent", "inputSchema": {"type": "object"}}]},
        "ThreadResumeParams": {"threadId": "thread", "cwd": "/tmp/project", "permissions": "local_mcp_write_test",
            "runtimeWorkspaceRoots": ["/tmp/project"], "approvalPolicy": "on-request", "excludeTurns": True},
        "TurnStartParams": {"threadId": "thread", "input": [{"type": "text", "text": "task", "text_elements": []}], "permissions": "local_mcp_write_test", "effort": "medium"},
        "TurnSteerParams": {"threadId": "thread", "expectedTurnId": "turn", "input": [{"type": "text", "text": "steer", "text_elements": []}]},
        "TurnInterruptParams": {"threadId": "thread", "turnId": "turn"},
        "ModelListParams": {"limit": 100, "cursor": None},
        "GetAccountParams": {"refreshToken": False},
        "ConfigReadParams": {"cwd": "/tmp/project", "includeLayers": False},
        "ThreadTurnsListParams": {"threadId": "thread", "limit": 50, "sortDirection": "asc", "itemsView": "summary", "cursor": None},
        "ThreadItemsListParams": {"threadId": "thread", "turnId": "turn", "limit": 50, "sortDirection": "asc", "cursor": None},
        "CommandExecutionRequestApprovalResponse": {"decision": "cancel"},
        "FileChangeRequestApprovalResponse": {"decision": "accept"},
        "PermissionsRequestApprovalResponse": {"permissions": {"network": {"enabled": True}}, "scope": "turn"},
        "ToolRequestUserInputResponse": {"answers": {"question": {"answers": ["answer"]}}},
        "DynamicToolCallResponse": {"success": True, "contentItems": [{"type": "inputText", "text": "answer"}]},
        "McpServerElicitationRequestResponse": {"action": "decline", "content": None},
        "ThreadSettingsUpdateParams": {"threadId": "thread", "approvalsReviewer": "auto_review", "collaborationMode": {"mode": "plan", "settings": {"model": "catalog-model", "reasoning_effort": "medium", "developer_instructions": None}}},
        "TurnSettingsUpdateParams": {"threadId": "thread", "turnId": "turn", "approvalsReviewer": "auto_review", "effort": "high"},
        "ThreadForkParams": {"threadId": "thread", "permissions": "local_mcp_write_test", "runtimeWorkspaceRoots": ["/tmp/project"], "excludeTurns": True, "deferGoalContinuation": True, "ephemeral": False},
        "ThreadSetNameParams": {"threadId": "thread", "name": "Renamed"},
        "ThreadArchiveParams": {"threadId": "thread"},
        "ThreadUnarchiveParams": {"threadId": "thread"},
        "ThreadDeleteParams": {"threadId": "thread"},
        "ThreadCompactStartParams": {"threadId": "thread"},
        "ThreadRevertParams": {"threadId": "thread", "beforeTurnId": "turn"},
        "ThreadQueueAddParams": {"threadId": "thread", "clientUserMessageId": "client", "input": [{"type": "text", "text": "queued"}]},
        "ThreadQueueListParams": {"threadId": "thread", "cursor": None, "limit": 50},
        "ThreadQueueUpdateParams": {"threadId": "thread", "queuedSubmissionId": "queued", "input": [{"type": "text", "text": "updated"}]},
        "ThreadQueueDeleteParams": {"threadId": "thread", "queuedSubmissionId": "queued"},
        "ThreadQueueReorderParams": {"threadId": "thread", "queuedSubmissionIds": ["one", "two"]},
        "ThreadQueueStartParams": {"threadId": "thread", "queuedSubmissionId": "queued"},
        "ReviewStartParams": {"threadId": "thread", "delivery": "inline", "target": {"type": "custom", "instructions": "Review"}},
        "ThreadBackgroundTerminalsListParams": {"threadId": "thread", "cursor": None, "limit": 50},
        "ThreadBackgroundTerminalsTerminateParams": {"threadId": "thread", "processId": "process"},
        "ThreadBackgroundTerminalsCleanParams": {"threadId": "thread"},
        "ThreadGoalSetParams": {"threadId": "thread", "objective": "Explicitly requested goal", "status": "paused", "origin": "user", "tokenBudget": 2000},
        "ThreadGoalGetParams": {"threadId": "thread"},
        "ThreadGoalClearParams": {"threadId": "thread", "origin": "user"},
        "SkillsListParams": {"cwds": ["/tmp/project"], "forceReload": False},
        "ThreadListParams": {"ancestorThreadId": "thread", "sourceKinds": ["subAgent", "subAgentThreadSpawn"], "limit": 50, "cursor": None},
        "ThreadReadParams": {"threadId": "thread", "includeTurns": False},
    }


class SchemaIndex:
    def __init__(self, directory):
        self.files = {}
        for path in Path(directory).rglob("*.json"):
            self.files.setdefault(path.stem, []).append(path)
        self.documents = {}

    def document(self, name):
        if name not in self.documents:
            files = self.files.get(name, [])
            if len(files) != 1:
                raise LookupError(f"Expected exactly one {name} schema, got {len(files)}")
            self.documents[name] = json.loads(files[0].read_text(encoding="utf-8"))
        return self.documents[name]

    def wire_schema(self, key):
        direction, kind, method = key.split(":", 2)
        if kind == "error":
            return self.document("JSONRPCError")
        if kind == "response":
            response = self.document((NATIVE_RESPONSE_SCHEMAS if direction == "inbound" else SERVER_RESPONSE_SCHEMAS)[method])
            return {"type": "object", "required": ["id", "result"], "properties": {
                "id": self.document("RequestId"), "result": {k: v for k, v in response.items() if k != "definitions"}},
                "definitions": response.get("definitions", {})}
        name = {("outbound", "request"): "ClientRequest", ("outbound", "notification"): "ClientNotification",
                ("inbound", "request"): "ServerRequest", ("inbound", "notification"): "ServerNotification"}[direction, kind]
        document = self.document(name)
        for branch in document["oneOf"]:
            if method in branch.get("properties", {}).get("method", {}).get("enum", []):
                common = {k: v for k, v in document.items() if k not in ("oneOf", "definitions")}
                return {"allOf": [common, branch], "definitions": document.get("definitions", {})}
        raise LookupError(f"No {name} schema for {method}")


def wire_shapes(fixture):
    pending = {}
    for frame in fixture["messages"]:
        direction, message = frame["direction"], frame["message"]
        if "method" in message:
            kind = "request" if "id" in message else "notification"
            method = message["method"]
            if direction == "inbound" and kind == "request":
                pending[message["id"]] = method
        elif direction == "outbound" and "error" in message:
            kind, method = "error", pending.pop(message["id"], "unknown")
        elif direction == "outbound" and "result" in message:
            kind, method = "response", pending.pop(message["id"])
        elif direction == "inbound" and "reply_to" in frame:
            kind, method = "error" if "error" in message else "response", frame["reply_to"]
        else:
            raise ValueError(f"Unexpected recorded frame: {direction}")
        yield f"{direction}:{kind}:{method}", message


def json_type(value):
    return {type(None): "null", bool: "boolean", int: "integer", float: "number",
            str: "string", list: "array", dict: "object"}[type(value)]


def schema_nodes(root, node, seen=()):
    if isinstance(node, bool):
        if node:
            yield {}
        return
    reference = node.get("$ref")
    if reference:
        if not reference.startswith("#/definitions/"):
            raise ValueError(f"Unsupported schema reference: {reference}")
        if reference not in seen:
            yield from schema_nodes(root, root["definitions"][reference.split("/")[-1]], (*seen, reference))
        return
    yield node
    for kind in ("allOf", "oneOf", "anyOf"):
        for child in node.get(kind, []):
            yield from schema_nodes(root, child, seen)


def field_schema_types(root, path):
    nodes = [root]
    for part in path:
        found = []
        for parent in nodes:
            for node in schema_nodes(root, parent):
                child = node.get("items") if part == "*" else node.get("properties", {}).get(part)
                if isinstance(child, (dict, bool)):
                    found.append(child)
        nodes = found
    types = set()
    for parent in nodes:
        for node in schema_nodes(root, parent):
            kind = node.get("type")
            if kind:
                types.update(kind if isinstance(kind, list) else [kind])
            elif not node or set(node) <= {"title", "description"}:
                types.update(("null", "boolean", "integer", "number", "string", "array", "object"))
    return types


def field_values(value, path):
    if not path:
        return [value]
    part, *rest = path
    if part == "*" and isinstance(value, list):
        return [found for item in value for found in field_values(item, rest)]
    if isinstance(value, dict) and part in value:
        return field_values(value[part], rest)
    return [MISSING]


MISSING = object()


def check_inbound_contract(key, message, schema):
    method = message["method"]
    if method not in INBOUND_FIELDS:
        raise LookupError(f"No consumed-field contract for {method}")
    fields = {"method": "string", "params": "object"}
    if "id" in message:
        fields["id"] = "integer|string"
    for path, kind in INBOUND_FIELDS[method].items():
        fields[("?params." + path[1:]) if path.startswith("?") else ("params." + path)] = kind
    item = message.get("params", {}).get("item", {})
    for path, kind in INBOUND_ITEM_FIELDS.get(item.get("type"), {}).items():
        fields[("?params.item." + path[1:]) if path.startswith("?") else ("params.item." + path)] = kind
    for raw_path, expected in fields.items():
        optional = raw_path.startswith("?")
        path = raw_path.lstrip("?").split(".")
        native_types = field_schema_types(schema, path)
        accepted = set(expected.split("|"))
        if not native_types.intersection(accepted):
            raise ValueError(f"Consumed field {'.'.join(path)} is absent or has an incompatible native schema type")
        for value in field_values(message, path):
            if value is MISSING:
                if not optional:
                    raise ValueError(f"Fake frame lacks consumed field {'.'.join(path)}")
            elif json_type(value) not in accepted or json_type(value) not in native_types:
                raise ValueError(f"Fake field {'.'.join(path)} has an incompatible type: {json_type(value)}")
    extensions = []
    for path, kinds in INBOUND_OPTIONAL_EXTENSIONS.get(method, {}).items():
        full_path = ("params." + path).split(".")
        native_types = field_schema_types(schema, full_path)
        for value in field_values(message, full_path):
            if value is not MISSING:
                if json_type(value) not in kinds.split("|") or (native_types and json_type(value) not in native_types):
                    raise ValueError(f"Guarded optional field {path} has an incompatible type")
                if not native_types:
                    extensions.append(f"params.{path} ({json_type(value)}; guarded optional extension, not declared by native schema)")
    return extensions


def validate_wire(fixture, schema_for, label, output=print):
    checked = {"outbound": set(), "inbound": set()}
    counts = {"outbound": 0, "inbound": 0}
    unchecked, failures, omissions, extensions = [], [], set(), set()
    for key, message in wire_shapes(fixture):
        direction = key.split(":", 1)[0]
        try:
            schema = schema_for(key)
        except (LookupError, KeyError) as exc:
            reason = f"{key}: {exc}"
            unchecked.append(reason)
            if key != "inbound:request:future/unknownApproval":
                failures.append(reason)
            output(f"UNCHECKED {label} {reason}" + (" (intentional fail-closed probe)" if key.endswith(":future/unknownApproval") else ""))
            continue
        try:
            jsonschema.Draft7Validator(schema).validate(message)
            if direction == "inbound" and "method" in message:
                extensions.update(f"{key} {field}" for field in check_inbound_contract(key, message, schema))
        except (ValueError, LookupError, jsonschema.ValidationError) as exc:
            failure = f"{key}: {str(exc).splitlines()[0]}"
            failures.append(failure)
            output(f"FAILED {label} {failure}")
        else:
            counts[direction] += 1
            checked[direction].add(key)
    for direction in ("outbound", "inbound"):
        mode = "fully validated"
        output(f"{label} {direction}: {counts[direction]} messages, {len(checked[direction])} shapes {mode}")
    for omission in sorted(omissions):
        output(f"FAKE OMITS {label} {omission}")
    for extension in sorted(extensions):
        output(f"FAKE EXTENSION {label} {extension}")
    output(f"{label} unchecked: {len(unchecked)}; failures: {len(failures)}")
    return {"counts": counts, "shapes": checked, "unchecked": unchecked, "failures": failures,
            "fake_schema_omissions": sorted(omissions), "fake_extensions": sorted(extensions)}


def schema_subset(index, keys, version):
    definitions, schemas = {}, {}

    def strip(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key in ("description", "title", "$schema"):
                    continue
                if key in ("properties", "definitions", "patternProperties"):
                    result[key] = {name: strip(child) for name, child in item.items()}
                elif key in ("const", "enum", "default", "examples"):
                    result[key] = item
                else:
                    result[key] = strip(item)
            return result
        return [strip(item) for item in value] if isinstance(value, list) else value

    def collect(schema, local):
        if isinstance(schema, dict):
            reference = schema.get("$ref", "")
            if isinstance(reference, str) and reference.startswith("#/definitions/"):
                name = reference[len("#/definitions/"):]
                value = strip(local[name])
                if name in definitions:
                    if definitions[name] != value:
                        raise ValueError(f"Conflicting generated definition: {name}")
                else:
                    definitions[name] = value
                    collect(value, local)
            for key, value in schema.items():
                if key not in ("const", "enum", "default", "examples"):
                    collect(value, local)
        elif isinstance(schema, list):
            for value in schema:
                collect(value, local)

    for key in sorted(keys):
        schema = strip(copy.deepcopy(index.wire_schema(key)))
        local = schema.pop("definitions", {})
        collect(schema, local)
        schemas[key] = schema
    return {"format": 1, "codex_version": version,
            "generated_by": "codex app-server generate-json-schema --experimental",
            "scope": "Recorded wire shapes and transitive definitions; schema descriptions and titles removed",
            "schemas": schemas, "definitions": definitions}


def pinned_schema(document, key):
    return {"allOf": [document["schemas"][key]], "definitions": document["definitions"]}


def report_unrecorded(fixture, output=print):
    keys = {key for key, _ in wire_shapes(fixture)}
    for kind, inventory, directions in (("request", NATIVE_METHODS, ("outbound",)),
                                        ("request", NATIVE_SERVER_REQUESTS, ("inbound",)),
                                        ("notification", NATIVE_NOTIFICATIONS, ("outbound", "inbound"))):
        for method, requirement in inventory.items():
            if not any(f"{direction}:{kind}:{method}" in keys for direction in directions):
                output(f"NOT RECORDED {requirement} {kind} {method}: no representative offline flow emitted this shape")


def refresh_fixtures(live, subset, mcp, directory=None, output=print):
    directory = directory or REPO / "tests" / "fixtures"
    documents = {"native-wire.json": live, "native-schema.json": subset,
                 CATALOG_FIXTURE: {"format": 1, "source": "MCP tools/list result; shared by every supported revision", "tools": TOOLS}}
    documents.update({f"mcp-{version}.json": revision_fixture(transcript) for version, transcript in mcp.items()})
    documents["result-contract.json"] = {"result_version": 1, "errors": {code: {
        "when": row[0], "retryable": row[1], "uncertain": row[2], "next_steps": list(row[3])} for code, row in BridgeError.CODES.items()}}
    from tests.incident_samples import sample_incidents
    documents.update(sample_incidents())
    directory.mkdir(parents=True, exist_ok=True)
    for name, document in documents.items():
        path = directory / name
        compact = name == "native-schema.json"
        data = (json.dumps(document, sort_keys=True, **({"separators": (",", ":")} if compact else {"indent": 2})) + "\n").encode("utf-8")
        old = path.read_bytes() if path.exists() else b""
        if old == data:
            output(f"UNCHANGED {name} ({len(data)} bytes)")
        else:
            path.write_bytes(data)
            before = hashlib.sha256(old).hexdigest()[:12] if old else "absent"
            output(f"UPDATED {name}: {len(old)} -> {len(data)} bytes; sha256 {before} -> {hashlib.sha256(data).hexdigest()[:12]}")


async def capture_mcp_revisions():
    return {version: await capture_mcp_wire(version) for version in MCP_VERSIONS}


def main(binary, update_fixtures=False):
    version = subprocess.run([binary, "--version"], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    print(f"Codex version: {version}")
    compatibility = codex_compatibility(version)
    print(f"Compatibility: {compatibility['status']}; {compatibility['reason']}")
    if compatibility["status"] == "unsupported":
        return 1
    if update_fixtures and parse_codex_version(version).version not in CODEX_TESTED_VERSIONS:
        raise ValueError("Only a tested Codex version may update pinned fixtures")
    fixture_path = REPO / "tests" / "fixtures" / "native-wire.json"
    with tempfile.TemporaryDirectory(prefix="codex-schema-") as directory:
        subprocess.run([binary, "app-server", "generate-json-schema", "--experimental", "--out", directory], check=True, timeout=60)
        index = SchemaIndex(directory)
        raw_count, raw_shapes, raw_failures = 0, set(), []
        def validate_raw(key, message):
            nonlocal raw_count
            if key == "inbound:request:future/unknownApproval":
                return
            try:
                jsonschema.Draft7Validator(index.wire_schema(key)).validate(message)
            except (LookupError, KeyError, jsonschema.ValidationError) as exc:
                raw_failures.append(key + ": " + str(exc).splitlines()[0])
            else:
                raw_count += 1
                raw_shapes.add(key)
        live = asyncio.run(capture_native_wire(validate_raw))
        print(f"Raw inbound: {raw_count} messages, {len(raw_shapes)} shapes fully validated; intermediate observation replies are not pinned")
        for failure in raw_failures:
            print("FAILED raw " + failure)
        samples = representative_samples()
        failures = list(raw_failures)
        for name, sample in samples.items():
            try:
                schema = index.document(name)
                unknown = set(sample) - set(schema.get("properties", {}))
                if unknown:
                    raise ValueError(f"Unknown fields: {sorted(unknown)}")
                jsonschema.Draft7Validator(schema).validate(sample)
                print(name, "OK")
            except (LookupError, ValueError, jsonschema.ValidationError) as exc:
                failures.append(name)
                print(name, "FAILED:", str(exc).splitlines()[0])
        print(f"Validated {len(samples) - len(failures)}/{len(samples)} representative protocol shapes")
        result = validate_wire(live, index.wire_schema, "live")
        report_unrecorded(live)
        failures.extend(result["failures"])
        if update_fixtures:
            if failures:
                return 1
            subset = schema_subset(index, result["shapes"]["outbound"] | result["shapes"]["inbound"], version)
            refresh_fixtures(live, subset, asyncio.run(capture_mcp_revisions()))
        else:
            stored = json.loads(fixture_path.read_text(encoding="utf-8"))
            failures.extend(validate_wire(stored, index.wire_schema, "recorded")["failures"])
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--update-fixtures", action="store_true", help="Refresh ALL native/MCP fixtures and the shared catalog; requires a tested Codex version")
    args = parser.parse_args()
    raise SystemExit(main(args.codex, args.update_fixtures))
