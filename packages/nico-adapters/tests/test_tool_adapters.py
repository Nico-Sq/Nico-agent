import tempfile
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nico_adapters.plugins import LocalPlugins, PluginRetired
from nico_adapters.registry import load_channels, load_tool_adapters


class ToolAdapterTests(unittest.TestCase):
    def test_failed_plugin_prepare_does_not_publish_partial_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.py").write_text(
                "def read(args): return 'ok'\n"
                "TOOLS = [{'name':'read','read_only':True,'parameters':{'type':'object'},"
                "'run':read}, {'name':'bad','parameters':{'type':'array'},'run':read}]\n",
                encoding="utf-8",
            )
            config = root / "plugins.json"
            config.write_text(json.dumps([{"path": "bad.py"}]), encoding="utf-8")
            plugins = LocalPlugins.__new__(LocalPlugins)
            with self.assertRaises(ValueError):
                plugins.__init__(config)
            self.assertEqual(plugins.discover(), [])

    def test_retired_plugin_rejects_pending_call_but_finishes_inflight_call(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "reader.py").write_text(
                    "from threading import Event\n"
                    "STARTED = Event()\nRELEASE = Event()\n"
                    "def read(args):\n"
                    "    STARTED.set()\n    RELEASE.wait(2)\n    return args['value']\n"
                    "TOOLS = [{'name':'read','read_only':True,'parameters':"
                    "{'type':'object','properties':{'value':{'type':'string'}},"
                    "'required':['value']},'run':read}]\n",
                    encoding="utf-8",
                )
                config = root / "plugins.json"
                config.write_text(json.dumps([{"path": "reader.py"}]), encoding="utf-8")
                plugins = LocalPlugins(config)
                item = plugins.discover()[0]
                tool = plugins._active_tools[item["name"]]
                started = tool.run.__globals__["STARTED"]
                release = tool.run.__globals__["RELEASE"]
                call = asyncio.create_task(plugins.call(item, {"value": "ok"}))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 1))
                    plugins.retire()
                    with self.assertRaises(PluginRetired):
                        await plugins.call(item, {"value": "late"})
                    closing = asyncio.create_task(plugins.close())
                    await asyncio.sleep(0)
                    self.assertFalse(closing.done())
                finally:
                    release.set()
                self.assertEqual(await call, {"text": "ok"})
                await closing
                self.assertEqual(plugins.discover(), [])
                self.assertEqual(plugins.state, "closed")

        asyncio.run(scenario())

    def test_close_runs_optional_plugin_cleanup_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "closed.txt"
            (root / "reader.py").write_text(
                "from pathlib import Path\n"
                "def read(args): return 'ok'\n"
                f"def close():\n    p = Path({str(marker)!r})\n"
                "    p.write_text((p.read_text() if p.exists() else '') + 'x')\n"
                "TOOLS = [{'name':'read','read_only':True,'parameters':{'type':'object'},"
                "'run':read}]\n",
                encoding="utf-8",
            )
            config = root / "plugins.json"
            config.write_text(json.dumps([{"path": "reader.py"}]), encoding="utf-8")
            plugins = LocalPlugins(config)
            asyncio.run(plugins.close())
            asyncio.run(plugins.close())
            self.assertEqual(marker.read_text(), "x")
            self.assertEqual(plugins.discover(), [])

    def test_only_requested_channel_entry_points_are_loaded(self):
        loaded = []

        class Console:
            pass

        class ThirdParty:
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
            EntryPoint("console", "nico-adapters", Console),
            EntryPoint("external", "third-party-channel", ThirdParty),
        ]
        with patch("nico_adapters.registry.entry_points", return_value=points):
            selected = load_channels(("console",))

        self.assertEqual(selected, {"console": Console})
        self.assertEqual(loaded, ["console"])

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
            self.assertEqual(plugins.state, "active")
            discovered[0]["parameters"]["properties"]["text"]["type"] = "integer"
            self.assertEqual(plugins.discover()[0]["parameters"]["properties"]["text"]["type"],
                             "string")
            item = plugins.discover()[0]
            plugins.retire()
            self.assertEqual(plugins.state, "retired")
            self.assertEqual(plugins.discover(), [])
            with self.assertRaisesRegex(ValueError, "已移除"):
                import asyncio
                asyncio.run(plugins.call(item, {"text": "hello"}))
            plugins.activate()
            self.assertEqual(plugins.state, "active")
            self.assertEqual(plugins.discover()[0]["name"], "plugin_reader_read")
            import asyncio
            asyncio.run(plugins.close())
            self.assertEqual(plugins.state, "closed")
            self.assertEqual(plugins.discover(), [])
            plugins.activate()
            self.assertEqual(plugins.state, "active")
            self.assertEqual(plugins.discover()[0]["name"], "plugin_reader_read")


if __name__ == "__main__":
    unittest.main()
