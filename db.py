"""SQLite storage for Texas scratch-off snapshots."""

import os
import sqlite3

DB_PATH = os.environ.get("TXSCRATCH_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scratch.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_number   INTEGER PRIMARY KEY,
    game_name     TEXT,
    ticket_price  REAL,
    start_date    TEXT,
    close_date    TEXT,
    total_tickets INTEGER,
    overall_odds  REAL,
    details_url   TEXT,
    closing_soon  INTEGER DEFAULT 0,
    active        INTEGER DEFAULT 1,
    first_seen    TEXT,
    last_seen     TEXT
);

-- One row per game per prize tier per day. prize_level -1 holds the TOTAL row.
CREATE TABLE IF NOT EXISTS prize_snapshots (
    snapshot_date  TEXT NOT NULL,
    game_number    INTEGER NOT NULL,
    prize_level    REAL NOT NULL,
    total_prizes   INTEGER NOT NULL,
    prizes_claimed INTEGER NOT NULL,
    PRIMARY KEY (snapshot_date, game_number, prize_level)
);

CREATE INDEX IF NOT EXISTS idx_snap_game ON prize_snapshots (game_number, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_snap_date ON prize_snapshots (snapshot_date);

CREATE TABLE IF NOT EXISTS refresh_log (
    run_at        TEXT,
    snapshot_date TEXT,
    rows_written  INTEGER,
    games_seen    INTEGER,
    note          TEXT
);
"""


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_game(conn, g, today):
    """g: dict with game_number and any subset of the other columns."""
    existing = conn.execute(
        "SELECT * FROM games WHERE game_number = ?", (g["game_number"],)
    ).fetchone()

    if existing is None:
        conn.execute(
            """INSERT INTO games (game_number, game_name, ticket_price, start_date,
                   close_date, total_tickets, overall_odds, details_url, closing_soon,
                   active, first_seen, last_seen)
               VALUES (:game_number, :game_name, :ticket_price, :start_date, :close_date,
                   :total_tickets, :overall_odds, :details_url, :closing_soon, :active,
                   :first_seen, :last_seen)""",
            {
                "game_number": g["game_number"],
                "game_name": g.get("game_name"),
                "ticket_price": g.get("ticket_price"),
                "start_date": g.get("start_date"),
                "close_date": g.get("close_date"),
                "total_tickets": g.get("total_tickets"),
                "overall_odds": g.get("overall_odds"),
                "details_url": g.get("details_url"),
                "closing_soon": int(g.get("closing_soon") or 0),
                "active": int(g.get("active", 1)),
                "first_seen": today,
                "last_seen": today,
            },
        )
        return

    # Update only the fields we actually have. Never blank out ticket counts
    # we already scraped, since detail pages are the slow part.
    fields, params = [], {}
    for col in ("game_name", "ticket_price", "start_date", "close_date",
                "total_tickets", "overall_odds", "details_url", "closing_soon", "active"):
        if col in g and g[col] is not None:
            fields.append(f"{col} = :{col}")
            params[col] = g[col]
    fields.append("last_seen = :last_seen")
    params["last_seen"] = today
    params["game_number"] = g["game_number"]
    conn.execute(f"UPDATE games SET {', '.join(fields)} WHERE game_number = :game_number", params)


def write_snapshot(conn, snapshot_date, rows):
    """rows: list of (game_number, prize_level, total_prizes, prizes_claimed)."""
    conn.execute("DELETE FROM prize_snapshots WHERE snapshot_date = ?", (snapshot_date,))
    conn.executemany(
        """INSERT INTO prize_snapshots
           (snapshot_date, game_number, prize_level, total_prizes, prizes_claimed)
           VALUES (?, ?, ?, ?, ?)""",
        [(snapshot_date, *r) for r in rows],
    )
    return len(rows)


def log_refresh(conn, run_at, snapshot_date, rows_written, games_seen, note=""):
    conn.execute(
        "INSERT INTO refresh_log (run_at, snapshot_date, rows_written, games_seen, note) VALUES (?,?,?,?,?)",
        (run_at, snapshot_date, rows_written, games_seen, note),
    )


def latest_snapshot_date(conn):
    row = conn.execute("SELECT MAX(snapshot_date) AS d FROM prize_snapshots").fetchone()
    return row["d"] if row else None


def snapshot_dates(conn, game_number=None):
    if game_number is None:
        q = "SELECT DISTINCT snapshot_date FROM prize_snapshots ORDER BY snapshot_date"
        return [r[0] for r in conn.execute(q)]
    q = "SELECT DISTINCT snapshot_date FROM prize_snapshots WHERE game_number = ? ORDER BY snapshot_date"
    return [r[0] for r in conn.execute(q, (game_number,))]


def load_snapshot(conn, snapshot_date):
    """Return {game_number: {'total': (printed, claimed), 'levels': {amount: (printed, claimed)}}}"""
    out = {}
    q = """SELECT game_number, prize_level, total_prizes, prizes_claimed
           FROM prize_snapshots WHERE snapshot_date = ?"""
    for r in conn.execute(q, (snapshot_date,)):
        g = out.setdefault(r["game_number"], {"total": None, "levels": {}})
        if r["prize_level"] < 0:
            g["total"] = (r["total_prizes"], r["prizes_claimed"])
        else:
            g["levels"][r["prize_level"]] = (r["total_prizes"], r["prizes_claimed"])
    return out


def load_snapshot_filtered(conn, snapshot_date, game_number):
    out = {}
    q = """SELECT game_number, prize_level, total_prizes, prizes_claimed
           FROM prize_snapshots WHERE snapshot_date = ? AND game_number = ?"""
    for r in conn.execute(q, (snapshot_date, game_number)):
        g = out.setdefault(r["game_number"], {"total": None, "levels": {}})
        if r["prize_level"] < 0:
            g["total"] = (r["total_prizes"], r["prizes_claimed"])
        else:
            g["levels"][r["prize_level"]] = (r["total_prizes"], r["prizes_claimed"])
    return out


def load_games(conn):
    return {r["game_number"]: dict(r) for r in conn.execute("SELECT * FROM games")}
