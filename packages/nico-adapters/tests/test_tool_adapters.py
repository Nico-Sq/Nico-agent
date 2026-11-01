import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nico_adapters.plugins import LocalPlugins
from nico_adapters.registry import load_tool_adapters


class ToolAdapterTests(unittest.TestCase):
    def test_only_requested_entry_points_are_loaded_and_can_be_removed(self):
        loaded = []

        class MCP:
            pass

        class Extra:
            pass

        class EntryPoint:
            def __init__(self, name, distribution, target):
                self.name = name
                self.dist = SimpleNamespace(name=distribution)
                self.target = target

            def load(self):
                loaded.append(self.name)
                return self.target

        points = [
            EntryPoint("stdio_mcp", "nico-adapters", MCP),
            EntryPoint("extra", "third-party-tools", Extra),
        ]
        with patch("nico_adapters.registry.entry_points", return_value=points):
            selected = load_tool_adapters(("stdio_mcp", "extra"))
            self.assertEqual(set(selected), {"stdio_mcp", "extra"})
            self.assertEqual(loaded, ["stdio_mcp", "extra"])
            loaded.clear()
            remaining = load_tool_adapters(("stdio_mcp",))
        self.assertEqual(set(remaining), {"stdio_mcp"})
        self.assertEqual(loaded, ["stdio_mcp"])

    def test_plugin_lifecycle_keeps_an_immutable_catalog_and_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "reader.py"
            plugin.write_text(
                "def read(arguments): return arguments['text']\n"
                "TOOLS = [{'name':'read','description':'read','read_only':True,"
                "'parameters':{'type':'object','properties':{'text':{'type':'string'}},"
                "'required':['text'],'additionalProperties':False},'run':read}]\n",
                encoding="utf-8",
            )
            config = root / "plugins.json"
            config.write_text('[{"path":"reader.py"}]', encoding="utf-8")
            plugins = LocalPlugins(config)
            discovered = plugins.discover()
            discovered[0]["parameters"]["properties"]["text"]["type"] = "integer"
            self.assertEqual(plugins.discover()[0]["parameters"]["properties"]["text"]["type"],
                             "string")
            item = plugins.discover()[0]
            plugins.retire()
            self.assertEqual(plugins.discover(), [])
            with self.assertRaisesRegex(ValueError, "已移除"):
                import asyncio
                asyncio.run(plugins.call(item, {"text": "hello"}))
            plugins.activate()
            self.assertEqual(plugins.discover()[0]["name"], "plugin_reader_read")
            import asyncio
            asyncio.run(plugins.close())
            self.assertEqual(plugins.discover(), [])


if __name__ == "__main__":
    unittest.main()
