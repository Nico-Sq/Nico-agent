import hashlib
import hmac
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class AuthStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    expires_at INTEGER NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def register(self, username: str, password: str) -> str | None:
        user_id = uuid.uuid4().hex
        salt = secrets.token_bytes(16)
        password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
        try:
            with self.connect() as db:
                db.execute(
                    "INSERT INTO users(id, username, salt, password_hash) VALUES (?, ?, ?, ?)",
                    (user_id, username.lower(), salt, password_hash),
                )
        except sqlite3.IntegrityError:
            return None
        return user_id

    def login(self, username: str, password: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id, salt, password_hash FROM users WHERE username = ?",
                (username.lower(),),
            ).fetchone()
        if row is None:
            return None
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), row[1], 310_000)
        return row[0] if hmac.compare_digest(actual, row[2]) else None

    def issue_token(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute(
                "INSERT INTO login_tokens(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (self._hash_token(token), user_id, int(time.time()) + 7 * 24 * 3600),
            )
        return token

    def resolve_token(self, token: str | None) -> str | None:
        if not token:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT user_id FROM login_tokens WHERE token_hash = ? AND expires_at > ?",
                (self._hash_token(token), int(time.time())),
            ).fetchone()
        return row[0] if row else None

    def revoke_token(self, token: str | None):
        if token:
            with self.connect() as db:
                db.execute("DELETE FROM login_tokens WHERE token_hash = ?", (self._hash_token(token),))

    def username(self, user_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
