import sqlite3
import os
from typing import Optional, List

DB_PATH = os.path.join(os.path.dirname(__file__), "job_agent.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # makes rows behave like dicts
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            email     TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS job_applications (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER,
            job_url          TEXT,
            company          TEXT,
            role             TEXT,
            jd_text          TEXT,
            resume_original  TEXT,
            resume_tailored  TEXT,
            cover_letter     TEXT,
            analysis         TEXT,
            ats_score        INTEGER,
            status           TEXT DEFAULT 'pending',
            apply_status     TEXT DEFAULT 'not_applied',
            apply_method     TEXT,
            apply_error      TEXT,
            applied_date     TEXT,
            follow_up_sent   INTEGER DEFAULT 0,
            last_email_at    TEXT,
            created_at       TEXT DEFAULT (datetime('now')),
            updated_at       TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS application_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id      INTEGER NOT NULL,
            event_type  TEXT NOT NULL,
            source      TEXT DEFAULT 'system',
            title       TEXT NOT NULL,
            body        TEXT,
            email_from  TEXT,
            email_subj  TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (app_id) REFERENCES job_applications(id) ON DELETE CASCADE
        );
    """)

    # Migrate: add ats_score column if missing (safe on existing DBs)
    cols = [row[1] for row in cur.execute("PRAGMA table_info(job_applications)").fetchall()]
    if "ats_score" not in cols:
        cur.execute("ALTER TABLE job_applications ADD COLUMN ats_score INTEGER")
    if "analysis" not in cols:
        cur.execute("ALTER TABLE job_applications ADD COLUMN analysis TEXT")
    if "apply_status" not in cols:
        cur.execute("ALTER TABLE job_applications ADD COLUMN apply_status TEXT DEFAULT 'not_applied'")
    if "apply_method" not in cols:
        cur.execute("ALTER TABLE job_applications ADD COLUMN apply_method TEXT")
    if "apply_error" not in cols:
        cur.execute("ALTER TABLE job_applications ADD COLUMN apply_error TEXT")
    if "last_email_at" not in cols:
        cur.execute("ALTER TABLE job_applications ADD COLUMN last_email_at TEXT")

    # Seed default user
    cur.execute("SELECT id FROM users LIMIT 1")
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (name, email) VALUES (?, ?)",
            ("Default User", "user@example.com"),
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row):
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Application queries
# ---------------------------------------------------------------------------

def create_application(data: dict) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO job_applications
           (user_id, job_url, company, role, jd_text, resume_original,
            resume_tailored, cover_letter, analysis, ats_score, status, applied_date)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            data.get("user_id", 1),
            data.get("job_url"),
            data.get("company"),
            data.get("role"),
            data.get("jd_text"),
            data.get("resume_original"),
            data.get("resume_tailored"),
            data.get("cover_letter"),
            data.get("analysis"),
            data.get("ats_score"),
            data.get("status", "pending"),
            data.get("applied_date"),
        ),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


def get_application(app_id: int) -> Optional[dict]:
    conn = get_connection()
    cur = conn.execute("SELECT * FROM job_applications WHERE id=?", (app_id,))
    row = _row_to_dict(cur.fetchone())
    conn.close()
    return row


def list_applications(user_id: Optional[int] = None) -> List[dict]:
    conn = get_connection()
    if user_id:
        cur = conn.execute(
            "SELECT * FROM job_applications WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM job_applications ORDER BY created_at DESC"
        )
    rows = [_row_to_dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def update_application(app_id: int, data: dict) -> bool:
    allowed = {
        "company", "role", "jd_text", "resume_original", "resume_tailored",
        "cover_letter", "analysis", "ats_score", "status", "applied_date",
        "follow_up_sent", "apply_status", "apply_method", "apply_error", "last_email_at",
    }
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return False
    fields["updated_at"] = "datetime('now')"
    set_clause = ", ".join(
        f"{k}=datetime('now')" if k == "updated_at" else f"{k}=?"
        for k in fields
    )
    values = [v for k, v in fields.items() if k != "updated_at"] + [app_id]
    conn = get_connection()
    cur = conn.execute(
        f"UPDATE job_applications SET {set_clause} WHERE id=?", values
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def delete_application(app_id: int) -> bool:
    conn = get_connection()
    cur = conn.execute("DELETE FROM job_applications WHERE id=?", (app_id,))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def add_event(app_id: int, event_type: str, title: str, body: str = "",
              source: str = "system", email_from: str = "", email_subj: str = "") -> int:
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO application_events
           (app_id, event_type, source, title, body, email_from, email_subj)
           VALUES (?,?,?,?,?,?,?)""",
        (app_id, event_type, source, title, body, email_from, email_subj),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


def get_events(app_id: int) -> List[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM application_events WHERE app_id=? ORDER BY created_at ASC",
        (app_id,),
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_stats() -> dict:
    conn = get_connection()
    total     = conn.execute("SELECT COUNT(*) FROM job_applications").fetchone()[0]
    interviews= conn.execute("SELECT COUNT(*) FROM job_applications WHERE status='interview'").fetchone()[0]
    offers    = conn.execute("SELECT COUNT(*) FROM job_applications WHERE status='offer'").fetchone()[0]
    followups = conn.execute("SELECT COUNT(*) FROM job_applications WHERE follow_up_sent=1").fetchone()[0]
    conn.close()
    return {"total": total, "interviews": interviews, "offers": offers, "followups": followups}
