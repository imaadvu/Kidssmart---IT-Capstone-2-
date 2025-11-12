# database.py (WAL + upgrades + USD price support)
from __future__ import annotations
import sqlite3, json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

DB = "search_results.db"

def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=30, check_same_thread=False, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    con.execute("PRAGMA busy_timeout=30000;")
    return con

def _domain_of(url: str) -> str:
    try:
        p = urlparse(url); return (p.netloc or url).lower()
    except Exception:
        return url.lower()

def create_database() -> None:
    con = _connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      email TEXT UNIQUE,
      name TEXT,
      password_hash TEXT,
      role TEXT DEFAULT 'user'
    );
    CREATE TABLE IF NOT EXISTS sources(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      url TEXT UNIQUE,
      last_scraped_at TEXT
    );
    CREATE TABLE IF NOT EXISTS queries(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      topic TEXT,
      filters_json TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS programs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source_id INTEGER,
      url TEXT,
      title TEXT,
      description TEXT,
      price REAL,
      currency TEXT,
      price_usd_real REAL,
      start_date TEXT,
      end_date TEXT,
      mode TEXT,
      venue TEXT,
      city TEXT,
      country TEXT,
      type TEXT,
      is_approved INTEGER DEFAULT 1,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(url, title),
      FOREIGN KEY(source_id) REFERENCES sources(id)
    );
    """)
    # upgrade: add price_usd_real if missing
    cols = [c[1] for c in con.execute("PRAGMA table_info(programs)").fetchall()]
    if "price_usd_real" not in cols:
        con.execute("ALTER TABLE programs ADD COLUMN price_usd_real REAL;")
    con.close()

# -------- users --------
def create_user(email: str, name: str, password: str, role: str = "user") -> Tuple[bool,str]:
    email = email.strip().lower(); name = (name or "").strip()
    if not email or not password: return False, "Email and password are required."
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE;")
        con.execute("INSERT INTO users(email,name,password_hash,role) VALUES (?,?,?,?)",
                    (email, name or email.split("@")[0], password, role))
        con.commit()
        return True, "Account created."
    except sqlite3.IntegrityError:
        con.rollback(); return False, "Email already exists."
    except Exception as e:
        con.rollback(); return False, f"Error: {e}"
    finally:
        con.close()

def get_user_by_email(email: str) -> Optional[Tuple]:
    con = _connect()
    row = con.execute(
        "SELECT id,email,name,password_hash,role FROM users WHERE LOWER(email)=LOWER(?)",
        (email.strip(),)
    ).fetchone()
    con.close()
    return row

def verify_user(email: str, password: str) -> Optional[dict]:
    row = get_user_by_email(email)
    if not row: return None
    uid, em, name, pw, role = row
    return {"id": uid, "email": em, "name": name, "role": role} if password == pw else None

# -------- queries --------
def save_query(user_id: Optional[int], topic: str, filters: Dict[str, Any]) -> None:
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE;")
        con.execute("INSERT INTO queries(user_id,topic,filters_json) VALUES (?,?,?)",
                    (user_id, topic, json.dumps(filters)))
        con.commit()
    except Exception:
        con.rollback(); raise
    finally:
        con.close()

# -------- programs --------
def _ensure_source_within(con: sqlite3.Connection, program_url: str) -> int:
    domain = _domain_of(program_url)
    con.execute("INSERT OR IGNORE INTO sources(url) VALUES (?)", (domain,))
    src = con.execute("SELECT id FROM sources WHERE url=?", (domain,)).fetchone()
    return src[0]

def save_program_rows(rows: List[Dict[str, Any]]) -> None:
    if not rows: return
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE;")
        for p in rows:
            program_url = p.get("url") or ""
            if not program_url: continue
            source_id = _ensure_source_within(con, program_url)
            con.execute("""
                INSERT OR IGNORE INTO programs
                (source_id,url,title,description,price,currency,price_usd_real,
                 start_date,end_date,mode,venue,city,country,type,is_approved)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, ?, 1)
            """, (
                source_id, program_url,
                p.get("title"), p.get("description"),
                p.get("price"), p.get("currency"), p.get("price_usd"),
                p.get("start_date"), p.get("end_date"),
                p.get("mode"), p.get("venue"),
                p.get("city"), p.get("country"),
                p.get("type")
            ))
        con.commit()
    except Exception:
        con.rollback(); raise
    finally:
        con.close()

def list_programs(filters: Dict[str, Any]) -> List[Tuple]:
    q = "SELECT id,title,type,mode,country,city,price,currency,url FROM programs WHERE 1=1"
    args: List[Any] = []
    if filters.get("type") and filters["type"] != "Any": q += " AND type=?"; args.append(filters["type"])
    if filters.get("mode") and filters["mode"] != "Any": q += " AND mode=?"; args.append(filters["mode"])
    if filters.get("cost") and filters["cost"] != "Any":
        if filters["cost"] == "Free": q += " AND (price IS NULL OR price=0)"
        else: q += " AND (price IS NOT NULL AND price>0)"
    if filters.get("country_contains"): q += " AND LOWER(country) LIKE ?"; args.append(f"%{filters['country_contains'].lower()}%")
    if filters.get("city_contains"):    q += " AND LOWER(city) LIKE ?";    args.append(f"%{filters['city_contains'].lower()}%")
    con = _connect()
    rows = con.execute(q, args).fetchall()
    con.close()
    return rows

def get_program_detail(pid: int) -> Optional[Tuple]:
    con = _connect()
    row = con.execute("""
        SELECT id,url,title,description,price,currency,price_usd_real,
               start_date,end_date,mode,venue,city,country,type,is_approved,created_at
        FROM programs WHERE id=?
    """, (pid,)).fetchone()
    con.close()
    return row

def quick_stats() -> Optional[Tuple[int,int,int]]:
    con = _connect()
    stats = con.execute("""
        SELECT
          (SELECT COUNT(*) FROM programs),
          (SELECT COUNT(*) FROM programs WHERE is_approved=1),
          (SELECT COUNT(*) FROM sources)
    """).fetchone()
    con.close()
    return stats

def toggle_program_approved(pid: int) -> None:
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE;")
        con.execute("UPDATE programs SET is_approved = 1 - is_approved WHERE id=?", (pid,))
        con.commit()
    except Exception:
        con.rollback(); raise
    finally:
        con.close()

# -------- back-compat shims --------
def save_result(query: str, title: str, link: str, content: str) -> None:
    save_program_rows([{
        "url": link, "title": title or "Program", "description": content or "",
        "price": None, "currency": None, "price_usd": None,
        "start_date": None, "end_date": None,
        "mode": "Unknown", "venue": None, "city": None, "country": None, "type": "Other"
    }])

def get_results() -> List[Tuple]:
    con = _connect()
    rows = con.execute("""
        SELECT id, '' as query, COALESCE(title,'Program'), COALESCE(url,''), COALESCE(description,'')
        FROM programs ORDER BY id DESC
    """).fetchall()
    con.close()
    return rows
