import unittest

from nico_application import ToolCatalog, ToolGrantError, ToolGrantUseCases


class FakeStore:
    def __init__(self):
        self.grants = []

    def tool_grants(self, session_id):
        return list(self.grants)

    def set_tool_grants(self, session_id, names):
        self.grants = sorted(set(names))
        return list(self.grants)


class ToolingTests(unittest.TestCase):
    def test_snapshot_freezes_prompt_and_execution_descriptors(self):
        builtin = {"type": "function", "function": {
            "name": "read_record", "description": "read",
            "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
        }}
        external = {"name": "mcp_search", "description": "search", "read_only": True,
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                    "server": "local"}
        writer = {"name": "plugin_save", "description": "save", "read_only": False,
                  "parameters": {"type": "object", "properties": {}}, "source": "plugin"}

        class Adapter:
            async def call(self, tool, arguments):
                return {"text": "ok"}

            def discover(self):
                return []

            async def close(self):
                pass

        adapter = Adapter()
        snapshot = ToolCatalog().snapshot(
            [builtin], [external, writer], {"mcp_search", "plugin_save"},
            {"read_record"}, {"mcp_search": adapter, "plugin_save": adapter},
        )
        builtin["function"]["parameters"]["properties"]["id"]["type"] = "integer"
        external["parameters"]["properties"]["query"]["type"] = "integer"
        model_tools = snapshot.definitions()
        self.assertEqual([item["function"]["name"] for item in model_tools],
                         ["read_record", "mcp_search"])
        self.assertEqual(model_tools[0]["function"]["parameters"]["properties"]["id"]["type"], "string")
        model_tools[1]["function"]["parameters"]["properties"]["query"]["type"] = "boolean"
        self.assertEqual(snapshot.definitions()[1]["function"]["parameters"]["properties"]["query"]["type"],
                         "string")
        descriptor = snapshot.external_tool("mcp_search")
        descriptor["parameters"]["properties"]["query"]["type"] = "number"
        self.assertEqual(snapshot.external_tool("mcp_search")["parameters"]["properties"]["query"]["type"],
                         "string")
        self.assertEqual(snapshot.external_adapter("mcp_search"), adapter)
        self.assertFalse(snapshot.is_read_only("plugin_save"))
        self.assertEqual(snapshot.external_tool("plugin_save")["read_only"], False)

    def test_tool_grants_share_strict_read_only_policy(self):
        store = FakeStore()
        grants = ToolGrantUseCases(store)
        available = [
            {"name": "read", "read_only": True},
            {"name": "write", "read_only": False},
            {"name": "ambiguous", "read_only": 1},
        ]
        self.assertEqual(grants.replace("session", ["read"], available), ["read"])
        with self.assertRaises(ToolGrantError) as error:
            grants.replace("session", ["write"], available)
        self.assertEqual(error.exception.kind, "write_disabled")
        with self.assertRaises(ToolGrantError) as error:
            grants.grant("session", "ambiguous", available)
        self.assertEqual(error.exception.kind, "write_disabled")
        with self.assertRaises(ToolGrantError) as error:
            grants.replace("session", ["missing"], available)
        self.assertEqual(error.exception.kind, "unavailable")
        self.assertEqual(grants.revoke("session", "read"), [])


if __name__ == "__main__":
    unittest.main()
