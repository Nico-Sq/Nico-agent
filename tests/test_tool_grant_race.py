import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from nico_storage import Store
from nico_web import route_support, tool_routes


class ToolGrantRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_wait_cannot_grant_tool_after_run_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "career_agent.sqlite3")
            session = store.create("chat")
            store.set_tool_grants(session["id"], ["existing"])
            discovery_started = asyncio.Event()
            continue_discovery = asyncio.Event()

            class Plugins:
                def discover(self):
                    return []

            class Mcp:
                async def discover(self):
                    discovery_started.set()
                    await continue_discovery.wait()
                    return [{
                        "name": "mcp_read", "read_only": True,
                        "parameters": {"type": "object", "properties": {}},
                    }]

            backend = SimpleNamespace(
                local_plugins=Plugins,
                mcp_servers=Mcp,
            )
            with patch.object(tool_routes, "active_store", return_value=store), \
                    patch.object(route_support, "active_store", return_value=store), \
                    patch.object(tool_routes, "active_backend", return_value=backend):
                request = asyncio.create_task(tool_routes.set_session_tools(
                    "chat", session["id"], tool_routes.ToolGrantsInput(names=["mcp_read"]),
                ))
                await asyncio.wait_for(discovery_started.wait(), timeout=2)
                self.assertIsNotNone(store.claim_run(session["id"], "chat", "active", "开始运行"))
                continue_discovery.set()

                with self.assertRaises(HTTPException) as error:
                    await request

            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(store.tool_grants(session["id"]), ["existing"])


if __name__ == "__main__":
    unittest.main()
