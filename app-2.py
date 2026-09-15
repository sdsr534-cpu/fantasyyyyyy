from flask import Flask, jsonify, send_from_directory, request
import requests
import os
import sqlite3
import time
import hmac
import hashlib
import json
import random
import string
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from contextlib import closing

app = Flask(__name__, static_folder=".", static_url_path="")

FPL_BASE = "https://fantasy.premierleague.com/api/"
TIMEOUT = 15

@app.get("/")
def index():
    return send_from_directory(".", "index.html")

@app.get("/api/<path:path>")
def fpl_proxy(path):
    # Only proxy read-only GET requests to the public FPL API.
    # This deliberately does not forward cookies or authorization headers.
    if not path:
        return jsonify({"error": "Missing FPL API path"}), 400

    # Keep the proxy restricted to the FPL API path space.
    url = FPL_BASE + path
    try:
        r = requests.get(
            url,
            params=request.args,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; SajedFantasy/1.0)",
                "Accept": "application/json",
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return jsonify({"error": "FPL request failed", "detail": str(exc)}), 502

    content_type = r.headers.get("Content-Type", "application/json")
    return (r.content, r.status_code, {"Content-Type": content_type, "Cache-Control": "no-store"})

@app.get("/health")
def health():
    return jsonify({"ok": True})


# ============================================================
# FANTASY SAJED LEAGUE
# The one custom feature allowed to use our own backend/database.
# This is NOT the official FPL league system — it is a Sajed Fantasy
# league that lives entirely in our own SQLite database.
#
# We only ever store: Telegram user id / username / first name / last
# name, FPL Team ID, Sajed league id/code/name, membership rows
# (with role), and created/updated timestamps. All football data
# (points, ranks, team names, team value) is fetched live from the
# real FPL API on every standings/profile/history call — nothing
# football-related is invented or cached beyond a short TTL, which
# exists only to protect the public FPL API from being hammered.
#
# Storage note for Render: SQLite lives on local disk, which is
# ephemeral on Render's free/standard web services (it resets on
# every deploy/restart) unless you attach a Render Persistent Disk
# and point SAJED_DB_PATH at a file inside its mount path. Set the
# SAJED_DB_PATH env var accordingly once you add a disk.
#
# TELEGRAM IDENTITY
# The frontend never tells us "who it is" and gets trusted — it sends
# Telegram's raw `initData` string (Telegram.WebApp.initData) on every
# call that touches identity, and we verify it here, server-side,
# using the bot token via HMAC-SHA256 exactly as Telegram's Mini Apps
# spec requires. The bot token lives ONLY in the TELEGRAM_BOT_TOKEN
# environment variable on this server — it is never sent to, or
# readable from, the frontend. If TELEGRAM_BOT_TOKEN is not configured,
# identity verification fails closed (nobody can register/create/join)
# rather than trusting unverified client-supplied fields.
# ============================================================

DB_PATH = os.environ.get(
    "SAJED_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sajed_league.db"),
)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
INIT_DATA_MAX_AGE = 86400  # seconds — reject stale/replayed init data
STANDINGS_CACHE_TTL = 45  # seconds — protects the public FPL API from being hammered
LEAGUE_CODE_ALPHABET = string.ascii_uppercase + string.digits
LEAGUE_CODE_LENGTH = 6


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _add_column_if_missing(conn, table, column, ddl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db():
    with closing(db_conn()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id TEXT PRIMARY KEY,
                telegram_username TEXT,
                telegram_first_name TEXT,
                telegram_last_name TEXT,
                fpl_team_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leagues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS league_members (
                league_id INTEGER NOT NULL,
                telegram_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                joined_at TEXT NOT NULL,
                PRIMARY KEY (league_id, telegram_id),
                FOREIGN KEY (league_id) REFERENCES leagues(id),
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
        """)
        # Lightweight migration path for databases created by earlier
        # versions of this feature (single combined telegram_name column,
        # no league ownership/role columns yet).
        _add_column_if_missing(conn, "users", "telegram_first_name", "telegram_first_name TEXT")
        _add_column_if_missing(conn, "users", "telegram_last_name", "telegram_last_name TEXT")
        _add_column_if_missing(conn, "leagues", "created_by", "created_by TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "leagues", "updated_at", "updated_at TEXT")
        _add_column_if_missing(conn, "league_members", "role", "role TEXT NOT NULL DEFAULT 'member'")


init_db()


# ------------------------------------------------------------
# Telegram Mini App init-data verification (server-side only)
# ------------------------------------------------------------

def verify_telegram_init_data(init_data):
    """
    Cryptographically verifies a Telegram Mini App `initData` string using
    the bot token, per Telegram's WebApp spec:
      secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
      check_hash = HMAC_SHA256(key=secret_key, msg=data_check_string)
    Returns a dict of verified identity fields on success, or None if the
    payload is missing, malformed, unsigned, expired, or the token isn't
    configured. NEVER falls back to trusting unsigned client-supplied
    identity fields.
    """
    if not init_data or not TELEGRAM_BOT_TOKEN:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None

    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = data.get("auth_date")
    if auth_date:
        try:
            if time.time() - int(auth_date) > INIT_DATA_MAX_AGE:
                return None
        except ValueError:
            return None

    user_raw = data.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except (ValueError, TypeError):
        return None
    telegram_id = user.get("id")
    if not telegram_id:
        return None

    return {
        "telegram_id": str(telegram_id),
        "telegram_username": user.get("username") or None,
        "telegram_first_name": user.get("first_name") or None,
        "telegram_last_name": user.get("last_name") or None,
    }


def require_identity(payload):
    """Extracts and verifies `init_data` from a request payload (JSON body
    or query args). Returns (identity, error_response_or_None)."""
    init_data = (payload.get("init_data") or "").strip()
    if not TELEGRAM_BOT_TOKEN:
        return None, (jsonify({
            "error": "Telegram verification is not configured on this server. "
                     "Set the TELEGRAM_BOT_TOKEN environment variable."
        }), 503)
    identity = verify_telegram_init_data(init_data)
    if not identity:
        return None, (jsonify({"error": "Could not verify Telegram identity. Please reopen the app from Telegram."}), 401)
    return identity, None


def upsert_user_identity(conn, identity, fpl_team_id=None):
    """Keeps the users row's Telegram identity fields fresh on every verified
    call, and updates fpl_team_id only when explicitly provided."""
    ts = now_iso()
    existing = conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (identity["telegram_id"],)
    ).fetchone()
    if existing:
        new_team_id = fpl_team_id if fpl_team_id is not None else existing["fpl_team_id"]
        conn.execute(
            """UPDATE users SET telegram_username=?, telegram_first_name=?, telegram_last_name=?,
               fpl_team_id=?, updated_at=? WHERE telegram_id=?""",
            (identity["telegram_username"], identity["telegram_first_name"], identity["telegram_last_name"],
             new_team_id, ts, identity["telegram_id"]),
        )
    else:
        conn.execute(
            """INSERT INTO users
               (telegram_id, telegram_username, telegram_first_name, telegram_last_name,
                fpl_team_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (identity["telegram_id"], identity["telegram_username"], identity["telegram_first_name"],
             identity["telegram_last_name"], fpl_team_id, ts, ts),
        )
    return conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (identity["telegram_id"],)
    ).fetchone()


def display_name(user_row):
    name = " ".join(filter(None, [user_row["telegram_first_name"], user_row["telegram_last_name"]])).strip()
    return name or (("@" + user_row["telegram_username"]) if user_row["telegram_username"] else user_row["telegram_id"])


# ------------------------------------------------------------
# Live FPL data helpers — never invent a value; None on any failure
# ------------------------------------------------------------

def _fpl_get(path):
    try:
        r = requests.get(
            FPL_BASE + path,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; SajedFantasy/1.0)",
                "Accept": "application/json",
            },
            timeout=TIMEOUT,
        )
        if r.ok:
            return r.json()
    except requests.RequestException:
        pass
    return None


def fetch_fpl_entry(fpl_team_id):
    """Live lookup of a manager's official FPL entry (name, manager name)."""
    return _fpl_get("entry/" + str(fpl_team_id) + "/")


def fetch_fpl_entry_history(fpl_team_id):
    """Live lookup of a manager's full gameweek-by-gameweek history — this is
    also where gw points, running total, overall rank and team value per GW
    come from, so ranking history and 'previous GW' can be derived from it
    without inventing anything."""
    return _fpl_get("entry/" + str(fpl_team_id) + "/history/")


_fpl_cache = {}  # fpl_team_id -> {"ts": float, "entry": dict|None, "history": dict|None}


def get_member_fpl_data(fpl_team_id, ttl=STANDINGS_CACHE_TTL):
    now = time.time()
    cached = _fpl_cache.get(fpl_team_id)
    if cached and (now - cached["ts"]) < ttl:
        return cached["entry"], cached["history"]
    entry = fetch_fpl_entry(fpl_team_id)
    history = fetch_fpl_entry_history(fpl_team_id)
    _fpl_cache[fpl_team_id] = {"ts": now, "entry": entry, "history": history}
    return entry, history


def gen_unique_league_code(conn):
    for _ in range(25):
        code = "".join(random.choices(LEAGUE_CODE_ALPHABET, k=LEAGUE_CODE_LENGTH))
        exists = conn.execute("SELECT 1 FROM leagues WHERE code = ?", (code,)).fetchone()
        if not exists:
            return code
    raise RuntimeError("Could not generate a unique league code")


def get_league_by_code(conn, code):
    return conn.execute(
        "SELECT * FROM leagues WHERE code = ?", ((code or "").strip().upper(),)
    ).fetchone()


def get_membership(conn, league_id, telegram_id):
    return conn.execute(
        "SELECT * FROM league_members WHERE league_id=? AND telegram_id=?",
        (league_id, telegram_id),
    ).fetchone()


def build_league_standings(conn, league):
    """Membership comes from our DB; every point/rank/team-name/value comes
    live from the real FPL API for each member's Team ID."""
    rows = conn.execute(
        """SELECT u.telegram_id, u.telegram_username, u.telegram_first_name,
                  u.telegram_last_name, u.fpl_team_id
           FROM league_members m
           JOIN users u ON u.telegram_id = m.telegram_id
           WHERE m.league_id = ?""",
        (league["id"],),
    ).fetchall()

    members = []
    for row in rows:
        entry, history = get_member_fpl_data(row["fpl_team_id"])
        current = (history or {}).get("current") or []
        latest = current[-1] if current else None
        manager_name = (
            (entry.get("player_first_name", "") + " " + entry.get("player_last_name", "")).strip()
            if entry else None
        )
        members.append({
            "telegram_id": row["telegram_id"],
            "telegram_username": row["telegram_username"],
            "telegram_name": " ".join(filter(None, [row["telegram_first_name"], row["telegram_last_name"]])).strip() or None,
            "fpl_team_id": row["fpl_team_id"],
            "fpl_team_name": entry.get("name") if entry else None,
            "fpl_manager_name": manager_name or None,
            "total_points": latest.get("total_points") if latest else (entry.get("summary_overall_points") if entry else None),
            "gw_points": latest.get("points") if latest else (entry.get("summary_event_points") if entry else None),
            "overall_rank": latest.get("overall_rank") if latest else (entry.get("summary_overall_rank") if entry else None),
            "team_value": (latest.get("value") / 10.0) if (latest and latest.get("value") is not None) else None,
            "bank": (latest.get("bank") / 10.0) if (latest and latest.get("bank") is not None) else None,
            "current_gw": latest.get("event") if latest else None,
            "data_ok": entry is not None and history is not None,
        })

    overall = sorted(members, key=lambda m: (m["total_points"] is None, -(m["total_points"] or 0)))
    for i, m in enumerate(overall, start=1):
        m["overall_position"] = i
    gw = sorted(members, key=lambda m: (m["gw_points"] is None, -(m["gw_points"] or 0)))
    for i, m in enumerate(gw, start=1):
        m["gw_position"] = i

    # `members` list keeps a single merged view (both positions attached)
    by_id = {m["telegram_id"]: m for m in members}
    return by_id, overall, gw


def league_public(league):
    return {"id": league["id"], "code": league["code"], "name": league["name"], "created_by": league["created_by"]}


# ------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------

@app.post("/sajedleague/link-team")
def sajed_link_team():
    """Verifies the caller's Telegram identity and links/updates their real,
    FPL-API-verified FPL Team ID. Does not join any league by itself."""
    payload = request.get_json(silent=True) or {}
    identity, err = require_identity(payload)
    if err:
        return err

    fpl_team_id = str(payload.get("fpl_team_id") or "").strip()
    if not fpl_team_id.isdigit():
        return jsonify({"error": "A valid numeric fpl_team_id is required"}), 400
    entry = fetch_fpl_entry(fpl_team_id)
    if entry is None:
        return jsonify({"error": "That FPL Team ID could not be found on the official FPL API"}), 404

    with closing(db_conn()) as conn, conn:
        user = upsert_user_identity(conn, identity, fpl_team_id=fpl_team_id)

    return jsonify({
        "ok": True,
        "user": {
            "telegram_id": user["telegram_id"],
            "display_name": display_name(user),
            "fpl_team_id": user["fpl_team_id"],
            "fpl_team_name": entry.get("name"),
        },
    })


@app.get("/sajedleague/me")
def sajed_me():
    identity, err = require_identity(request.args)
    if err:
        return err
    with closing(db_conn()) as conn, conn:
        user = upsert_user_identity(conn, identity)
        league_rows = conn.execute(
            """SELECT l.code, l.name, m.role,
                      (SELECT COUNT(*) FROM league_members m2 WHERE m2.league_id = l.id) AS member_count
               FROM league_members m JOIN leagues l ON l.id = m.league_id
               WHERE m.telegram_id = ? ORDER BY m.joined_at DESC""",
            (identity["telegram_id"],),
        ).fetchall()

    return jsonify({
        "registered": True,
        "telegram_id": user["telegram_id"],
        "display_name": display_name(user),
        "telegram_username": user["telegram_username"],
        "fpl_team_id": user["fpl_team_id"],
        "leagues": [dict(r) for r in league_rows],
    })


@app.post("/sajedleague/leagues/create")
def sajed_create_league():
    payload = request.get_json(silent=True) or {}
    identity, err = require_identity(payload)
    if err:
        return err

    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "League name is required"}), 400
    if len(name) > 60:
        return jsonify({"error": "League name is too long (60 characters max)"}), 400

    with closing(db_conn()) as conn, conn:
        user = conn.execute("SELECT * FROM users WHERE telegram_id=?", (identity["telegram_id"],)).fetchone()
        if not user or not user["fpl_team_id"]:
            return jsonify({"error": "Link your FPL Team ID before creating a league"}), 400

        code = gen_unique_league_code(conn)
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO leagues (code, name, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (code, name, identity["telegram_id"], ts, ts),
        )
        league_id = cur.lastrowid
        conn.execute(
            "INSERT INTO league_members (league_id, telegram_id, role, joined_at) VALUES (?, ?, 'creator', ?)",
            (league_id, identity["telegram_id"], ts),
        )
        league = conn.execute("SELECT * FROM leagues WHERE id=?", (league_id,)).fetchone()

    return jsonify({"ok": True, "league": league_public(league), "role": "creator"})


@app.post("/sajedleague/leagues/join")
def sajed_join_league():
    payload = request.get_json(silent=True) or {}
    identity, err = require_identity(payload)
    if err:
        return err

    code = (payload.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "League code is required"}), 400

    with closing(db_conn()) as conn, conn:
        user = conn.execute("SELECT * FROM users WHERE telegram_id=?", (identity["telegram_id"],)).fetchone()

        # A team ID may be supplied inline for a first-time joiner; otherwise
        # they must already have one linked.
        fpl_team_id = str(payload.get("fpl_team_id") or "").strip() or (user["fpl_team_id"] if user else None)
        if not fpl_team_id or not str(fpl_team_id).isdigit():
            return jsonify({"error": "A valid numeric fpl_team_id is required"}), 400
        entry = fetch_fpl_entry(fpl_team_id)
        if entry is None:
            return jsonify({"error": "That FPL Team ID could not be found on the official FPL API"}), 404

        league = get_league_by_code(conn, code)
        if not league:
            return jsonify({"error": "No league found with that code"}), 404

        user = upsert_user_identity(conn, identity, fpl_team_id=str(fpl_team_id))
        existing_member = get_membership(conn, league["id"], identity["telegram_id"])
        if not existing_member:
            conn.execute(
                "INSERT INTO league_members (league_id, telegram_id, role, joined_at) VALUES (?, ?, 'member', ?)",
                (league["id"], identity["telegram_id"], now_iso()),
            )

    return jsonify({"ok": True, "league": league_public(league)})


@app.post("/sajedleague/leagues/leave")
def sajed_leave_league():
    payload = request.get_json(silent=True) or {}
    identity, err = require_identity(payload)
    if err:
        return err
    code = (payload.get("code") or "").strip().upper()

    with closing(db_conn()) as conn, conn:
        league = get_league_by_code(conn, code)
        if not league:
            return jsonify({"error": "No league found with that code"}), 404
        member = get_membership(conn, league["id"], identity["telegram_id"])
        if not member:
            return jsonify({"error": "You are not a member of this league"}), 400
        if member["role"] == "creator":
            return jsonify({"error": "As the creator, delete the league instead of leaving it"}), 403
        conn.execute(
            "DELETE FROM league_members WHERE league_id=? AND telegram_id=?",
            (league["id"], identity["telegram_id"]),
        )

    return jsonify({"ok": True})


@app.post("/sajedleague/leagues/delete")
def sajed_delete_league():
    payload = request.get_json(silent=True) or {}
    identity, err = require_identity(payload)
    if err:
        return err
    code = (payload.get("code") or "").strip().upper()

    with closing(db_conn()) as conn, conn:
        league = get_league_by_code(conn, code)
        if not league:
            return jsonify({"error": "No league found with that code"}), 404
        member = get_membership(conn, league["id"], identity["telegram_id"])
        if not member or member["role"] != "creator":
            return jsonify({"error": "Only the league creator can delete this league"}), 403
        conn.execute("DELETE FROM league_members WHERE league_id=?", (league["id"],))
        conn.execute("DELETE FROM leagues WHERE id=?", (league["id"],))

    return jsonify({"ok": True})


@app.get("/sajedleague/leagues/<code>/standings")
def sajed_league_standings(code):
    with closing(db_conn()) as conn:
        league = get_league_by_code(conn, code)
        if not league:
            return jsonify({"error": "No league found with that code"}), 404
        by_id, overall, gw = build_league_standings(conn, league)

    def strip(m):
        return {k: v for k, v in m.items()}

    return jsonify({
        "league": league_public(league),
        "updated_at": now_iso(),
        "member_count": len(overall),
        "overall": [strip(m) for m in overall],
        "gameweek": [strip(m) for m in gw],
    })


@app.get("/sajedleague/leagues/<code>/members")
def sajed_league_members(code):
    with closing(db_conn()) as conn:
        league = get_league_by_code(conn, code)
        if not league:
            return jsonify({"error": "No league found with that code"}), 404
        rows = conn.execute(
            """SELECT u.telegram_id, u.telegram_username, u.telegram_first_name,
                      u.telegram_last_name, m.role, m.joined_at
               FROM league_members m JOIN users u ON u.telegram_id = m.telegram_id
               WHERE m.league_id = ? ORDER BY m.joined_at ASC""",
            (league["id"],),
        ).fetchall()

    return jsonify({
        "league": league_public(league),
        "members": [
            {
                "telegram_id": r["telegram_id"],
                "telegram_username": r["telegram_username"],
                "display_name": " ".join(filter(None, [r["telegram_first_name"], r["telegram_last_name"]])).strip()
                                 or (("@" + r["telegram_username"]) if r["telegram_username"] else r["telegram_id"]),
                "role": r["role"],
                "joined_at": r["joined_at"],
            }
            for r in rows
        ],
    })


@app.get("/sajedleague/leagues/<code>/member/<telegram_id>")
def sajed_member_profile(code, telegram_id):
    with closing(db_conn()) as conn:
        league = get_league_by_code(conn, code)
        if not league:
            return jsonify({"error": "No league found with that code"}), 404
        member = get_membership(conn, league["id"], telegram_id)
        if not member:
            return jsonify({"error": "That user is not a member of this league"}), 404
        by_id, overall, gw = build_league_standings(conn, league)

    profile = by_id.get(telegram_id)
    if not profile:
        return jsonify({"error": "Profile data unavailable"}), 502

    return jsonify({
        "league": league_public(league),
        "role": member["role"],
        "profile": profile,
    })


@app.get("/sajedleague/leagues/<code>/history")
def sajed_league_history(code):
    """Ranking History: for every completed gameweek, ranks each member of
    this league by their real running total at that gameweek (from the FPL
    entry history endpoint) — never invented."""
    with closing(db_conn()) as conn:
        league = get_league_by_code(conn, code)
        if not league:
            return jsonify({"error": "No league found with that code"}), 404
        rows = conn.execute(
            """SELECT u.telegram_id, u.telegram_username, u.telegram_first_name,
                      u.telegram_last_name, u.fpl_team_id
               FROM league_members m JOIN users u ON u.telegram_id = m.telegram_id
               WHERE m.league_id = ?""",
            (league["id"],),
        ).fetchall()

    member_histories = {}
    events_seen = set()
    for row in rows:
        _, history = get_member_fpl_data(row["fpl_team_id"])
        current = (history or {}).get("current") or []
        by_event = {h["event"]: h["total_points"] for h in current if "event" in h}
        member_histories[row["telegram_id"]] = {
            "display_name": " ".join(filter(None, [row["telegram_first_name"], row["telegram_last_name"]])).strip()
                             or (("@" + row["telegram_username"]) if row["telegram_username"] else row["telegram_id"]),
            "by_event": by_event,
        }
        events_seen.update(by_event.keys())

    events_sorted = sorted(events_seen)
    per_event_ranks = {tid: [] for tid in member_histories}
    for ev in events_sorted:
        standings_this_event = [
            (tid, info["by_event"][ev]) for tid, info in member_histories.items() if ev in info["by_event"]
        ]
        standings_this_event.sort(key=lambda x: -x[1])
        for rank, (tid, total) in enumerate(standings_this_event, start=1):
            per_event_ranks[tid].append({"event": ev, "rank": rank, "total_points": total})

    return jsonify({
        "league": league_public(league),
        "events": events_sorted,
        "members": [
            {"telegram_id": tid, "display_name": info["display_name"], "history": per_event_ranks[tid]}
            for tid, info in member_histories.items()
        ],
    })


@app.get("/sajedleague/leagues/<code>/previous-gw")
def sajed_league_previous_gw(code):
    """Previous Gameweek Results: the gameweek before each member's latest
    played one, with real points/rank for exactly that gameweek."""
    with closing(db_conn()) as conn:
        league = get_league_by_code(conn, code)
        if not league:
            return jsonify({"error": "No league found with that code"}), 404
        rows = conn.execute(
            """SELECT u.telegram_id, u.telegram_username, u.telegram_first_name,
                      u.telegram_last_name, u.fpl_team_id
               FROM league_members m JOIN users u ON u.telegram_id = m.telegram_id
               WHERE m.league_id = ?""",
            (league["id"],),
        ).fetchall()

    per_member = {}
    latest_events = []
    for row in rows:
        _, history = get_member_fpl_data(row["fpl_team_id"])
        current = (history or {}).get("current") or []
        per_member[row["telegram_id"]] = {
            "display_name": " ".join(filter(None, [row["telegram_first_name"], row["telegram_last_name"]])).strip()
                             or (("@" + row["telegram_username"]) if row["telegram_username"] else row["telegram_id"]),
            "current": current,
        }
        if current:
            latest_events.append(current[-1]["event"])

    if not latest_events:
        return jsonify({"league": league_public(league), "gameweek": None, "results": []})

    latest_event = max(latest_events)
    previous_event = latest_event - 1
    if previous_event < 1:
        return jsonify({"league": league_public(league), "gameweek": previous_event, "results": []})

    results = []
    for tid, info in per_member.items():
        match = next((h for h in info["current"] if h.get("event") == previous_event), None)
        results.append({
            "telegram_id": tid,
            "display_name": info["display_name"],
            "gw_points": match.get("points") if match else None,
            "total_points": match.get("total_points") if match else None,
        })

    results.sort(key=lambda r: (r["gw_points"] is None, -(r["gw_points"] or 0)))
    for i, r in enumerate(results, start=1):
        r["rank"] = i

    return jsonify({"league": league_public(league), "gameweek": previous_event, "results": results})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
