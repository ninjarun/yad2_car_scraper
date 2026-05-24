# db.py
import sqlite3
import json
import time
from contextlib import contextmanager
from typing import Dict, Iterable, Tuple, List, Optional
import time
import sqlite3
from typing import List, Dict


DB_PATH = "yad2_cars.db"

@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def init_db():
    with conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            url TEXT PRIMARY KEY,
            brand TEXT,
            model TEXT,
            title TEXT,
            price TEXT,
            year TEXT,
            hands TEXT,
            km TEXT,
            fields TEXT,          -- JSON string
            description TEXT,
            location TEXT,
            ad_created_at TEXT,
            seller_type TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT
        );
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_brand_model ON listings(brand, model);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_last_seen ON listings(last_seen_at);")

def get_listing(url: str) -> Optional[Dict]:
    with conn() as c:
        row = c.execute("SELECT * FROM listings WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else None

def touch_listing(url: str, brand: Optional[str] = None, model: Optional[str] = None) -> None:
    """Ensure a row exists for URL; update last_seen_at and optionally brand/model."""
    now = _now_iso()
    with conn() as c:
        exist = c.execute("SELECT url FROM listings WHERE url = ?", (url,)).fetchone()
        if exist:
            c.execute("""
                UPDATE listings
                   SET last_seen_at = ?,
                       brand = COALESCE(?, brand),
                       model = COALESCE(?, model)
                 WHERE url = ?;
            """, (now, brand, model, url))
        else:
            c.execute("""
                INSERT INTO listings(url, brand, model, first_seen_at, last_seen_at)
                VALUES(?, ?, ?, ?, ?);
            """, (url, brand, model, now, now))



def bulk_upsert_listings(rows: List[Dict]) -> int:
    """
    Insert / update many listings in one transaction.
    Returns number of rows processed.
    """
    if not rows:
        return 0

    now = _now_iso()
    prepared = []
    for row in rows:
        data = dict(row)
        data.setdefault("url", "")
        data.setdefault("brand", "")
        data.setdefault("model", "")
        data.setdefault("title", "")
        data.setdefault("price", "")
        data.setdefault("year", "")
        data.setdefault("hands", "")
        data.setdefault("km", "")
        fields = data.get("fields")
        if not isinstance(fields, str):
            data["fields"] = json.dumps(fields or {}, ensure_ascii=False)
        data.setdefault("description", "")
        data.setdefault("last_seen_at", data.get("last_seen_at") or now)
        data.setdefault("location", "")
        data.setdefault("ad_created_at", "")
        data.setdefault("seller_type", "")
        data.setdefault("first_seen_at", data.get("first_seen_at") or now)

        prepared.append((
            data["url"], data["brand"], data["model"], data["title"], data["price"],
            data["year"], data["hands"], data["km"], data["fields"], data["description"],
            data["ad_created_at"], data["seller_type"], data["first_seen_at"],
            data["last_seen_at"], data["location"],
        ))

    sql = """
    INSERT INTO listings(
        url, brand, model, title, price, year, hands, km,
        fields, description, ad_created_at, seller_type,
        first_seen_at, last_seen_at, location
    )
    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(url) DO UPDATE SET
        brand         = excluded.brand,
        model         = excluded.model,
        title         = excluded.title,
        price         = excluded.price,
        year          = excluded.year,
        hands         = excluded.hands,
        km            = excluded.km,
        fields        = excluded.fields,
        description   = excluded.description,
        ad_created_at = excluded.ad_created_at,
        seller_type   = excluded.seller_type,
        location      = excluded.location,
        last_seen_at  = excluded.last_seen_at
    ;
    """

    with conn() as c:
        c.executemany(sql, prepared)
    return len(prepared)


def upsert_listing(row: Dict) -> None:
    """Insert or update a full listing row."""
    now = _now_iso()
    data = dict(row)
    # ensure required keys exist
    data.setdefault("url", "")
    data.setdefault("brand", "")
    data.setdefault("model", "")
    data.setdefault("title", "")
    data.setdefault("price", "")
    data.setdefault("year", "")
    data.setdefault("hands", "")
    data.setdefault("km", "")
    fields = data.get("fields")
    if not isinstance(fields, str):
        data["fields"] = json.dumps(fields or {}, ensure_ascii=False)
    data.setdefault("description", "")
    data.setdefault("last_seen_at", now)
    data.setdefault("location", "") 
    data.setdefault("ad_created_at", "")
    data.setdefault("seller_type", "")  

    with conn() as c:
        c.execute("""
        INSERT INTO listings(
            url, brand, model, title, price, year, hands, km,
            fields, description, ad_created_at, seller_type,
            first_seen_at, last_seen_at, location
        )
        VALUES(
            :url, :brand, :model, :title, :price, :year, :hands, :km,
            :fields, :description, :ad_created_at, :seller_type,
            :first_seen_at, :last_seen_at, :location
        )
        ON CONFLICT(url) DO UPDATE SET
            brand         = excluded.brand,
            model         = excluded.model,
            title         = excluded.title,
            price         = excluded.price,
            year          = excluded.year,
            hands         = excluded.hands,
            km            = excluded.km,
            fields        = excluded.fields,
            description   = excluded.description,
            ad_created_at = excluded.ad_created_at,
            seller_type   = excluded.seller_type,
            location      = excluded.location,
            last_seen_at  = excluded.last_seen_at,
            first_seen_at = COALESCE(listings.first_seen_at, excluded.first_seen_at);
        """, {
            **data,
            "first_seen_at": row.get("first_seen_at") or now,
        })


def update_price_and_touch(url: str, new_price: str, brand: Optional[str], model: Optional[str]) -> None:
    now = _now_iso()
    with conn() as c:
        row = c.execute("SELECT url FROM listings WHERE url = ?", (url,)).fetchone()
        if row:
            c.execute("""
                UPDATE listings
                   SET price = ?, last_seen_at = ?,
                       brand = COALESCE(?, brand),
                       model = COALESCE(?, model)
                 WHERE url = ?;
            """, (new_price, now, brand, model, url))
        else:
            c.execute("""
                INSERT INTO listings(url, brand, model, price, first_seen_at, last_seen_at)
                VALUES(?, ?, ?, ?, ?, ?);
            """, (url, brand, model, new_price, now, now))

def delete_urls(urls: Iterable[str]) -> None:
    urls = list({u for u in urls if u})
    if not urls:
        return
    placeholders = ",".join("?" for _ in urls)
    with conn() as c:
        c.execute(f"DELETE FROM listings WHERE url IN ({placeholders});", urls)

def cleanup_deleted_listings(seen_by_pair: Dict[Tuple[str, str], Iterable[str]]) -> None:
    """
    For each (brand, model) cohort scraped this run, remove rows from the same cohort
    that were NOT seen in this run (scoped cleanup).
    """
    with conn() as c:
        for (brand, model), seen_urls in seen_by_pair.items():
            b = brand or "Unknown"
            if b == "Unknown":
                continue
            seen = set(u for u in seen_urls if u)
            rows = c.execute("SELECT url FROM listings WHERE brand = ? AND model = ?;", (brand, model)).fetchall()
            cohort = {r["url"] for r in rows}
            missing = list(cohort - seen)
            if not missing:
                continue
            ph = ",".join("?" for _ in missing)
            c.execute(f"DELETE FROM listings WHERE url IN ({ph});", missing)

def export_csv(path: str) -> None:
    import csv
    with conn() as c:
        rows = c.execute("SELECT * FROM listings ORDER BY last_seen_at DESC;").fetchall()
        if not rows:
            open(path, "w", encoding="utf-8").close()
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            for r in rows:
                w.writerow(dict(r))

def query_selection(brand_names: List[str], model_name_substrings: List[str], limit: int = 200) -> List[Dict]:
    """
    brand_names: exact brand labels (case-insensitive)
    model_name_substrings: substring matches (case-insensitive) in model OR title
    """
    sql = "SELECT * FROM listings WHERE 1=1"
    params: List[str] = []

    if brand_names:
        lowers = [b.lower() for b in brand_names]
        ph = ",".join("?" for _ in lowers)
        sql += f" AND LOWER(brand) IN ({ph})"
        params.extend(lowers)

    if model_name_substrings:
        parts = []
        for _ in model_name_substrings:
            parts.append("(LOWER(model) LIKE ? OR LOWER(title) LIKE ?)")
        sql += " AND (" + " OR ".join(parts) + ")"
        for s in model_name_substrings:
            like = f"%{s.lower()}%"
            params.extend([like, like])

    sql += " ORDER BY last_seen_at DESC LIMIT ?"
    params.append(int(limit))

    with conn() as c:
        rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    

    
# --- NEW: user + subscription storage and change tracking ---
import sqlite3
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

def build_subscriptions_keyboard(sub_rows: list[tuple[int,str,str]]) -> InlineKeyboardMarkup:
    """
    sub_rows = [(rowid, brand, model), ...]
    עבור כל מנוי נשים כפתור להסרה.
    בסוף נוסיף כפתור 'מחק הכל' אם יש בכלל מנויים.
    """
    rows = []

    for rowid, brand, model in sub_rows:
        # כיתוב יפה:
        if model and model.strip():
            label = f"{brand} — {model}"
        else:
            label = f"{brand} — כל הדגמים"

        rows.append([
            InlineKeyboardButton(
                f"❌ הסר {label}",
                callback_data=f"unsubscribe_one:{rowid}"
            )
        ])

    if sub_rows:
        rows.append([
            InlineKeyboardButton(
                "🗑 מחק את כל המנויים שלי",
                callback_data="unsubscribe_all"
            )
        ])

    return InlineKeyboardMarkup(rows)


DB_PATH = "yad2_cars.db"  # make sure this matches your code

def _db():
    return sqlite3.connect(DB_PATH)

def get_user_subscriptions(telegram_id: int) -> list[tuple[int, str, str]]:
    """
    מחזיר רשימת מנויים של המשתמש.
    כל שורה: (row_id, brand, model)
    """
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT rowid, brand, model
        FROM subscriptions
        WHERE telegram_id = ?
        ORDER BY brand, model
        """,
        (telegram_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def delete_subscription_row(row_id: int, telegram_id: int) -> None:
    """
    מוחק מנוי יחיד לפי rowid, אבל רק אם הוא שייך למשתמש הזה.
    """
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM subscriptions
        WHERE rowid = ?
          AND telegram_id = ?
        """,
        (row_id, telegram_id),
    )
    conn.commit()
    conn.close()

def delete_all_subscriptions(telegram_id: int) -> None:
    """
    מוחק את כל המנויים של המשתמש.
    """
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM subscriptions
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    )
    conn.commit()
    conn.close()

def init_subscriptions():
    with conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS tg_users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            brand TEXT NOT NULL,
            model TEXT NOT NULL,                 -- model name; may be "" to mean "all models for brand"
            created_at TEXT,
            last_checked_at TEXT,
            UNIQUE(telegram_id, brand, model)
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS subscription_items (
            telegram_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            last_price TEXT,
            last_seen_at TEXT,
            PRIMARY KEY (telegram_id, url)
        );
        """)

def upsert_user(telegram_id: int, username: str|None = None):
    now = _now_iso()
    with conn() as c:
        r = c.execute("SELECT telegram_id FROM tg_users WHERE telegram_id=?", (telegram_id,)).fetchone()
        if r:
            c.execute("UPDATE tg_users SET username=COALESCE(?, username), last_seen_at=? WHERE telegram_id=?",
                      (username, now, telegram_id))
        else:
            c.execute("INSERT INTO tg_users(telegram_id, username, first_seen_at, last_seen_at) VALUES(?,?,?,?)",
                      (telegram_id, username, now, now))

def add_subscription(telegram_id: int, brand: str, model: str):
    now = _now_iso()
    with conn() as c:
        c.execute("""
        INSERT INTO subscriptions(telegram_id, brand, model, created_at, last_checked_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(telegram_id, brand, model) DO UPDATE SET last_checked_at=excluded.last_checked_at
        """, (telegram_id, brand, model, now, None))

def list_subscriptions() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM subscriptions").fetchall()
        return [dict(r) for r in rows]

def list_user_subscriptions(telegram_id: int) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM subscriptions WHERE telegram_id=?", (telegram_id,)).fetchall()
        return [dict(r) for r in rows]

def update_subscription_checked(telegram_id: int, brand: str, model: str):
    now = _now_iso()
    with conn() as c:
        c.execute("UPDATE subscriptions SET last_checked_at=? WHERE telegram_id=? AND brand=? AND model=?",
                  (now, telegram_id, brand, model))

def diff_and_update_items(telegram_id: int, rows: list[dict]) -> tuple[list[dict], list[tuple[dict, str, str]]]:
    """
    rows: latest rows from `listings` for the user's selection.
    Returns:
       new_listings: [row,...] where url not tracked before
       price_changes: [(row, old_price, new_price), ...]
    Also updates subscription_items to current prices.
    """
    new_listings = []
    price_changes = []
    now = _now_iso()

    with conn() as c:
        for r in rows:
            url = (r.get("url") or "").strip()
            price = (r.get("price") or "").strip()
            seen = c.execute("SELECT last_price FROM subscription_items WHERE telegram_id=? AND url=?",
                             (telegram_id, url)).fetchone()
            if not seen:
                new_listings.append(r)
                c.execute("INSERT INTO subscription_items(telegram_id, url, last_price, last_seen_at) VALUES(?,?,?,?)",
                          (telegram_id, url, price, now))
            else:
                old_price = (seen["last_price"] or "").strip()
                if price and old_price and "".join(ch for ch in price if ch.isdigit()) != "".join(ch for ch in old_price if ch.isdigit()):
                    price_changes.append((r, old_price, price))
                c.execute("UPDATE subscription_items SET last_price=?, last_seen_at=? WHERE telegram_id=? AND url=?",
                          (price, now, telegram_id, url))
    return new_listings, price_changes

# --- Job de-dup (same user+selection within N minutes) ---
def init_job_runs():
    with conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER NOT NULL,
            brand_key   TEXT NOT NULL,
            model_key   TEXT NOT NULL,
            started_at  INTEGER NOT NULL
        );
        """)
        c.execute("""
        CREATE INDEX IF NOT EXISTS idx_job_runs_key_time
        ON job_runs(telegram_id, brand_key, model_key, started_at);
        """)

def recent_job_exists(telegram_id: int, brand_key: str, model_key: str, window_minutes: int = 20) -> bool:
    cutoff = int(time.time()) - window_minutes * 60
    with conn() as c:
        row = c.execute("""
            SELECT 1
            FROM job_runs
            WHERE telegram_id=? AND brand_key=? AND model_key=? AND started_at>=?
            LIMIT 1
        """, (telegram_id, brand_key, model_key, cutoff)).fetchone()
        return row is not None

def record_job_run(telegram_id: int, brand_key: str, model_key: str) -> None:
    with conn() as c:
        c.execute("""
            INSERT INTO job_runs (telegram_id, brand_key, model_key, started_at)
            VALUES (?, ?, ?, ?)
        """, (telegram_id, brand_key, model_key, int(time.time())))

def cleanup_deleted_listings_by_age(cutoff_days: int = 10) -> int:
    """
    Delete listings whose last_seen_at is older than NOW - cutoff_days.
    Works for ISO-8601 timestamps (e.g., '2025-11-05T13:05:00Z').
    Returns number of rows deleted.
    """
    conn = sqlite3.connect("yad2_cars.db")
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        # use julianday math (SQLite built-in)
        cur.execute("""
            DELETE FROM listings
            WHERE julianday(last_seen_at) < julianday('now', ?)
        """, (f'-{cutoff_days} days',))
        deleted = cur.rowcount if cur.rowcount is not None else 0
        conn.commit()
        return deleted
    finally:
        conn.close()

# --- ADMIN: wipe all subscriptions for ALL users (and per-item cache) ---
def reset_all_subscriptions() -> tuple[int, int]:
    """
    Deletes ALL rows in subscriptions and subscription_items.
    Returns (subs_deleted, items_deleted).
    """
    with conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) FROM subscriptions;")
        subs_before = cur.fetchone()[0] or 0
        cur.execute("DELETE FROM subscriptions;")

        cur.execute("SELECT COUNT(*) FROM subscription_items;")
        items_before = cur.fetchone()[0] or 0
        cur.execute("DELETE FROM subscription_items;")

        return subs_before, items_before

############################################################################
# ===== PLANS (Stripe/Telegram-payments) =====
############################################################################
def init_plans():
    with conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            telegram_id INTEGER PRIMARY KEY,
            plan_name   TEXT NOT NULL,
            expires_at  TEXT,                 -- ISO8601; NULL = no expiry (lifetime/admin)
            created_at  TEXT,
            updated_at  TEXT
        );
        """)
            # --- Safe schema upgrades for existing DBs (SQLite allows ADD COLUMN) ---
        try:
            c.execute("ALTER TABLE plans ADD COLUMN free_started_at TEXT;")
        except Exception:
            pass  # already exists

        try:
            c.execute("ALTER TABLE plans ADD COLUMN last_free_reset_at TEXT;")
        except Exception:
            pass  # already exists


def upsert_plan(telegram_id: int, plan_name: str, expires_at: str | None):
    now = _now_iso()
    with conn() as c:
        row = c.execute("SELECT telegram_id FROM plans WHERE telegram_id=?", (telegram_id,)).fetchone()
        if row:
            c.execute("""UPDATE plans
                         SET plan_name=?, expires_at=?, updated_at=?
                         WHERE telegram_id=?""",
                      (plan_name, expires_at, now, telegram_id))
        else:
            c.execute("""INSERT INTO plans(telegram_id, plan_name, expires_at, created_at, updated_at)
                         VALUES(?,?,?,?,?)""",
                      (telegram_id, plan_name, expires_at, now, now))

def get_plan(telegram_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM plans WHERE telegram_id=?", (telegram_id,)).fetchone()
        return dict(r) if r else None

def expire_if_needed(telegram_id: int) -> None:
    """If plan.expires_at is past, downgrade to 'free'."""
    with conn() as c:
        r = c.execute("SELECT plan_name, expires_at FROM plans WHERE telegram_id=?", (telegram_id,)).fetchone()
        if not r:  # no plan: treat as free
            return
        exp = r["expires_at"]
        if exp and exp < _now_iso():
            c.execute("""UPDATE plans SET plan_name='free', expires_at=NULL, updated_at=? WHERE telegram_id=?""",
                      (_now_iso(), telegram_id))
            
# Hard limits per plan (edit anytime)
PLAN_LIMITS = {
    "free": 2,           # 10-day free trial
    "free_expired": 0,   # trial over – no new subs
    "starter": 10,
    "pro": 20,
    "dealer": 50,
    "admin": 999,
}

def get_plan_limit(telegram_id: int) -> tuple[str, int]:
    """
    Return (plan_name, max_subscriptions).

    Behaviour:
    - Paid plans: honour expires_at (downgrade to free when passed).
    - Free plan:
        * When the user first appears, we create a 'free' row with free_started_at = now.
        * For 10 days from that timestamp they have a normal free plan.
        * After 10 days we flip them to 'free_expired' (0 subs).
    """
    # 1) Downgrade paid plans if needed
    expire_if_needed(telegram_id)

    # 2) Ensure there is a row & free_started_at for free users
    ensure_free_row(telegram_id)

    # 3) Read the current plan row
    p = get_plan(telegram_id) or {}
    plan = (p.get("plan_name") or "free").lower()

    # 4) Enforce 10-day window for free plan
    if plan == "free":
        free_start = p.get("free_started_at")
        if free_start:
            days = _days_between_iso(_iso_now(), free_start)
            if days >= 10.0:
                # Flip DB row to free_expired once
                with conn() as c:
                    c.execute(
                        "UPDATE plans SET plan_name='free_expired', updated_at=? WHERE telegram_id=?",
                        (_iso_now(), telegram_id),
                    )
                plan = "free_expired"

    return plan, PLAN_LIMITS.get(plan, 2)





def set_user_plan(telegram_id: int, plan_name: str, months: int | None):
    """
    Admin override: set a user's plan to <plan_name> for <months>.
    months=None or months>=9999 => lifetime (no expiry).
    """
    # Lifetime / no expiry
    if months is None or months >= 9999:
        expires_at = None
    else:
        # Use SQLite to compute "now + X months"
        with conn() as c:
            row = c.execute(
                "SELECT datetime('now', ?)",
                (f'+{months} months',)
            ).fetchone()
            expires_at = row[0] if row and row[0] else None

    # Reuse existing plan upsert logic
    upsert_plan(telegram_id, plan_name.lower(), expires_at)


#########
# Free trial expiry enforcement
#########
def _iso_now() -> str:
    return _now_iso()

def _days_between_iso(a: str, b: str) -> float:
    # Very small parser using sqlite julianday via a temp conn for reliability
    # (avoids timezone headaches). Returns abs(days).
    with conn() as c:
        row = c.execute("SELECT ABS(julianday(?) - julianday(?))", (a, b)).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

def ensure_free_row(telegram_id: int):
    """
    Ensure a free plan row exists.
    Also set free_started_at the first time we ever create the row.
    """
    with conn() as c:
        row = c.execute(
            "SELECT plan_name, free_started_at FROM plans WHERE telegram_id = ?",
            (telegram_id,)
        ).fetchone()

        # Already exists
        if row:
            return

        # Create first-time free plan entry
        now = _now_iso()
        c.execute("""
            INSERT INTO plans(telegram_id, plan_name, expires_at, free_started_at)
            VALUES(?, 'free', NULL, ?)
        """, (telegram_id, now))


def mark_free_reset(telegram_id: int):
    """Mark that we reset the user's free subs now, and restart their 10-day window."""
    now = _iso_now()
    with conn() as c:
        c.execute("""UPDATE plans
                     SET last_free_reset_at=?, free_started_at=?, updated_at=?
                     WHERE telegram_id=?""",
                  (now, now, now, telegram_id))

def is_free_plan(telegram_id: int) -> bool:
    p = get_plan(telegram_id)
    return (p is None) or ((p.get("plan_name") or "free").lower() == "free")

def free_reset_if_due(telegram_id: int, window_days: int = 10) -> int:
    """
    If user is on free plan and their free window exceeded `window_days`,
    delete all subscriptions and 'restart the window'.
    Returns count of deleted subscriptions (0 if none).
    """
    if not is_free_plan(telegram_id):
        return 0

    ensure_free_row(telegram_id)

    with conn() as c:
        r = c.execute("""SELECT free_started_at, last_free_reset_at
                         FROM plans WHERE telegram_id=?""", (telegram_id,)).fetchone()

    now = _iso_now()
    free_start = r["free_started_at"] or now
    days = _days_between_iso(now, free_start)

    if days >= float(window_days):
        # wipe user's subs
        delete_all_subscriptions(telegram_id)
        # mark the reset and restart the window
        mark_free_reset(telegram_id)
        return 1  # semantic: 'we performed a reset'
    return 0

def list_free_trial_expired_users(days: int = 10) -> list[tuple[int, str]]:
    """
    Return [(telegram_id, free_started_at_iso), ...] for users on the 'free' plan
    whose free window started >= `days` days ago.
    """
    with conn() as c:
        rows = c.execute(
            """
            SELECT telegram_id, free_started_at
            FROM plans
            WHERE LOWER(COALESCE(plan_name, 'free')) = 'free'
              AND free_started_at IS NOT NULL
              AND julianday('now') - julianday(free_started_at) >= ?
            """,
            (float(days),),
        ).fetchall()
        return [(r["telegram_id"], r["free_started_at"]) for r in rows]

def reset_user_subscriptions(telegram_id: int) -> int:
    """
    Delete all subscriptions (and per-item cache) for a user,
    mark the FREE window reset so a new 10-day window starts,
    and return how many subscriptions were deleted.
    """
    with conn() as c:
        # count first
        row = c.execute("SELECT COUNT(*) AS n FROM subscriptions WHERE telegram_id=?", (telegram_id,)).fetchone()
        n = int(row["n"] or 0)

        # wipe subs + per-item cache
        c.execute("DELETE FROM subscriptions WHERE telegram_id=?", (telegram_id,))
        c.execute("DELETE FROM subscription_items WHERE telegram_id=?", (telegram_id,))

    # restart the user's free window
    try:
        mark_free_reset(telegram_id)
    except Exception:
        pass

    return n

##########
#end free trial expiry enforcement
###########


############################################################################
# ===== END PLANS =====
############################################################################