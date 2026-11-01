from __future__ import annotations

import sqlite3


BUSINESS_SCHEMA_VERSION = 2
AUTH_SCHEMA_VERSION = 1

BUSINESS_TABLES = {
    "sessions": {"id", "kind", "status", "goal", "question_count", "max_questions", "version",
                 "run_started", "chat_memory", "model_id", "title", "created_at"},
    "messages": {"id", "session_id", "role", "content", "created_at"},
    "events": {"id", "session_id", "kind", "detail", "created_at"},
    "session_tool_grants": {"session_id", "tool_name"},
    "memories": {"id", "content", "created_at"},
    "memory_candidates": {"id", "session_id", "content", "status", "memory_id", "created_at",
                          "updated_at", "confirmed_at"},
    "memory_candidate_sources": {"candidate_id", "message_id"},
    "materials": {"id", "kind", "title", "content", "created_at", "revoked_at"},
    "claims": {"id", "material_id", "text", "status", "created_at", "revoked_at"},
    "session_materials": {"session_id", "material_id"},
    "session_claims": {"session_id", "claim_id"},
    "interview_items": {"session_id", "number", "question", "answer", "feedback", "skipped"},
    "interview_evidence": {"session_id", "number", "quote"},
    "practice_sessions": {"session_id", "source_session_id", "question_number"},
    "channel_bindings": {"channel_name", "conversation_id", "sender_id", "session_id", "created_at"},
    "channel_messages": {"channel_name", "conversation_id", "message_id", "sender_id", "session_id",
                         "message_hash", "status", "response", "delivery_id", "delivery_error",
                         "created_at", "updated_at"},
}
AUTH_TABLES = {
    "users": {"id", "username", "salt", "password_hash"},
    "login_tokens": {"token_hash", "user_id", "expires_at"},
    "login_attempts": {"username", "failures", "first_failed_at"},
}
BUSINESS_INDEXES = {
    "messages_session_id",
    "events_session_id",
    "memory_candidates_session_id",
    "memory_candidate_sources_message_id",
    "channel_bindings_session_id",
}
BUSINESS_LEGACY_COLUMNS = {
    "sessions": {"max_questions", "run_started", "chat_memory", "title", "model_id"},
    "materials": {"revoked_at"},
    "claims": {"revoked_at"},
    "interview_items": {"skipped"},
}
RESTORE_REQUIRED_TABLES = {
    "sessions", "messages", "events", "materials", "claims", "session_materials",
    "session_claims", "interview_items", "interview_evidence", "practice_sessions",
}
RESTORE_OPTIONAL_TABLES = {
    "session_tool_grants", "memories", "memory_candidates", "memory_candidate_sources",
}
LEGACY_RESTORE_MARKERS = {"sessions", "materials", "claims", "interview_items"}
BUSINESS_SCHEMA_HISTORY = {
    1: ({table: set(columns) for table, columns in BUSINESS_TABLES.items()
         if table not in {"channel_bindings", "channel_messages"}},
        BUSINESS_INDEXES - {"channel_bindings_session_id"}),
    2: ({table: set(columns) for table, columns in BUSINESS_TABLES.items()}, set(BUSINESS_INDEXES)),
}
AUTH_SCHEMA_HISTORY = {
    1: ({table: set(columns) for table, columns in AUTH_TABLES.items()}, set()),
}


class DatabaseSchemaError(RuntimeError):
    """A SQLite file is not a schema version this Nico-agent build can safely use."""


def schema_version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def validate_schema(
    db: sqlite3.Connection,
    kind: str,
    *,
    allow_legacy: bool = False,
    require_restore_tables: bool = False,
) -> int:
    history, current = _manifest(kind)
    version = schema_version(db)
    if version > current:
        raise DatabaseSchemaError(
            f"database schema version {version} is newer than supported version {current}"
        )
    if (version == 0 and not allow_legacy) or (version != 0 and version not in history):
        raise DatabaseSchemaError(f"unsupported database schema version {version}")
    tables, indexes = history.get(version, history[current])

    objects = db.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    user_tables = {name for object_type, name in objects if object_type == "table"}
    user_indexes = {name for object_type, name in objects if object_type == "index"}
    invalid_objects = any(
        object_type not in ("table", "index")
        or (object_type == "table" and name not in tables)
        or (object_type == "index" and name not in indexes)
        for object_type, name in objects
    )
    if invalid_objects:
        raise DatabaseSchemaError("database contains objects outside the known schema")
    if require_restore_tables and version == 0 and not LEGACY_RESTORE_MARKERS <= user_tables:
        raise DatabaseSchemaError("legacy database is missing required backup tables")

    if version != 0:
        if user_tables != set(tables) or user_indexes != set(indexes):
            raise DatabaseSchemaError("database schema does not match its recorded version")
        for table, expected in tables.items():
            if _columns(db, table) != expected:
                raise DatabaseSchemaError("database schema does not match its recorded version")
    elif user_tables:
        marker = "sessions" if kind == "business" else "users"
        if marker not in user_tables:
            raise DatabaseSchemaError("database schema does not match a known legacy database")
        for table in user_tables:
            actual = _columns(db, table)
            permitted = tables[table]
            if not actual <= permitted:
                raise DatabaseSchemaError("legacy database contains unknown columns")
            required = permitted - BUSINESS_LEGACY_COLUMNS.get(table, set()) if kind == "business" else permitted
            if not required <= actual:
                raise DatabaseSchemaError("legacy database is missing required columns")

    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise DatabaseSchemaError("SQLite file integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise DatabaseSchemaError("SQLite foreign key check failed")
    return version


def _manifest(
    kind: str,
) -> tuple[dict[int, tuple[dict[str, set[str]], set[str]]], int]:
    if kind == "business":
        return BUSINESS_SCHEMA_HISTORY, BUSINESS_SCHEMA_VERSION
    if kind == "auth":
        return AUTH_SCHEMA_HISTORY, AUTH_SCHEMA_VERSION
    raise ValueError(f"unknown database kind: {kind}")


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
