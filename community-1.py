"""Settings, daily limits, trading hours, lessons and challenges (stored in the same SQLite file)."""
import json
import random
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Africa/Lubumbashi")  # heure de Goma (UTC+2)
DEFAULTS = {"trading_enabled": "1", "open_min": "0", "close_min": "0", "base_daily": "5", "elite_daily": "20", "notice": ""}
ELITE_MAX = 50
ENERGY_PER_REFERRAL = 5


def init(db):
    conn = sqlite3.connect(db)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, body TEXT, ts REAL)")
    c.execute("""CREATE TABLE IF NOT EXISTS lesson_views (
        uid INTEGER, lesson_id INTEGER, seconds REAL DEFAULT 0, first_seen REAL, last_seen REAL,
        PRIMARY KEY (uid, lesson_id))""")
    c.execute("CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, name TEXT, text TEXT, is_admin INTEGER DEFAULT 0, ts REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS game_plays (id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, prize TEXT, forced INTEGER DEFAULT 0, ts REAL)")
    cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    for name, ddl in (("elite", "INTEGER DEFAULT 0"), ("referred_by", "INTEGER"), ("ref_rewarded", "INTEGER DEFAULT 0"),
                      ("energy", "INTEGER DEFAULT 0"), ("credits", "INTEGER DEFAULT 0"), ("sub_admin", "INTEGER DEFAULT 0"), ("level_unlock", "INTEGER DEFAULT 0"),
                      ("game_last", "REAL DEFAULT 0"), ("unlimited_until", "REAL DEFAULT 0"), ("notice_ack", "INTEGER DEFAULT 0")):
        if name not in cols:
            c.execute("ALTER TABLE users ADD COLUMN %s %s" % (name, ddl))
    conn.commit()
    conn.close()


def fmt_min(m: int) -> str:
    return "%02d:%02d" % (m // 60, m % 60)


def get_settings(db) -> dict:
    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    conn.close()
    v = {**DEFAULTS, **rows}
    return {
        "trading_enabled": v["trading_enabled"] == "1",
        "open_min": int(v["open_min"]),
        "close_min": int(v["close_min"]),
        "base_daily": int(v["base_daily"]),
        "elite_daily": int(v["elite_daily"]),
        "notice": v["notice"],
    }


def set_settings(db, data: dict):
    out = {}
    if "trading_enabled" in data:
        out["trading_enabled"] = "1" if data["trading_enabled"] else "0"
    for k in ("open_min", "close_min"):
        if k in data:
            m = int(data[k])
            if not 0 <= m < 1440:
                raise ValueError("Heure invalide.")
            out[k] = str(m)
    if "base_daily" in data:
        n = int(data["base_daily"])
        if not 0 <= n <= 200:
            raise ValueError("Limite journalière invalide (0 à 200).")
        out["base_daily"] = str(n)
    if "elite_daily" in data:
        n = int(data["elite_daily"])
        if not 0 <= n <= 500:
            raise ValueError("Limite élite invalide (0 à 500).")
        out["elite_daily"] = str(n)
    if "notice" in data:
        out["notice"] = str(data["notice"])[:300]
    conn = sqlite3.connect(db)
    for k, val in out.items():
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, val))
    conn.commit()
    conn.close()


def window_open(s: dict, now=None) -> bool:
    now = now or datetime.now(TZ)
    m = now.hour * 60 + now.minute
    o, c = s["open_min"], s["close_min"]
    if o == c:
        return True
    return o <= m < c if o < c else (m >= o or m < c)


def day_start_ts() -> float:
    return datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def lesson_required(body: str) -> int:
    return max(15, min(180, len(body or "") // 20))


def challenges(db, uid: int):
    conn = sqlite3.connect(db)
    c = conn.cursor()
    c.execute("""SELECT l.body, COALESCE(v.seconds, 0) FROM lessons l
                 LEFT JOIN lesson_views v ON v.lesson_id = l.id AND v.uid = ?""", (uid,))
    read = sum(1 for body, sec in c.fetchall() if sec >= lesson_required(body))
    chat_n = c.execute("SELECT COUNT(*) FROM chat WHERE uid=?", (uid,)).fetchone()[0]
    demo_n = c.execute("SELECT COUNT(*) FROM trades WHERE uid=? AND mode='demo' AND st IN ('win','loss','draw')", (uid,)).fetchone()[0]
    demo_w = c.execute("SELECT COUNT(*) FROM trades WHERE uid=? AND mode='demo' AND st='win'", (uid,)).fetchone()[0]
    demo_l = c.execute("SELECT COUNT(*) FROM trades WHERE uid=? AND mode='demo' AND st='loss'", (uid,)).fetchone()[0]
    conn.close()
    wl = demo_w + demo_l
    rate_ok = wl >= 20 and demo_w / wl >= 0.5
    defs = [
        ("read_3", "Lecteur assidu", "Lis 3 leçons du cours en entier.", read, 3, 2),
        ("read_10", "Élève sérieux", "Lis 10 leçons du cours en entier.", read, 10, 3),
        ("chat_3", "Esprit d'équipe", "Écris 3 messages dans la discussion.", chat_n, 3, 1),
        ("demo_10", "Entraînement", "Fais 10 trades en mode démo.", demo_n, 10, 2),
        ("demo_30", "Régularité", "Fais 30 trades en mode démo.", demo_n, 30, 3),
        ("demo_rate", "Discipline", "Atteins 50 % de victoires sur 20 trades démo.", 20 if rate_ok else min(wl, 19), 20, 3),
    ]
    out, bonus = [], 0
    for cid, title, desc, prog, target, reward in defs:
        done = prog >= target
        if done:
            bonus += reward
        out.append({"id": cid, "title": title, "desc": desc, "progress": min(prog, target), "target": target, "reward": reward, "done": done})
    return out, bonus


def user_flags(db, uid: int):
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT elite, energy, credits FROM users WHERE uid=?", (uid,)).fetchone()
    conn.close()
    return (bool(row[0]), row[1] or 0, row[2] or 0) if row else (False, 0, 0)


def unlimited_until(db, uid: int) -> float:
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT unlimited_until FROM users WHERE uid=?", (uid,)).fetchone()
    conn.close()
    t = (row[0] or 0) if row else 0
    return t if t > time.time() else 0


def daily_limit(db, uid: int, settings: dict = None, force_elite: bool = False) -> dict:
    settings = settings or get_settings(db)
    elite, _, credits = user_flags(db, uid)
    elite = elite or force_elite
    base, bonus = (settings["elite_daily"] if elite else settings["base_daily"]), 0
    conn = sqlite3.connect(db)
    used = conn.execute("SELECT COUNT(*) FROM trades WHERE uid=? AND ts>=?", (uid, day_start_ts())).fetchone()[0]
    conn.close()
    limit = base + bonus
    unl = unlimited_until(db, uid)
    remaining = 10 ** 6 if unl else max(0, limit - used) + credits
    return {"used": used, "base": base, "bonus": bonus, "limit": limit, "credits": credits, "elite": elite,
            "remaining": remaining, "unlimited_until": unl}


def trading_gate(db, uid: int, force_elite: bool = False):
    s = get_settings(db)
    if not s["trading_enabled"]:
        return False, "Le trading est suspendu par le mentor." + ((" " + s["notice"]) if s["notice"] else "")
    if not window_open(s):
        return False, "Trading fermé. Ouvert de %s à %s (heure de Goma)." % (fmt_min(s["open_min"]), fmt_min(s["close_min"]))
    lim = daily_limit(db, uid, s, force_elite)
    if lim["remaining"] <= 0:
        hint = "" if lim["elite"] else " Joue au Super 7, ou échange des énergies de parrainage, pour en débloquer plus."
        return False, "Limite de %d trades atteinte pour aujourd'hui.%s" % (lim["limit"], hint)
    return True, ""


def after_trade(db, uid: int, force_elite: bool = False):
    """Call after a trade row is inserted: beyond the daily limit, a trade uses one credit."""
    lim = daily_limit(db, uid, None, force_elite)
    if lim["unlimited_until"]:
        return
    if lim["used"] > lim["limit"] and lim["credits"] > 0:
        conn = sqlite3.connect(db)
        conn.execute("UPDATE users SET credits = credits - 1 WHERE uid=? AND credits > 0", (uid,))
        conn.commit()
        conn.close()


# --- Élite et parrainage ---
def set_elite(db, uid: int, value: bool):
    conn = sqlite3.connect(db)
    try:
        if value:
            n = conn.execute("SELECT COUNT(*) FROM users WHERE elite=1 AND uid<>?", (uid,)).fetchone()[0]
            if n >= ELITE_MAX:
                raise ValueError("Maximum de %d membres élite atteint." % ELITE_MAX)
            conn.execute("UPDATE users SET elite=1, affiliate_verified=1 WHERE uid=?", (uid,))
        else:
            conn.execute("UPDATE users SET elite=0 WHERE uid=?", (uid,))
        conn.commit()
    finally:
        conn.close()


def elite_count(db) -> int:
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM users WHERE elite=1").fetchone()[0]
    conn.close()
    return n


def register_referral(db, uid: int, ref_uid: int) -> bool:
    if uid == ref_uid:
        return False
    conn = sqlite3.connect(db)
    me = conn.execute("SELECT referred_by, affiliate_verified FROM users WHERE uid=?", (uid,)).fetchone()
    ref = conn.execute("SELECT 1 FROM users WHERE uid=?", (ref_uid,)).fetchone()
    ok = bool(me and ref and me[0] is None and not me[1])
    if ok:
        conn.execute("UPDATE users SET referred_by=? WHERE uid=?", (ref_uid, uid))
        conn.commit()
    conn.close()
    return ok


def reward_referrer(db, uid: int):
    """When the mentor validates `uid`, the person who referred them gets energy (once). Returns the referrer uid."""
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT referred_by, ref_rewarded FROM users WHERE uid=?", (uid,)).fetchone()
    ref = None
    if row and row[0] and not row[1]:
        ref = row[0]
        conn.execute("UPDATE users SET energy = energy + ? WHERE uid=?", (ENERGY_PER_REFERRAL, ref))
        conn.execute("UPDATE users SET ref_rewarded=1 WHERE uid=?", (uid,))
        conn.commit()
    conn.close()
    return ref


def referral_info(db, uid: int, bot_username: str = ""):
    conn = sqlite3.connect(db)
    total = conn.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (uid,)).fetchone()[0]
    ok = conn.execute("SELECT COUNT(*) FROM users WHERE referred_by=? AND ref_rewarded=1", (uid,)).fetchone()[0]
    conn.close()
    _, energy, credits = user_flags(db, uid)
    return {"link": ("https://t.me/%s?start=ref_%d" % (bot_username, uid)) if bot_username else "",
            "referred": total, "validated": ok, "energy": energy, "credits": credits, "per_referral": ENERGY_PER_REFERRAL}


def exchange_energy(db, uid: int, amount: int) -> bool:
    if amount <= 0:
        return False
    conn = sqlite3.connect(db)
    cur = conn.execute("UPDATE users SET energy = energy - ?, credits = credits + ? WHERE uid=? AND energy >= ?", (amount, amount, uid, amount))
    conn.commit()
    ok = cur.rowcount == 1
    conn.close()
    return ok


# --- Admins supplémentaires ---
SUB_ADMIN_MAX = 3


def sub_admin_ids(db) -> set:
    conn = sqlite3.connect(db)
    ids = {r[0] for r in conn.execute("SELECT uid FROM users WHERE sub_admin=1").fetchall()}
    conn.close()
    return ids


def set_sub_admin(db, uid: int, value: bool):
    conn = sqlite3.connect(db)
    try:
        if value:
            n = conn.execute("SELECT COUNT(*) FROM users WHERE sub_admin=1 AND uid<>?", (uid,)).fetchone()[0]
            if n >= SUB_ADMIN_MAX:
                raise ValueError("Maximum de %d admins supplémentaires atteint." % SUB_ADMIN_MAX)
            conn.execute("UPDATE users SET sub_admin=1, affiliate_verified=1 WHERE uid=?", (uid,))
        else:
            conn.execute("UPDATE users SET sub_admin=0 WHERE uid=?", (uid,))
        conn.commit()
    finally:
        conn.close()


def give_credits(db, uid: int, n: int) -> bool:
    if not 1 <= n <= 100:
        return False
    conn = sqlite3.connect(db)
    cur = conn.execute("UPDATE users SET credits = credits + ? WHERE uid=?", (n, uid))
    conn.commit()
    conn.close()
    return cur.rowcount == 1


def team_report(db):
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT uid, name, username, energy, credits FROM users WHERE sub_admin=1").fetchall()
    out = []
    for uid, name, username, energy, credits in rows:
        trades = conn.execute("SELECT COUNT(*), SUM(CASE WHEN st='win' THEN 1 ELSE 0 END) FROM trades WHERE uid=?", (uid,)).fetchone()
        secs = conn.execute("SELECT COALESCE(SUM(seconds),0) FROM lesson_views WHERE uid=?", (uid,)).fetchone()[0]
        refs = conn.execute("SELECT COUNT(*) FROM users WHERE referred_by=? AND ref_rewarded=1", (uid,)).fetchone()[0]
        chat_n = conn.execute("SELECT COUNT(*) FROM chat WHERE uid=?", (uid,)).fetchone()[0]
        ch, _ = challenges(db, uid)
        out.append({"uid": uid, "name": name, "username": username, "energy": energy or 0, "credits": credits or 0,
                    "trades": trades[0] or 0, "wins": trades[1] or 0, "minutes": int(secs / 60), "referrals": refs, "messages": chat_n,
                    "challenges_done": sum(1 for x in ch if x["done"]), "challenges_total": len(ch)})
    conn.close()
    return out


# --- Niveaux : le niveau est réglé par l'admin (plus de déblocage par nombre de trades) ; le score reste visible du mentor seulement ---
LEVELS = [(0, "Débutant"), (1, "Bronze"), (2, "Argent"), (3, "Or"), (4, "Diamant")]
LEVEL_THRESHOLDS = [0, 15, 35, 60, 90]
_lv_cache = {"t": 0.0, "v": {}}


def level_name(n: int) -> str:
    return LEVELS[max(0, min(4, n))][1]


def _compute_levels(db) -> dict:
    """Score based on learning and discipline, never on profit or real-money volume.
    lessons read x6 + messages (max 30) x0.5 + demo trades (max 100) x0.3 + validated referrals x10 + 10 for >=50% wins over 20 demo trades."""
    conn = sqlite3.connect(db)
    users = conn.execute("SELECT uid, level_unlock FROM users").fetchall()
    bodies = {i: b for i, b in conn.execute("SELECT id, body FROM lessons").fetchall()}
    read = {}
    for uid, lid, sec in conn.execute("SELECT uid, lesson_id, seconds FROM lesson_views").fetchall():
        if lid in bodies and sec >= lesson_required(bodies[lid]):
            read[uid] = read.get(uid, 0) + 1
    chat = dict(conn.execute("SELECT uid, COUNT(*) FROM chat GROUP BY uid").fetchall())
    demo = {u: (n or 0, w or 0, l or 0) for u, n, w, l in conn.execute(
        "SELECT uid, COUNT(*), SUM(CASE WHEN st='win' THEN 1 ELSE 0 END), SUM(CASE WHEN st='loss' THEN 1 ELSE 0 END) FROM trades WHERE mode='demo' AND st IN ('win','loss','draw') GROUP BY uid").fetchall()}
    refs = dict(conn.execute("SELECT referred_by, COUNT(*) FROM users WHERE referred_by IS NOT NULL AND ref_rewarded=1 GROUP BY referred_by").fetchall())
    conn.close()
    out = {}
    for uid, unlock in users:
        n, w, l = demo.get(uid, (0, 0, 0))
        score = read.get(uid, 0) * 6 + min(chat.get(uid, 0), 30) * 0.5 + min(n, 100) * 0.3 + refs.get(uid, 0) * 10
        if w + l >= 20 and w / (w + l) >= 0.5:
            score += 10
        computed = max(i for i, t in enumerate(LEVEL_THRESHOLDS) if score >= t)
        unlock = unlock or 0
        out[uid] = {"score": round(score, 1), "computed": computed, "unlock": unlock, "level": unlock}
    return out


def levels(db) -> dict:
    now = time.time()
    if now - _lv_cache["t"] > 15:
        _lv_cache["v"] = _compute_levels(db)
        _lv_cache["t"] = now
    return _lv_cache["v"]


def level_of(db, uid: int) -> int:
    return levels(db).get(uid, {"level": 0})["level"]


def set_level_unlock(db, uid: int, n: int):
    if not 0 <= n <= 4:
        raise ValueError("Niveau invalide (0 à 4).")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE users SET level_unlock=? WHERE uid=?", (n, uid))
    conn.commit()
    conn.close()
    _lv_cache["t"] = 0.0


# --- Super 7 : jeu de prix (en trades) ---
GAME_COOLDOWN = 20 * 3600          # un joueur qui a déjà joué attend 20 h
MYZI_SECONDS = 3600                # super prix MyziMind : 1 h de trading
# (clé, trades offerts, poids du tirage aléatoire)
GAME_PRIZES = [("3", 3, 22.0), ("6", 6, 20.0), ("9", 9, 16.0), ("12", 12, 14.0), ("15", 15, 10.0),
               ("24", 24, 8.0), ("44", 44, 5.0), ("100", 100, 2.5), ("200", 200, 1.5), ("myzi", 0, 0.5)]
GAME_KEYS = [p[0] for p in GAME_PRIZES]


def prize_label(key: str) -> str:
    if key == "myzi":
        return "MyziMind : 1 h de trading"
    if key == "0":
        return "0 trade cette fois"
    return "+%s trades" % key


def get_game_queue(db) -> list:
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT value FROM settings WHERE key='game_queue'").fetchone()
    conn.close()
    try:
        q = json.loads(row[0]) if row else []
    except ValueError:
        q = []
    return [k for k in q if k in GAME_KEYS or k == "rand"][:3]


def set_game_queue(db, queue: list):
    q = [str(k) for k in (queue or []) if str(k) in GAME_KEYS or str(k) == "rand"][:3]
    conn = sqlite3.connect(db)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('game_queue', ?)", (json.dumps(q),))
    conn.commit()
    conn.close()
    return q


def game_status(db, uid: int) -> dict:
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT game_last FROM users WHERE uid=?", (uid,)).fetchone()
    last = conn.execute("SELECT prize FROM game_plays WHERE uid=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    conn.close()
    gl = (row[0] or 0) if row else 0
    next_at = gl + GAME_COOLDOWN if gl else 0
    return {"can_play": (not gl) or time.time() >= next_at, "next_at": next_at,
            "last": ({"prize": last[0], "label": prize_label(last[0])} if last else None),
            "unlimited_until": unlimited_until(db, uid)}


def play_game(db, uid: int) -> dict:
    st = game_status(db, uid)
    if not st["can_play"]:
        left = int(st["next_at"] - time.time())
        raise ValueError("Le jeu se débloque dans %dh%02d." % (left // 3600, (left % 3600) // 60))
    queue = get_game_queue(db)
    forced = 0
    if queue:
        key = queue.pop(0)
        set_game_queue(db, queue)
        forced = 0 if key == "rand" else 1
    else:
        key = "rand"
    if key == "rand":
        key = random.choices(GAME_KEYS, weights=[p[2] for p in GAME_PRIZES])[0]
    trades = dict((p[0], p[1]) for p in GAME_PRIZES)[key]
    now = time.time()
    conn = sqlite3.connect(db)
    if trades:
        conn.execute("UPDATE users SET credits = credits + ? WHERE uid=?", (trades, uid))
    if key == "myzi":
        row = conn.execute("SELECT unlimited_until FROM users WHERE uid=?", (uid,)).fetchone()
        start = max(now, (row[0] or 0) if row else 0)
        conn.execute("UPDATE users SET unlimited_until=? WHERE uid=?", (start + MYZI_SECONDS, uid))
    conn.execute("UPDATE users SET game_last=? WHERE uid=?", (now, uid))
    conn.execute("INSERT INTO game_plays (uid, prize, forced, ts) VALUES (?, ?, ?, ?)", (uid, key, forced, now))
    conn.commit()
    conn.close()
    return {"prize": key, "label": prize_label(key), "trades": trades, "myzi": key == "myzi", "next_at": now + GAME_COOLDOWN}


def recent_plays(db, n: int = 8) -> list:
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT u.name, g.prize, g.forced, g.ts FROM game_plays g LEFT JOIN users u ON u.uid = g.uid ORDER BY g.id DESC LIMIT ?", (n,)).fetchall()
    conn.close()
    return [{"name": r[0] or "?", "label": prize_label(r[1]), "forced": bool(r[2]), "ts": r[3]} for r in rows]


def set_notice_ack(db, uid: int):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE users SET notice_ack=1 WHERE uid=?", (uid,))
    conn.commit()
    conn.close()
