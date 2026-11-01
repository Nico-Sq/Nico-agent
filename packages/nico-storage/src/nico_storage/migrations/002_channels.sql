CREATE TABLE IF NOT EXISTS channel_bindings (
    channel_name TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel_name, conversation_id)
);
CREATE INDEX IF NOT EXISTS channel_bindings_session_id ON channel_bindings(session_id);
CREATE TABLE IF NOT EXISTS channel_messages (
    channel_name TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    message_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'response_ready', 'delivery_failed', 'completed')),
    response TEXT,
    delivery_id TEXT UNIQUE,
    delivery_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_name, conversation_id, message_id)
);
