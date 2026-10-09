ALTER TABLE history ADD COLUMN source_metadata_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE history ADD COLUMN failure_code TEXT;
ALTER TABLE history ADD COLUMN deletion_state TEXT NOT NULL DEFAULT 'active';
ALTER TABLE dictionary_entries ADD COLUMN canonical_key TEXT;
