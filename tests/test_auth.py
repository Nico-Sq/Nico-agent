import asyncio
import hashlib
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from career_agent import main
from career_agent.auth import AuthStore, LoginRateLimited


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.original = main.AUTH_REQUIRED, main.auth_store, main.DATA_DIR, main.provider.mode
        main.DATA_DIR = Path(self.temp.name)
        main.auth_store = AuthStore(main.DATA_DIR / "auth.sqlite3")
        main.AUTH_REQUIRED = True
        main.provider.mode = "demo"
        self.alice = TestClient(main.app)
        self.bob = TestClient(main.app)

    def tearDown(self):
        self.alice.close()
        self.bob.close()
        main.AUTH_REQUIRED, main.auth_store, main.DATA_DIR, main.provider.mode = self.original
        self.temp.cleanup()

    def test_login_and_account_data_isolation(self):
        self.assertEqual(self.alice.get("/api/materials").status_code, 401)
        self.assertEqual(self.alice.get("/api/data/backup").status_code, 401)
        self.assertTrue(self.alice.get("/api/config").json()["auth_required"])
        alice = self.alice.post("/api/auth/register", json={
            "username": "Alice", "password": "correct-password",
        })
        self.assertEqual(alice.status_code, 200, alice.text)
        self.assertIn("HttpOnly", alice.headers["set-cookie"])
        self.assertEqual(self.alice.get("/api/auth/me").json()["username"], "alice")
        self.assertEqual(self.bob.post("/api/auth/register", json={
            "username": "alice", "password": "another-password",
        }).status_code, 409)
        self.assertEqual(self.bob.post("/api/auth/register", json={
            "username": "bob", "password": "correct-password",
        }).status_code, 200)
        material = self.alice.post("/api/materials", json={
            "kind": "project", "title": "Alice 的材料", "content": "PRIVATE_ALICE",
        }).json()
        interview = self.alice.post("/api/interview/sessions", json={
            "goal": "Alice 的面试", "material_ids": [material["id"]],
        }).json()
        self.assertEqual(self.bob.get("/api/materials").json(), [])
        bob_backup = Path(self.temp.name) / "bob-backup.sqlite3"
        bob_backup.write_bytes(self.bob.get("/api/data/backup").content)
        with closing(sqlite3.connect(bob_backup)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM materials").fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name = 'users'").fetchone())
        self.assertEqual(self.bob.get(f"/api/interview/sessions/{interview['id']}").status_code, 404)
        private_chat = self.alice.post("/api/chat/sessions").json()
        self.alice.post(f"/api/chat/sessions/{private_chat['id']}/messages/claim", json={
            "text": "仅 Alice 可查找的内容",
        })
        private_source = self.alice.get(
            f"/api/chat/sessions/{private_chat['id']}"
        ).json()["messages"][0]
        private_candidate = self.alice.post(
            f"/api/chat/sessions/{private_chat['id']}/memory-candidates",
            json={"content": "Alice 私有候选", "source_message_ids": [private_source["id"]]},
        ).json()
        self.assertEqual(self.bob.get("/api/memory-candidates").json(), [])
        self.assertEqual(self.bob.get(
            f"/api/memory-candidates/{private_candidate['id']}"
        ).status_code, 404)
        self.assertEqual(self.bob.get("/api/chat/sessions", params={
            "q": "仅 Alice 可查找的内容",
        }).json(), [])
        self.assertEqual(self.bob.put(f"/api/interview/sessions/{interview['id']}/title", json={
            "title": "其他账户的会话",
        }).status_code, 404)
        self.assertEqual(self.bob.post("/api/interview/sessions", json={
            "goal": "窃取材料", "material_ids": [material["id"]],
        }).status_code, 422)
        self.assertEqual(self.bob.get(f"/api/interview/sessions/{interview['id']}/export.md").status_code, 404)
        bob_chat = self.bob.post("/api/chat/sessions").json()["id"]
        tool_reply = self.bob.post(f"/api/chat/sessions/{bob_chat}/messages", json={
            "text": f"请解释材料 {material['id']}",
        }).json()["message"]
        self.assertNotIn("PRIVATE_ALICE", tool_reply)
        self.assertEqual(self.alice.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.alice.get("/api/materials").status_code, 401)
        self.assertEqual(self.alice.post("/api/auth/login", json={
            "username": "alice", "password": "wrong-password",
        }).status_code, 401)
        self.assertEqual(self.alice.post("/api/auth/login", json={
            "username": "alice", "password": "correct-password",
        }).status_code, 200)
        self.assertEqual(self.alice.get("/api/materials").json()[0]["id"], material["id"])

    def test_login_is_limited_after_repeated_failures(self):
        self.alice.post("/api/auth/register", json={
            "username": "alice", "password": "correct-password",
        })
        now = int(time.time())
        with patch("career_agent.auth.time.time", return_value=now):
            for _ in range(5):
                response = self.bob.post("/api/auth/login", json={
                    "username": "alice", "password": "wrong-password",
                })
                self.assertEqual(response.status_code, 401)
            blocked = self.bob.post("/api/auth/login", json={
                "username": "alice", "password": "correct-password",
            })
            self.assertEqual(blocked.status_code, 429)
        with patch("career_agent.auth.time.time", return_value=now + 901):
            accepted = self.bob.post("/api/auth/login", json={
                "username": "alice", "password": "correct-password",
            })
        self.assertEqual(accepted.status_code, 200)

    def test_auth_store_hashes_unknown_users_and_prunes_expired_tokens(self):
        store = AuthStore(Path(self.temp.name) / "auth-direct.sqlite3")
        with patch("career_agent.auth.hashlib.pbkdf2_hmac", wraps=hashlib.pbkdf2_hmac) as hashing:
            self.assertIsNone(store.login("nobody", "whatever-password"))
        self.assertEqual(hashing.call_count, 1)

        user_id = store.register("carol", "correct-password")
        token = store.issue_token(user_id)
        self.assertEqual(store.resolve_token(token), user_id)
        with closing(sqlite3.connect(store.path)) as db:
            db.execute("UPDATE login_tokens SET expires_at = ?", (int(time.time()) - 1,))
            db.commit()
        self.assertIsNone(store.resolve_token(token))
        store.issue_token(user_id)
        with closing(sqlite3.connect(store.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM login_tokens").fetchone()[0], 1)

    def test_locked_login_skips_password_hashing(self):
        store = AuthStore(Path(self.temp.name) / "auth-locked.sqlite3")
        store.register("dave", "correct-password")
        now = int(time.time())
        with patch("career_agent.auth.time.time", return_value=now):
            for _ in range(5):
                self.assertIsNone(store.login("dave", "wrong-password"))
            with patch("career_agent.auth.hashlib.pbkdf2_hmac", wraps=hashlib.pbkdf2_hmac) as hashing:
                with self.assertRaises(LoginRateLimited):
                    store.login("dave", "correct-password")
                self.assertEqual(hashing.call_count, 0)
            self.assertIsNone(store.login("nobody", "wrong-password"))

    def test_parallel_accounts_keep_request_context_separate(self):
        for client, username in ((self.alice, "alice"), (self.bob, "bob")):
            self.assertEqual(client.post("/api/auth/register", json={
                "username": username, "password": "correct-password",
            }).status_code, 200)

        async def scenario():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                         cookies={"nico_session": self.alice.cookies.get("nico_session")}) as alice:
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                             cookies={"nico_session": self.bob.cookies.get("nico_session")}) as bob:
                    await asyncio.gather(
                        alice.post("/api/materials", json={"kind": "project", "title": "Alice", "content": "A_ONLY"}),
                        bob.post("/api/materials", json={"kind": "project", "title": "Bob", "content": "B_ONLY"}),
                    )
                    return await asyncio.gather(alice.get("/api/materials"), bob.get("/api/materials"))

        alice_list, bob_list = asyncio.run(scenario())
        self.assertEqual([item["content"] for item in alice_list.json()], ["A_ONLY"])
        self.assertEqual([item["content"] for item in bob_list.json()], ["B_ONLY"])
