CREATE TABLE IF NOT EXISTS dayone_sync_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT NOT NULL UNIQUE,
  device_id TEXT NOT NULL,
  transcript TEXT NOT NULL,
  dream_local_date TEXT NOT NULL,
  dream_local_time TEXT NOT NULL,
  audio_filename TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  dayone_entry_id TEXT,
  completed_at TEXT,
  last_error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dayone_sync_jobs_status_date
  ON dayone_sync_jobs(status, dream_local_date, dream_local_time);

CREATE INDEX IF NOT EXISTS idx_dayone_sync_jobs_idempotency_key
  ON dayone_sync_jobs(idempotency_key);
