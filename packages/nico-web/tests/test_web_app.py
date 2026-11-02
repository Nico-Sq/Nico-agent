import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from nico_web import create_app
from nico_web.app import _MaterialUploadBodyLimitMiddleware
from nico_web.backend import WebBackend


class WebAppTests(unittest.TestCase):
    def test_factory_serves_package_resources_and_api(self):
        provider = SimpleNamespace(mode="demo", model="demo", configuration_error=lambda: None)
        extracted = []
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
            max_file_bytes=8,
            extract_text=lambda _filename, data: extracted.append(data) or "",
            login_rate_limited_error=RuntimeError,
        )
        with TestClient(create_app(backend)) as client:
            page = client.get("/")
            config = client.get("/api/config")
            script = client.get("/static/app.js")
            oversized = client.post(
                "/api/materials/extract",
                files={"file": ("large.txt", b"x" * (64 * 1024 + 9), "text/plain")},
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn('/static/shared.js', page.text)
        self.assertEqual(config.json(), {
            "provider": "demo", "model": "demo", "auth_required": False,
            "config_error": None,
        })
        self.assertEqual(script.status_code, 200)
        self.assertIn("async function bootstrap()", script.text)
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(extracted, [])

    def test_material_upload_limit_counts_streamed_body_without_length(self):
        responses = []
        messages = iter((
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5", "more_body": False},
        ))

        async def receive():
            return next(messages)

        async def send(message):
            responses.append(message)

        async def endpoint(_scope, endpoint_receive, endpoint_send):
            while True:
                message = await endpoint_receive()
                if not message.get("more_body", False):
                    break
            await endpoint_send({"type": "http.response.start", "status": 200, "headers": []})
            await endpoint_send({"type": "http.response.body", "body": b"ok"})

        scope = {"type": "http", "method": "POST", "path": "/api/materials/extract", "headers": []}
        asyncio.run(_MaterialUploadBodyLimitMiddleware(endpoint, 4)(scope, receive, send))

        self.assertEqual(responses[0]["status"], 413)
        self.assertIn(b"\"detail\"", responses[1]["body"])


if __name__ == "__main__":
    unittest.main()
