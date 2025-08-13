import os
if os.environ.get("REPTY_SQLITE_BACKEND") == "pysqlite3":
    try:
        import pysqlite3 as sqlite3  # type: ignore
        sqlite3 = sqlite3.dbapi2  # type: ignore
    except Exception:  # fallback
        import sqlite3  # type: ignore
else:
    import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Dict

DB_FILENAME = ".repty.db"


def get_db_path() -> str:
    home = Path.home()
    return str(home / DB_FILENAME)


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    # Migrate legacy cmdvault DB if present and new DB missing
    try:
        if not Path(path).exists():
            legacy = Path.home() / ".cmdvault.db"
            if legacy.exists():
                try:
                    legacy.rename(path)
                except Exception:
                    # If rename fails (e.g., cross-device), try copy
                    import shutil
                    shutil.copy2(legacy, path)
                # Move WAL/SHM if they exist
                for suffix in ("-wal", "-shm"):
                    lp = Path(str(legacy) + suffix)
                    np = Path(str(path) + suffix)
                    try:
                        if lp.exists() and not np.exists():
                            try:
                                lp.rename(np)
                            except Exception:
                                import shutil
                                shutil.copy2(lp, np)
                    except Exception:
                        pass
    except Exception:
        pass
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    # Enable WAL for better concurrency and performance
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command TEXT NOT NULL,
            cwd TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            exit_code INTEGER,
            tags TEXT
        );
        """
    )
    # FTS5 external content table
    try:
        cur.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS commands_fts 
            USING fts5(command, tags, content='commands', content_rowid='id');
            """
        )
    except sqlite3.OperationalError as e:
        # Common when SQLite is compiled without FTS5
        if "fts5" in str(e).lower():
            raise RuntimeError(
                "Your Python's SQLite does not support FTS5. Install a build with FTS5 (e.g., Python 3.11+), or install 'pysqlite3-binary' and run REPTY_SQLITE_BACKEND=pysqlite3."
            ) from e
        else:
            raise
    # Triggers to keep FTS in sync
    cur.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS commands_ai AFTER INSERT ON commands BEGIN
          INSERT INTO commands_fts(rowid, command, tags) VALUES (new.id, new.command, new.tags);
        END;
        CREATE TRIGGER IF NOT EXISTS commands_ad AFTER DELETE ON commands BEGIN
          DELETE FROM commands_fts WHERE rowid = old.id;
        END;
        CREATE TRIGGER IF NOT EXISTS commands_au AFTER UPDATE ON commands BEGIN
          DELETE FROM commands_fts WHERE rowid = old.id;
          INSERT INTO commands_fts(rowid, command, tags) VALUES (new.id, new.command, new.tags);
        END;
        """
    )
    conn.commit()


def add_command(conn: sqlite3.Connection, command: str, cwd: Optional[str], exit_code: Optional[int], tags: Optional[str]) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO commands (command, cwd, exit_code, tags) VALUES (?, ?, ?, ?)",
        (command, cwd, exit_code, tags),
    )
    conn.commit()
    return int(cur.lastrowid)


def append_tag(conn: sqlite3.Connection, cmd_id: int, tag: str) -> None:
    tag = tag.strip()
    if not tag:
        return
    cur = conn.cursor()
    cur.execute("SELECT tags FROM commands WHERE id=?", (cmd_id,))
    row = cur.fetchone()
    if not row:
        raise ValueError(f"No command found with id={cmd_id}")
    existing = row["tags"] or ""
    tags = set([t for t in existing.split(",") if t.strip()])
    tags.add(tag)
    new_tags = ",".join(sorted(tags)) if tags else None
    cur.execute("UPDATE commands SET tags=? WHERE id=?", (new_tags, cmd_id))
    conn.commit()


def remove_tag(conn: sqlite3.Connection, cmd_id: int, tag: str) -> None:
    """Remove a tag (case-insensitive exact match) from a command's tags."""
    tag = (tag or "").strip()
    cur = conn.cursor()
    cur.execute("SELECT tags FROM commands WHERE id=?", (cmd_id,))
    row = cur.fetchone()
    if not row:
        return
    existing = row[0] if isinstance(row, (tuple, list)) else row["tags"]
    if not existing:
        return
    parts = [p.strip() for p in str(existing).split(",") if p.strip()]
    new_parts = []
    for p in parts:
        if p.lower() == tag.lower():
            continue
        new_parts.append(p)
    new_tags = ",".join(dict.fromkeys(new_parts)) if new_parts else None
    cur.execute("UPDATE commands SET tags=? WHERE id=?", (new_tags, cmd_id))
    conn.commit()


def parse_inline_tags(command: str) -> List[str]:
    import re
    tags: List[str] = []
    # Matches: #tag label, #tag my-label_1 etc
    for m in re.finditer(r"#tag\s+([A-Za-z0-9_\-:./]+)", command):
        tags.append(m.group(1))
    return tags


def ensure_tags_string(existing: Optional[str], extra: Iterable[str]) -> Optional[str]:
    tags = set([t.strip() for t in (existing or "").split(",") if t.strip()])
    for t in extra:
        t = t.strip()
        if t:
            tags.add(t)
    return ",".join(sorted(tags)) if tags else None


def search_fts(conn: sqlite3.Connection, query: str, limit: int = 50) -> List[sqlite3.Row]:
    """Full-text search returning unique commands, ordered by recency.

    We deduplicate by command string and keep the most recent row per command,
    so users don't see the same command repeated many times. Avoid bm25() to
    support older SQLite/FTS5 contexts.
    """
    cur = conn.cursor()
    try:
        # Preferred: use window function for precise per-command ranking by recency
        cur.execute(
            """
            WITH matched AS (
              SELECT c.id, c.command, c.cwd, c.timestamp, c.exit_code, c.tags
              FROM commands c
              JOIN commands_fts f ON f.rowid = c.id
              WHERE commands_fts MATCH ?
                AND c.command NOT LIKE 'repty %'
                AND c.command NOT LIKE 'source %'
                AND c.command NOT LIKE '. %'
            ), ranked AS (
              SELECT *,
                     ROW_NUMBER() OVER (
                       PARTITION BY command
                       ORDER BY timestamp DESC, id DESC
                     ) AS rn
              FROM matched
            )
            SELECT id, command, cwd, timestamp, exit_code, tags
            FROM ranked
            WHERE rn = 1
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            (query, limit),
        )
        return cur.fetchall()
    except sqlite3.OperationalError:
        # Fallback for SQLite without window functions
        cur.execute(
            """
            WITH matched AS (
              SELECT c.*
              FROM commands c
              JOIN commands_fts f ON f.rowid = c.id
              WHERE commands_fts MATCH ?
                AND c.command NOT LIKE 'repty %'
                AND c.command NOT LIKE 'source %'
                AND c.command NOT LIKE '. %'
            ), latest AS (
              SELECT command, MAX(id) AS max_id
              FROM matched
              GROUP BY command
            )
            SELECT c.id, c.command, c.cwd, c.timestamp, c.exit_code, c.tags
            FROM commands c
            JOIN latest l ON l.max_id = c.id
            ORDER BY c.timestamp DESC, c.id DESC
            LIMIT ?
            """,
            (query, limit),
        )
        return cur.fetchall()


def build_fts_query(raw: str, mode: str = "and", prefix: bool = True) -> str:
    """
    Convert a natural language string into an FTS5 MATCH expression.
    - Splits on whitespace into terms
    - Joins with AND (default) or OR
    - Appends '*' to each term for prefix matching when prefix=True
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    # Basic whitespace split; avoid injecting FTS operators from raw
    parts = [p for p in raw.replace('"', ' ').split() if p]
    if not parts:
        return ""
    if prefix:
        parts = [p + "*" for p in parts]
    joiner = " OR " if mode.lower() == "or" else " AND "
    return joiner.join(parts)


def recent(conn: sqlite3.Connection, limit: int = 20) -> List[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, command, cwd, timestamp, exit_code, tags FROM commands ORDER BY datetime(timestamp) DESC, id DESC LIMIT ?",
        (limit,),
    )
    return cur.fetchall()


def get_last_n(conn: sqlite3.Connection, limit: int = 500) -> List[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, command, cwd, timestamp, exit_code, tags FROM commands ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return cur.fetchall()


def get_by_id(conn: sqlite3.Connection, cmd_id: int) -> Optional[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, command, cwd, timestamp, exit_code, tags FROM commands WHERE id=? LIMIT 1",
        (cmd_id,),
    )
    row = cur.fetchone()
    return row


def last_user_command(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Return the most recent non-internal command (skips repty/source/dot)."""
    cur = conn.cursor()
    cur.execute(
        (
            "SELECT id, command, cwd, timestamp, exit_code, tags FROM commands "
            "WHERE command NOT LIKE 'repty %' AND command NOT LIKE 'source %' AND command NOT LIKE '. %' "
            "ORDER BY id DESC LIMIT 1"
        )
    )
    row = cur.fetchone()
    return row


def search_by_tags(conn: sqlite3.Connection, tokens: List[str], limit: int = 50) -> List[sqlite3.Row]:
    """Return rows whose tags field fuzzy-matches any of the tokens.
    Includes tags like 'favorite' and 'desc:...'. Skips internal commands.
    """
    tokens = [t.strip().lower() for t in tokens if t.strip()]
    if not tokens:
        return []
    cur = conn.cursor()
    cond = " OR ".join(["LOWER(tags) LIKE ?" for _ in tokens])
    params = [f"%{t}%" for t in tokens]
    sql = (
        "SELECT id, command, cwd, timestamp, exit_code, tags FROM commands "
        f"WHERE ({cond}) AND command NOT LIKE 'repty %' AND command NOT LIKE 'source %' AND command NOT LIKE '. %' "
        "ORDER BY id DESC LIMIT ?"
    )
    cur.execute(sql, (*params, limit))
    return cur.fetchall()


def export_to(conn: sqlite3.Connection, out_path: str, limit: Optional[int] = None) -> str:
    out_path = str(Path(out_path).expanduser())
    # Ensure parent directory exists
    try:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    rows = recent(conn, limit or 1000000)
    if out_path.lower().endswith(".csv"):
        import csv
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "timestamp", "exit_code", "cwd", "tags", "command"])
            for r in rows:
                w.writerow([r["id"], r["timestamp"], r["exit_code"], r["cwd"], r["tags"], r["command"]])
        return out_path
    # default markdown
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# Repty Export\n\n")
        f.write("| id | timestamp | exit | cwd | tags | command |\n")
        f.write("|---:|:----------|-----:|-----|------|---------|\n")
        for r in rows:
            cmd = (r["command"] or "").replace("|", "\\|")
            cwd = (r["cwd"] or "").replace("|", "\\|")
            tags = (r["tags"] or "").replace("|", "\\|")
            f.write(f"| {r['id']} | {r['timestamp']} | {r['exit_code']} | {cwd} | {tags} | `{cmd}` |\n")
    return out_path


def format_row(r: sqlite3.Row) -> str:
    tags = f" [{r['tags']}]" if r["tags"] else ""
    ec = r["exit_code"] if r["exit_code"] is not None else "?"
    return f"{r['id']:>6}  {r['timestamp']}  (exit {ec})\n  {r['cwd'] or ''}{tags}\n  $ {r['command']}\n"
