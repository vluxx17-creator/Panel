"""Слой доступа к SQLite."""
import os
from datetime import datetime, timedelta, timezone

import aiosqlite

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    nickname       TEXT,
    photo_url      TEXT,
    wallet         TEXT,
    role           TEXT    DEFAULT 'Модератор',
    registered_at  TEXT,
    is_banned      INTEGER DEFAULT 0,
    captcha_passed INTEGER DEFAULT 0,
    balance        REAL    DEFAULT 0,
    streak         INTEGER DEFAULT 0,
    referrals      INTEGER DEFAULT 0,
    referred_by    INTEGER,
    join_status    TEXT    DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS payouts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL,
    code              TEXT,
    link              TEXT,
    shot_file_id      TEXT,
    wallet            TEXT,
    amount            REAL    DEFAULT 0,
    status            TEXT    DEFAULT 'pending',
    reason            TEXT,
    requested_at      TEXT,
    accepted_at       TEXT,
    admin_id          INTEGER,
    admin_message_id  INTEGER
);

CREATE TABLE IF NOT EXISTS tasks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    title    TEXT,
    icon     TEXT,
    price    REAL DEFAULT 0,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    text     TEXT,
    sent_at  TEXT
);

CREATE TABLE IF NOT EXISTS captcha_sessions (
    user_id    INTEGER PRIMARY KEY,
    answer     INTEGER,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS promos (
    code       TEXT PRIMARY KEY,
    bonus      REAL DEFAULT 0,
    uses_left  INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_payouts_user ON payouts(user_id);
CREATE INDEX IF NOT EXISTS idx_payouts_status ON payouts(status);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def connect() -> aiosqlite.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.executescript(SCHEMA)
    await conn.commit()
    return conn


async def upsert_user(conn, user: dict) -> None:
    await conn.execute(
        """INSERT INTO users (user_id, username, first_name, photo_url, registered_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             username = excluded.username,
             first_name = excluded.first_name,
             photo_url = COALESCE(excluded.photo_url, users.photo_url)""",
        (user["id"], user.get("username"), user.get("first_name"),
         user.get("photo_url"), now_iso()),
    )
    await conn.commit()


async def get_user(conn, user_id: int):
    cur = await conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return await cur.fetchone()


async def set_fields(conn, user_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    await conn.execute(f"UPDATE users SET {cols} WHERE user_id = ?",
                       (*fields.values(), user_id))
    await conn.commit()


async def add_balance(conn, user_id: int, delta: float) -> None:
    await conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?",
                       (delta, user_id))
    await conn.commit()


async def create_payout(conn, user_id: int, code: str, link: str,
                        wallet: str, shot_file_id: str | None) -> int:
    cur = await conn.execute(
        """INSERT INTO payouts (user_id, code, link, wallet, shot_file_id, requested_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, code, link, wallet, shot_file_id, now_iso()),
    )
    await conn.commit()
    return cur.lastrowid


async def get_payout(conn, payout_id: int):
    cur = await conn.execute("SELECT * FROM payouts WHERE id = ?", (payout_id,))
    return await cur.fetchone()


async def list_payouts(conn, status: str | None = None, user_id: int | None = None):
    sql = "SELECT * FROM payouts"
    where, args = [], []
    if status:
        where.append("status = ?")
        args.append(status)
    if user_id:
        where.append("user_id = ?")
        args.append(user_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT 200"
    cur = await conn.execute(sql, args)
    return await cur.fetchall()


async def set_payout_status(conn, payout_id: int, status: str, *,
                            amount: float | None = None, admin_id: int | None = None,
                            reason: str | None = None) -> None:
    await conn.execute(
        """UPDATE payouts SET status = ?,
             amount = COALESCE(?, amount),
             admin_id = COALESCE(?, admin_id),
             reason = COALESCE(?, reason),
             accepted_at = ?
           WHERE id = ?""",
        (status, amount, admin_id, reason, now_iso(), payout_id),
    )
    await conn.commit()


async def user_tasks(conn, user_id: int):
    cur = await conn.execute(
        "SELECT title, icon, price FROM tasks WHERE user_id = ? ORDER BY id DESC", (user_id,))
    return await cur.fetchall()


async def leaderboard(conn, period: str = "all"):
    since = None
    if period in ("day", "week", "month"):
        days = {"day": 1, "week": 7, "month": 30}[period]
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    sql = """SELECT u.user_id, COALESCE(u.nickname, u.first_name) AS name, u.photo_url,
                    COUNT(p.id) AS payouts, COALESCE(SUM(p.amount), 0) AS amount
             FROM users u
             LEFT JOIN payouts p ON p.user_id = u.user_id AND p.status = 'accepted'
             {flt}
             GROUP BY u.user_id ORDER BY amount DESC, payouts DESC LIMIT 50"""
    flt = "AND p.accepted_at >= ?" if since else ""
    cur = await conn.execute(sql.format(flt=flt), (since,) if since else ())
    return await cur.fetchall()


async def place_of(conn, user_id: int) -> int | str:
    rows = await leaderboard(conn)
    for i, row in enumerate(rows, start=1):
        if row["user_id"] == user_id:
            return i
    return "—"


async def monthly_chart(conn, user_id: int) -> list[float]:
    cur = await conn.execute(
        """SELECT accepted_at, amount FROM payouts
           WHERE user_id = ? AND status = 'accepted' AND accepted_at IS NOT NULL""",
        (user_id,),
    )
    bars = [0.0] * 30
    today = datetime.now(timezone.utc).date()
    for row in await cur.fetchall():
        try:
            day = datetime.fromisoformat(row["accepted_at"]).date()
        except (TypeError, ValueError):
            continue
        idx = 29 - (today - day).days
        if 0 <= idx < 30:
            bars[idx] += float(row["amount"] or 0)
    return bars


async def chat_messages(conn, limit: int = 50):
    cur = await conn.execute(
        """SELECT m.text, m.sent_at, COALESCE(u.nickname, u.first_name) AS name, u.photo_url
           FROM chat_messages m JOIN users u ON u.user_id = m.user_id
           ORDER BY m.id DESC LIMIT ?""", (limit,))
    return list(reversed(await cur.fetchall()))


async def add_chat_message(conn, user_id: int, text: str) -> None:
    await conn.execute(
        "INSERT INTO chat_messages (user_id, text, sent_at) VALUES (?, ?, ?)",
        (user_id, text, now_iso()))
    await conn.commit()


async def use_promo(conn, code: str) -> float | None:
    cur = await conn.execute("SELECT bonus, uses_left FROM promos WHERE code = ?", (code,))
    row = await cur.fetchone()
    if not row or row["uses_left"] <= 0:
        return None
    await conn.execute("UPDATE promos SET uses_left = uses_left - 1 WHERE code = ?", (code,))
    await conn.commit()
    return float(row["bonus"])


async def all_user_ids(conn) -> list[int]:
    cur = await conn.execute("SELECT user_id FROM users WHERE is_banned = 0")
    return [row["user_id"] for row in await cur.fetchall()]
