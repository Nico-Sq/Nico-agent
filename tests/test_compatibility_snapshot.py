import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from nico_contracts.channels import ChannelResult, TextMessage
from nico_contracts.errors import ChannelAccessError, ChannelDeliveryError, ModelError
from nico_contracts.events import RunStreamEvent, RunUpdateEvent, Usage
from nico_contracts.models import ModelAdapter

from career_agent.channels import (
    ChannelAccessError as LegacyChannelAccessError,
    ChannelDeliveryError as LegacyChannelDeliveryError,
    ChannelResult as LegacyChannelResult,
    TextMessage as LegacyTextMessage,
)
from career_agent.cli import build_parser, discover_mcp, run_chat
from nico_cli import build_parser as PackageCLIBuildParser
from career_agent.events import (
    RunStreamEvent as LegacyRunStreamEvent,
    RunUpdateEvent as LegacyRunUpdateEvent,
    Usage as LegacyUsage,
)
from career_agent.provider import ModelError as LegacyProviderModelError
from career_agent.providers import ModelError as LegacyProvidersModelError
from career_agent.providers.base import Adapter as LegacyAdapter
from career_agent.providers.errors import ModelError as LegacyErrorsModelError
from career_agent.auth import AuthStore as LegacyAuthStore
from career_agent.auto_backup import backup_all as legacy_backup_all
from career_agent.restore import restore as legacy_restore
from career_agent.store import Store as LegacyStore
import career_agent.tools as LegacyTools
import career_agent.mcp as LegacyMCP
import career_agent.plugins as LegacyPlugins
from nico_storage import AuthStore, Store
from nico_storage.backup import backup_all
from nico_storage.restore import restore as storage_restore
import nico_application.tools as ApplicationTools
import nico_adapters.mcp as AdapterMCP
import nico_adapters.plugins as AdapterPlugins


ROOT = Path(__file__).resolve().parent.parent


EXPECTED_API_ROUTES = {
    "DELETE /api/claims/{claim_id}",
    "DELETE /api/materials/{material_id}",
    "DELETE /api/memories/{memory_id}",
    "DELETE /api/memory-candidates/{candidate_id}",
    "DELETE /api/{kind}/sessions/{session_id}",
    "GET /api/auth/me",
    "GET /api/claims",
    "GET /api/config",
    "GET /api/data/backup",
    "GET /api/interview/sessions/{session_id}/report",
    "GET /api/materials",
    "GET /api/mcp/servers",
    "GET /api/mcp/tools",
    "GET /api/memories",
    "GET /api/memory-candidates",
    "GET /api/memory-candidates/{candidate_id}",
    "GET /api/models",
    "GET /api/tools",
    "GET /api/{kind}/sessions",
    "GET /api/{kind}/sessions/{session_id}",
    "GET /api/{kind}/sessions/{session_id}/export.md",
    "GET /api/{kind}/sessions/{session_id}/tools",
    "POST /api/auth/login",
    "POST /api/auth/logout",
    "POST /api/auth/register",
    "POST /api/chat/sessions",
    "POST /api/chat/sessions/{session_id}/interrupt",
    "POST /api/chat/sessions/{session_id}/memory-candidates",
    "POST /api/chat/sessions/{session_id}/messages",
    "POST /api/chat/sessions/{session_id}/messages/claim",
    "POST /api/chat/sessions/{session_id}/messages/run",
    "POST /api/chat/sessions/{session_id}/retry",
    "POST /api/claims",
    "POST /api/claims/{claim_id}/confirm",
    "POST /api/interview/sessions",
    "POST /api/interview/sessions/{session_id}/answers",
    "POST /api/interview/sessions/{session_id}/answers/claim",
    "POST /api/interview/sessions/{session_id}/answers/run",
    "POST /api/interview/sessions/{session_id}/cancel",
    "POST /api/interview/sessions/{session_id}/pause",
    "POST /api/interview/sessions/{session_id}/practice",
    "POST /api/interview/sessions/{session_id}/resume",
    "POST /api/interview/sessions/{session_id}/skip",
    "POST /api/interview/sessions/{session_id}/start",
    "POST /api/materials",
    "POST /api/materials/extract",
    "POST /api/memories",
    "POST /api/memory-candidates/{candidate_id}/confirm",
    "PUT /api/chat/sessions/{session_id}/memory",
    "PUT /api/claims/{claim_id}",
    "PUT /api/materials/{material_id}",
    "PUT /api/memory-candidates/{candidate_id}",
    "PUT /api/{kind}/sessions/{session_id}/title",
    "PUT /api/{kind}/sessions/{session_id}/tools",
}


class CompatibilitySnapshotTests(unittest.TestCase):
    def test_legacy_storage_imports_forward_to_the_storage_package(self):
        self.assertIs(LegacyStore, Store)
        self.assertIs(LegacyAuthStore, AuthStore)
        self.assertIs(legacy_backup_all, backup_all)
        self.assertIs(legacy_restore, storage_restore)

    def test_legacy_imports_are_the_contract_objects(self):
        self.assertIs(LegacyUsage, Usage)
        self.assertIs(LegacyRunUpdateEvent, RunUpdateEvent)
        self.assertIs(LegacyRunStreamEvent, RunStreamEvent)
        self.assertIs(LegacyProviderModelError, ModelError)
        self.assertIs(LegacyProvidersModelError, ModelError)
        self.assertIs(LegacyErrorsModelError, ModelError)
        self.assertIs(LegacyTextMessage, TextMessage)
        self.assertIs(LegacyChannelResult, ChannelResult)
        self.assertIs(LegacyChannelAccessError, ChannelAccessError)
        self.assertIs(LegacyChannelDeliveryError, ChannelDeliveryError)
        self.assertIs(LegacyAdapter, ModelAdapter)

    def test_legacy_tool_modules_forward_to_the_new_packages(self):
        self.assertIs(LegacyTools, ApplicationTools)
        self.assertIs(LegacyMCP, AdapterMCP)
        self.assertIs(LegacyPlugins, AdapterPlugins)

    def test_cli_top_level_commands_are_stable(self):
        self.assertIs(build_parser, PackageCLIBuildParser)
        parser = build_parser()
        subparsers = next(
            action for action in parser._actions
            if getattr(action, "dest", None) == "command"
        )
        self.assertEqual(
            set(subparsers.choices),
            {"chat", "models", "memory", "candidate", "interview", "tools"},
        )
        parsed = parser.parse_args(["interview", "create", "后端开发", "--questions", "2"])
        self.assertEqual((parsed.command, parsed.action, parsed.questions),
                         ("interview", "create", 2))

    def test_legacy_cli_helpers_remain_importable(self):
        self.assertTrue(callable(run_chat))
        self.assertTrue(callable(discover_mcp))

    def test_api_data_paths_and_account_layout_are_stable(self):
        code = r"""
import json
import os
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="nico-compat-") as data_dir:
    os.environ["DATA_DIR"] = data_dir
    os.environ["AUTH_REQUIRED"] = "true"
    os.environ["MODEL_PROVIDER"] = "demo"
    from fastapi.testclient import TestClient
    from career_agent import main

    routes = set()
    for path, operations in main.app.openapi()["paths"].items():
        if not path.startswith("/api"):
            continue
        for method in operations:
            if method.lower() in {"get", "post", "put", "patch", "delete", "options", "head"}:
                routes.add(f"{method.upper()} {path}")

    client = TestClient(main.app)
    response = client.post("/api/auth/register", json={
        "username": "compat-user", "password": "correct-password",
    })
    assert response.status_code == 200, response.text
    assert client.get("/api/materials").status_code == 200
    client.close()

    account_paths = sorted(
        path.relative_to(data_dir).as_posix()
        for path in Path(data_dir, "users").glob("*/career_agent.sqlite3")
    )
    print(json.dumps({
        "data_dir_matches": Path(main.DATA_DIR) == Path(data_dir),
        "root_store": main.store.path.name,
        "auth_store": main.auth_store.path.name,
        "account_paths": account_paths,
        "routes": sorted(routes),
    }, ensure_ascii=False))
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
            str(ROOT), os.environ.get("PYTHONPATH", ""),
        )))
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        )
        snapshot = json.loads(result.stdout)
        self.assertTrue(snapshot["data_dir_matches"])
        self.assertEqual(snapshot["root_store"], "career_agent.sqlite3")
        self.assertEqual(snapshot["auth_store"], "auth.sqlite3")
        self.assertEqual(len(snapshot["account_paths"]), 1)
        self.assertRegex(snapshot["account_paths"][0],
                         r"^users/[0-9a-f]+/career_agent\.sqlite3$")
        self.assertEqual(set(snapshot["routes"]), EXPECTED_API_ROUTES)


if __name__ == "__main__":
    unittest.main()
