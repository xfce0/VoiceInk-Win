CREATE TABLE history (
    id TEXT PRIMARY KEY NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    duration REAL NOT NULL,
    original_text TEXT NOT NULL,
    enhanced_text TEXT,
    selected_variant TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    audio_artifact_path TEXT
);

CREATE INDEX history_newest_idx ON history (created_at DESC, id DESC);

CREATE TABLE dictionary_entries (
    id TEXT PRIMARY KEY NOT NULL,
    phrase TEXT NOT NULL UNIQUE,
    replacement TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
);

CREATE INDEX dictionary_order_idx ON dictionary_entries (phrase COLLATE NOCASE, id);

CREATE TABLE settings (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    language TEXT NOT NULL,
    selected_mode TEXT NOT NULL,
    hotkeys_json TEXT NOT NULL,
    auto_copy INTEGER NOT NULL CHECK (auto_copy IN (0, 1)),
    model_preferences_json TEXT NOT NULL,
    audio_preferences_json TEXT NOT NULL
);
