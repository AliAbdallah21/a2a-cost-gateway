"""Phase 4 trace store — build a queryable SQLite view of the JSONL event logs.

See arch.md's Phase 4 design notes and schema.md's trace store schema for the
reasoning behind every choice here: one denormalized `events` table (not several
— every phase so far has added a new event_type, which would force a migration
under a normalized schema), JSONL stays canonical (this script only ever reads
it, nothing writes back), and every run drops and fully rebuilds the table from
whatever logs/run_*.jsonl files currently exist — no incremental ingestion, no
dedup key, by deliberate choice (see arch.md).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"
TRACE_DB_PATH = LOGS_DIR / "trace.db"

_SCHEMA = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    source_file TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    task_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX idx_events_task_id ON events(task_id);
CREATE INDEX idx_events_event_type ON events(event_type);
"""


def build_trace_db(logs_dir: Path = LOGS_DIR, db_path: Path = TRACE_DB_PATH) -> int:
    """Drop and fully rebuild db_path's events table from every logs_dir/run_*.jsonl file.

    Returns the number of events ingested. Idempotent by construction — the table
    is always dropped and rebuilt from scratch, so running this twice in a row
    produces the same result, not double-counted rows (see arch.md's Phase 4 notes
    on why full rebuild was chosen over incremental ingestion with a dedup key).

    A malformed or truncated line (e.g. a process killed mid-write, exactly the
    kind of partial write the port-race in tests/test_trace_store.py's fixture
    could realistically leave behind) is skipped with a warning to stderr, not
    fatal to the whole rebuild — every other module in this project (cost
    estimation, budget checks, request forwarding) was built to degrade on bad
    input rather than crash; this is the same rule applied here.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("DROP TABLE IF EXISTS events;")
        conn.executescript(_SCHEMA)

        event_count = 0
        skipped_count = 0
        for jsonl_path in sorted(logs_dir.glob("run_*.jsonl")):
            with jsonl_path.open("r", encoding="utf-8") as f:
                for line_number, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        conn.execute(
                            "INSERT INTO events (source_file, timestamp, task_id, event_type, actor, payload) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                jsonl_path.name,
                                record["timestamp"],
                                record["task_id"],
                                record["event_type"],
                                record["actor"],
                                json.dumps(record["payload"]),
                            ),
                        )
                        event_count += 1
                    except Exception as exc:
                        skipped_count += 1
                        print(
                            f"WARNING: skipping malformed line {jsonl_path.name}:{line_number} "
                            f"({type(exc).__name__}: {exc})",
                            file=sys.stderr,
                        )
        conn.commit()
        if skipped_count:
            print(f"WARNING: skipped {skipped_count} malformed line(s) total", file=sys.stderr)
        return event_count
    finally:
        conn.close()


def main() -> None:
    """Rebuild logs/trace.db from every logs/run_*.jsonl file present right now."""
    count = build_trace_db()
    print(f"Ingested {count} events from {LOGS_DIR} into {TRACE_DB_PATH}")


if __name__ == "__main__":
    main()
