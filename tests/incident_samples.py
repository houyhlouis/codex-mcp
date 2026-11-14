"""Public, content-free sample incidents exported through the real exporter."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp.incidents import export_incident
from codex_local_mcp.store import Store
from tests.helpers import temp_root


def sample_incidents():
    result = {}
    for pending in (False, True):
        with tempfile.TemporaryDirectory(prefix="sample-", dir=temp_root()) as directory:
            state = Path(directory)
            state.chmod(0o700)
            store = Store(state)
            try:
                task = {"id": "sample-task", "thread_id": "sample-thread", "turn_id": "sample-turn", "project": "/fixture/project",
                    "status": "waiting" if pending else "completed", "created_at": 0, "updated_at": 0,
                    "summary": "Private sample output", "evidence_complete": True, "evidence_gaps": []}
                common = {"threadId": "sample-thread", "turnId": "sample-turn"}
                with patch("codex_local_mcp.store.time.time", return_value=1700000000.0), store.transaction():
                    store.save("tasks", task)
                    store.event(task["id"], "bridge/task_created", {"project": task["project"]})
                    store.event(task["id"], "bridge/input", {"text": "Private sample prompt"})
                    store.event(task["id"], "turn/started", {**common, "turn": {"id": "sample-turn", "status": "inProgress", "items": []}})
                    if pending:
                        request = {"id": "sample-request", "task_id": task["id"], "method": "item/commandExecution/requestApproval", "kind": "command_approval",
                            "status": "pending", "generation": 1, "rpc_id": 7, "requires_human": True, "blocking": True,
                            "params": {**common, "itemId": "sample-command", "command": "Private sample command", "cwd": task["project"], "startedAtMs": 0}}
                        store.save("requests", request)
                        store.event(task["id"], "bridge/request", request)
                    else:
                        store.event(task["id"], "turn/completed", {**common, "turn": {"id": "sample-turn", "status": "completed", "items": []}})
                fixture = export_incident(store, [task["id"]])
                fixture["replay"]["injections"] = [{"action": "crash" if pending else "drop_ack", "at": len(fixture["events"])}]
                result["incident-pending-approval.json" if pending else "incident-lost-ack.json"] = fixture
            finally:
                store.close()
    return result
