CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('chat', 'interview')),
                    status TEXT NOT NULL,
                    goal TEXT NOT NULL DEFAULT '',
                    question_count INTEGER NOT NULL DEFAULT 0,
                    max_questions INTEGER NOT NULL DEFAULT 3,
                    version INTEGER NOT NULL DEFAULT 0,
                    run_started INTEGER NOT NULL DEFAULT 0,
                    chat_memory TEXT NOT NULL DEFAULT '',
                    model_id TEXT NOT NULL DEFAULT 'default',
                    title TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_tool_grants (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    tool_name TEXT NOT NULL,
                    PRIMARY KEY (session_id, tool_name)
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed')),
                    memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    confirmed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS memory_candidate_sources (
                    candidate_id TEXT NOT NULL REFERENCES memory_candidates(id) ON DELETE CASCADE,
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    PRIMARY KEY (candidate_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS materials (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('resume', 'job_description', 'project')),
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS claims (
                    id TEXT PRIMARY KEY,
                    material_id TEXT REFERENCES materials(id),
                    text TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('draft', 'confirmed')),
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS session_materials (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    material_id TEXT NOT NULL REFERENCES materials(id),
                    PRIMARY KEY (session_id, material_id)
                );
                CREATE TABLE IF NOT EXISTS session_claims (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    claim_id TEXT NOT NULL REFERENCES claims(id),
                    PRIMARY KEY (session_id, claim_id)
                );
                CREATE TABLE IF NOT EXISTS interview_items (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    number INTEGER NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT,
                    feedback TEXT,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (session_id, number)
                );
                CREATE TABLE IF NOT EXISTS interview_evidence (
                    session_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    quote TEXT NOT NULL,
                    PRIMARY KEY (session_id, number, quote),
                    FOREIGN KEY (session_id, number) REFERENCES interview_items(session_id, number)
                );
                CREATE TABLE IF NOT EXISTS practice_sessions (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                    source_session_id TEXT NOT NULL REFERENCES sessions(id),
                    question_number INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_session_id ON messages(session_id, id);
                CREATE INDEX IF NOT EXISTS events_session_id ON events(session_id, id);
                CREATE INDEX IF NOT EXISTS memory_candidates_session_id
                    ON memory_candidates(session_id, status, created_at);
                CREATE INDEX IF NOT EXISTS memory_candidate_sources_message_id
                    ON memory_candidate_sources(message_id);
