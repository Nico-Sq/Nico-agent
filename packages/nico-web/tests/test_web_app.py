import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from nico_web import create_app
from nico_web.backend import WebBackend


class WebAppTests(unittest.TestCase):
    def test_factory_serves_package_resources_and_api(self):
        provider = SimpleNamespace(mode="demo", model="demo", configuration_error=lambda: None)
        backend = WebBackend(
            data_dir=lambda: Path("data"),
            auth_required=lambda: False,
            auth_store=lambda: None,
            store=lambda: None,
            runtime=lambda: None,
            provider=lambda: provider,
            model_catalog=lambda: SimpleNamespace(list=lambda: []),
            mcp_servers=lambda: None,
            local_plugins=lambda: None,
            make_store=lambda _path: None,
            make_runtime=lambda _store, _provider: None,
            cancel_session_runs=lambda _session_id: 0,
            max_file_bytes=5_000_000,
            extract_text=lambda _filename, _data: "",
            login_rate_limited_error=RuntimeError,
        )
        with TestClient(create_app(backend)) as client:
            page = client.get("/")
            config = client.get("/api/config")
            script = client.get("/static/app.js")

        self.assertEqual(page.status_code, 200)
        self.assertIn('/static/shared.js', page.text)
        self.assertEqual(config.json(), {
            "provider": "demo", "model": "demo", "auth_required": False,
            "config_error": None,
        })
        self.assertEqual(script.status_code, 200)
        self.assertIn("async function bootstrap()", script.text)


if __name__ == "__main__":
    unittest.main()
