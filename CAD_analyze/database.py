"""
SQLite database layer for the Construction Material Tracker.

Tables
------
projects            – project metadata
uploaded_files      – one row per file uploaded to a project (CAD or spec)
jobs                – persisted job records (extraction + generation)
tracker_rows        – final merged material rows (one per building × material)
spec_companies      – global spec library: one row per company
spec_company_files  – files belonging to a spec company
"""

import sqlite3
import json
import os
from datetime import datetime, timezone
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "tracker.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    owner          TEXT DEFAULT '',
    epcm           TEXT DEFAULT '',
    project_no     TEXT DEFAULT '',
    contractor     TEXT DEFAULT '',
    sub_contractor TEXT DEFAULT '',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uploaded_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_type    TEXT NOT NULL CHECK(file_type IN ('cad', 'spec')),
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    size_bytes   INTEGER DEFAULT 0,
    uploaded_at  TEXT NOT NULL,
    UNIQUE(project_id, file_type, filename)
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    project_id  TEXT REFERENCES projects(id) ON DELETE SET NULL,
    job_type    TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'queued',
    progress    INTEGER DEFAULT 0,
    total       INTEGER DEFAULT 0,
    log_json    TEXT DEFAULT '[]',
    result_json TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spec_companies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spec_company_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES spec_companies(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    size_bytes   INTEGER DEFAULT 0,
    uploaded_at  TEXT NOT NULL,
    UNIQUE(company_id, filename)
);

CREATE TABLE IF NOT EXISTS tracker_rows (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id            TEXT REFERENCES projects(id) ON DELETE CASCADE,
    gen_job_id            TEXT,
    sn                    INTEGER,
    building              TEXT,
    unit                  TEXT,
    system                TEXT,
    material_category     TEXT,
    material_name         TEXT,
    adnoc_spec_ref        TEXT,
    international_standard TEXT,
    description           TEXT,
    approved_vendor       TEXT,
    brand                 TEXT,
    origin                TEXT,
    unit_of_measure       TEXT,
    quantity              TEXT,
    drawing_ref           TEXT,
    submittal_status      TEXT,
    consultant_approval   TEXT,
    client_approval       TEXT,
    avl_status            TEXT,
    po_status             TEXT,
    production_status     TEXT,
    delivery_status       TEXT,
    site_status           TEXT,
    installation_status   TEXT,
    generated_at          TEXT NOT NULL
);
"""


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    """Create tables if they don't exist and migrate legacy _index.json data."""
    with _conn() as con:
        con.executescript(SCHEMA)
    _migrate_legacy_index()


def _migrate_legacy_index():
    """One-time import of the old JSON project index into SQLite."""
    index_path = os.path.join(os.path.dirname(__file__), "projects", "_index.json")
    if not os.path.exists(index_path):
        return

    with open(index_path, "r", encoding="utf-8") as f:
        projects = json.load(f)

    with _conn() as con:
        for p in projects:
            # skip if already imported
            exists = con.execute(
                "SELECT 1 FROM projects WHERE id = ?", (p["id"],)
            ).fetchone()
            if exists:
                continue

            con.execute(
                """INSERT INTO projects (id, name, owner, epcm, project_no,
                   contractor, sub_contractor, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    p["id"], p["name"], p.get("owner", ""), p.get("epcm", ""),
                    p.get("project_no", ""), p.get("contractor", ""),
                    p.get("sub_contractor", ""), p.get("created_at", _now()),
                ),
            )

            base_dir = os.path.join(os.path.dirname(__file__), "projects", p["id"])
            now = _now()
            for fname in p.get("cad_files", []):
                stored = os.path.join(base_dir, "cad", fname)
                size = os.path.getsize(stored) if os.path.exists(stored) else 0
                con.execute(
                    """INSERT OR IGNORE INTO uploaded_files
                       (project_id, file_type, filename, stored_path, size_bytes, uploaded_at)
                       VALUES (?, 'cad', ?, ?, ?, ?)""",
                    (p["id"], fname, stored, size, now),
                )
            for fname in p.get("spec_files", []):
                stored = os.path.join(base_dir, "spec", fname)
                size = os.path.getsize(stored) if os.path.exists(stored) else 0
                con.execute(
                    """INSERT OR IGNORE INTO uploaded_files
                       (project_id, file_type, filename, stored_path, size_bytes, uploaded_at)
                       VALUES (?, 'spec', ?, ?, ?, ?)""",
                    (p["id"], fname, stored, size, now),
                )


# ── helpers ────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_project(row) -> dict:
    p = dict(row)
    return p


def _project_with_files(con, project_id: str) -> dict | None:
    row = con.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if not row:
        return None
    p = _row_to_project(row)
    files = con.execute(
        "SELECT file_type, filename, size_bytes, uploaded_at FROM uploaded_files WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    p["cad_files"] = [
        {"filename": r["filename"], "size_bytes": r["size_bytes"], "uploaded_at": r["uploaded_at"]}
        for r in files if r["file_type"] == "cad"
    ]
    p["spec_files"] = [
        {"filename": r["filename"], "size_bytes": r["size_bytes"], "uploaded_at": r["uploaded_at"]}
        for r in files if r["file_type"] == "spec"
    ]
    return p


# ── project CRUD ───────────────────────────────────────────────────────────────

def create_project(project_id: str, name: str, owner: str = "", epcm: str = "",
                   project_no: str = "", contractor: str = "",
                   sub_contractor: str = "") -> dict:
    now = _now()
    with _conn() as con:
        con.execute(
            """INSERT INTO projects (id, name, owner, epcm, project_no,
               contractor, sub_contractor, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, name, owner, epcm, project_no, contractor, sub_contractor, now),
        )
    return get_project(project_id)


def list_projects() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM projects ORDER BY created_at DESC"
        ).fetchall()
        return [_project_with_files(con, r["id"]) for r in rows]


def get_project(project_id: str) -> dict | None:
    with _conn() as con:
        return _project_with_files(con, project_id)


def delete_project(project_id: str):
    with _conn() as con:
        con.execute("DELETE FROM projects WHERE id = ?", (project_id,))


# ── file storage ───────────────────────────────────────────────────────────────

def add_file(project_id: str, file_type: str, filename: str,
             stored_path: str, size_bytes: int = 0):
    """Record a newly uploaded file. Silently ignores duplicates."""
    with _conn() as con:
        con.execute(
            """INSERT OR IGNORE INTO uploaded_files
               (project_id, file_type, filename, stored_path, size_bytes, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (project_id, file_type, filename, stored_path, size_bytes, _now()),
        )


def list_files(project_id: str, file_type: str | None = None) -> list[dict]:
    query = "SELECT * FROM uploaded_files WHERE project_id = ?"
    args: list = [project_id]
    if file_type:
        query += " AND file_type = ?"
        args.append(file_type)
    query += " ORDER BY uploaded_at"
    with _conn() as con:
        rows = con.execute(query, args).fetchall()
        return [dict(r) for r in rows]


def delete_file(project_id: str, file_type: str, filename: str):
    with _conn() as con:
        con.execute(
            "DELETE FROM uploaded_files WHERE project_id=? AND file_type=? AND filename=?",
            (project_id, file_type, filename),
        )


# ── job persistence ────────────────────────────────────────────────────────────

def upsert_job(job_id: str, project_id: str | None, job_type: str,
               status: str, progress: int, total: int,
               log: list, result=None):
    now = _now()
    with _conn() as con:
        existing = con.execute(
            "SELECT id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if existing:
            con.execute(
                """UPDATE jobs SET status=?, progress=?, total=?,
                   log_json=?, result_json=?, updated_at=? WHERE id=?""",
                (status, progress, total,
                 json.dumps(log, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False) if result is not None else None,
                 now, job_id),
            )
        else:
            con.execute(
                """INSERT INTO jobs (id, project_id, job_type, status, progress, total,
                   log_json, result_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, project_id, job_type, status, progress, total,
                 json.dumps(log, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False) if result is not None else None,
                 now, now),
            )


def get_job(job_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["log"] = json.loads(d.pop("log_json") or "[]")
        d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
        return d


def list_jobs(project_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["log"] = json.loads(d.pop("log_json") or "[]")
            d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
            result.append(d)
        return result


# ── tracker rows ───────────────────────────────────────────────────────────────

def save_tracker_rows(project_id: str, gen_job_id: str, rows: list[dict]):
    """Persist merged tracker rows for a project. Replaces previous rows for same gen_job_id."""
    now = _now()
    with _conn() as con:
        con.execute("DELETE FROM tracker_rows WHERE gen_job_id = ?", (gen_job_id,))
        con.executemany(
            """INSERT INTO tracker_rows (
                project_id, gen_job_id, sn, building, unit, system,
                material_category, material_name, adnoc_spec_ref,
                international_standard, description, approved_vendor,
                brand, origin, unit_of_measure, quantity, drawing_ref,
                submittal_status, consultant_approval, client_approval,
                avl_status, po_status, production_status, delivery_status,
                site_status, installation_status, generated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    project_id, gen_job_id,
                    r.get("sn"), r.get("building"), r.get("unit"), r.get("system"),
                    r.get("material_category"), r.get("material_name"),
                    r.get("adnoc_spec_ref"), r.get("international_standard"),
                    r.get("description"), r.get("approved_vendor"),
                    r.get("brand"), r.get("origin"), r.get("unit"),
                    r.get("quantity"), r.get("drawing_ref"),
                    r.get("submittal_status"), r.get("consultant_approval"),
                    r.get("client_approval"), r.get("avl_status"),
                    r.get("po_status"), r.get("production_status"),
                    r.get("delivery_status"), r.get("site_status"),
                    r.get("installation_status"), now,
                )
                for r in rows
            ],
        )


def list_materials(project_id: str | None = None, building: str | None = None,
                   material_name: str | None = None) -> list[dict]:
    """Query tracker_rows with optional filters."""
    conditions = []
    args: list = []
    if project_id:
        conditions.append("project_id = ?")
        args.append(project_id)
    if building:
        conditions.append("building = ?")
        args.append(building)
    if material_name:
        conditions.append("material_name LIKE ?")
        args.append(f"%{material_name}%")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    query = f"SELECT * FROM tracker_rows {where} ORDER BY project_id, sn"
    with _conn() as con:
        rows = con.execute(query, args).fetchall()
        return [dict(r) for r in rows]


# ── spec library ───────────────────────────────────────────────────────────────

SPEC_LIB_DIR = os.path.join(os.path.dirname(__file__), "spec_library")
os.makedirs(SPEC_LIB_DIR, exist_ok=True)


def create_spec_company(company_name: str) -> dict:
    now = _now()
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO spec_companies (company_name, created_at) VALUES (?, ?)",
            (company_name, now),
        )
        return {"id": cur.lastrowid, "company_name": company_name,
                "created_at": now, "files": []}


def find_spec_company_by_name(company_name: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM spec_companies WHERE company_name = ?", (company_name,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        files = con.execute(
            "SELECT filename, stored_path, size_bytes, uploaded_at FROM spec_company_files WHERE company_id = ?",
            (row["id"],),
        ).fetchall()
        d["files"] = [dict(f) for f in files]
        return d


def list_spec_companies() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM spec_companies ORDER BY company_name"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            files = con.execute(
                "SELECT filename, size_bytes, uploaded_at FROM spec_company_files WHERE company_id = ?",
                (r["id"],),
            ).fetchall()
            d["files"] = [dict(f) for f in files]
            result.append(d)
        return result


def get_spec_company(company_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM spec_companies WHERE id = ?", (company_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        files = con.execute(
            "SELECT filename, stored_path, size_bytes, uploaded_at FROM spec_company_files WHERE company_id = ?",
            (company_id,),
        ).fetchall()
        d["files"] = [dict(f) for f in files]
        return d


def add_spec_company_file(company_id: int, filename: str,
                          stored_path: str, size_bytes: int = 0):
    with _conn() as con:
        con.execute(
            """INSERT OR IGNORE INTO spec_company_files
               (company_id, filename, stored_path, size_bytes, uploaded_at)
               VALUES (?, ?, ?, ?, ?)""",
            (company_id, filename, stored_path, size_bytes, _now()),
        )


def delete_spec_company(company_id: int):
    with _conn() as con:
        con.execute("DELETE FROM spec_companies WHERE id = ?", (company_id,))


def load_spec_company_file_bytes(company_id: int) -> list[dict]:
    """Load actual file bytes from disk for a spec company (for extraction)."""
    company = get_spec_company(company_id)
    if not company:
        return []
    result = []
    for f in company["files"]:
        path = f["stored_path"]
        if os.path.exists(path):
            with open(path, "rb") as fp:
                result.append({"filename": f["filename"], "bytes": fp.read()})
    return result
