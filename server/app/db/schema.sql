PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  display_name TEXT,
  home_categories TEXT NOT NULL DEFAULT '[]',
  preference_profile TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS papers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  arxiv_id TEXT NOT NULL UNIQUE,
  version INTEGER NOT NULL DEFAULT 1,
  title TEXT NOT NULL,
  authors TEXT NOT NULL DEFAULT '[]',
  abstract TEXT NOT NULL DEFAULT '',
  ai_summary TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  pdf_url TEXT NOT NULL DEFAULT '',
  abs_url TEXT NOT NULL DEFAULT '',
  published_at TEXT,
  updated_at TEXT,
  markdown_path TEXT,
  storage_dir TEXT,
  raw_metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  analyzed_at TEXT
);

CREATE TABLE IF NOT EXISTS paper_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(paper_id, chunk_index)
);

CREATE VIRTUAL TABLE IF NOT EXISTS paper_chunks_fts
USING fts5(content, paper_id UNINDEXED, chunk_id UNINDEXED);

CREATE TABLE IF NOT EXISTS chat_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  scope TEXT NOT NULL,
  paper_id INTEGER REFERENCES papers(id) ON DELETE SET NULL,
  title TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  selection TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_missions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'queued',
  mode TEXT NOT NULL DEFAULT 'paper',
  message TEXT NOT NULL,
  paper_id INTEGER REFERENCES papers(id) ON DELETE SET NULL,
  selection TEXT,
  attachment_paper_ids TEXT NOT NULL DEFAULT '[]',
  error_message TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS ocr_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id INTEGER REFERENCES papers(id) ON DELETE SET NULL,
  provider_job_id TEXT NOT NULL,
  status TEXT NOT NULL,
  pages_total INTEGER NOT NULL DEFAULT 0,
  pages_extracted INTEGER NOT NULL DEFAULT 0,
  result_url TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS quota_usage (
  usage_date TEXT PRIMARY KEY,
  pages_used INTEGER NOT NULL DEFAULT 0,
  daily_limit INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT NOT NULL,
  status TEXT NOT NULL,
  fetched_count INTEGER NOT NULL DEFAULT 0,
  inserted_count INTEGER NOT NULL DEFAULT 0,
  updated_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crawl_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  category TEXT NOT NULL,
  date_filters TEXT NOT NULL DEFAULT '[]',
  max_results INTEGER NOT NULL DEFAULT 20,
  status TEXT NOT NULL DEFAULT 'queued',
  total_steps INTEGER NOT NULL DEFAULT 0,
  completed_steps INTEGER NOT NULL DEFAULT 0,
  fetched_count INTEGER NOT NULL DEFAULT 0,
  inserted_count INTEGER NOT NULL DEFAULT 0,
  updated_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS crawl_job_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
  step_index INTEGER NOT NULL,
  category TEXT NOT NULL,
  target_date TEXT,
  source TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  fetched_count INTEGER NOT NULL DEFAULT 0,
  inserted_count INTEGER NOT NULL DEFAULT 0,
  updated_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_run_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE(job_id, step_index)
);

CREATE TABLE IF NOT EXISTS llm_cache (
  cache_key TEXT PRIMARY KEY,
  response_text TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS favorite_folders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS paper_favorites (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  folder_id INTEGER REFERENCES favorite_folders(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(user_id, paper_id)
);

CREATE TABLE IF NOT EXISTS daily_paper_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  target_date TEXT NOT NULL,
  categories TEXT NOT NULL DEFAULT '[]',
  max_results INTEGER NOT NULL DEFAULT 12,
  status TEXT NOT NULL DEFAULT 'queued',
  total_papers INTEGER NOT NULL DEFAULT 0,
  completed_papers INTEGER NOT NULL DEFAULT 0,
  inserted_count INTEGER NOT NULL DEFAULT 0,
  updated_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS daily_papers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER REFERENCES daily_paper_runs(id) ON DELETE SET NULL,
  paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  target_date TEXT NOT NULL,
  category TEXT NOT NULL,
  short_summary TEXT NOT NULL DEFAULT '',
  long_summary TEXT NOT NULL DEFAULT '',
  markdown_path TEXT,
  rag_collection TEXT,
  rag_document_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',
  error_message TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(target_date, paper_id, category)
);
