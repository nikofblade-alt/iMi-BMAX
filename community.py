"""Settings, daily limits, trading hours, lessons and challenges (stored in the same SQLite file)."""
import sqlite3
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
    cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    for name, ddl in (("elite", "INTEGER DEFAULT 0"), ("referred_by", "INTEGER"), ("ref_rewarded", "INTEGER DEFAULT 0"),
                      ("energy", "INTEGER DEFAULT 0"), ("credits", "INTEGER DEFAULT 0")):
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


def daily_limit(db, uid: int, settings: dict = None, force_elite: bool = False) -> dict:
    settings = settings or get_settings(db)
    elite, _, credits = user_flags(db, uid)
    elite = elite or force_elite
    if elite:
        base, bonus = settings["elite_daily"], 0
    else:
        base, (_, bonus) = settings["base_daily"], challenges(db, uid)
    conn = sqlite3.connect(db)
    used = conn.execute("SELECT COUNT(*) FROM trades WHERE uid=? AND ts>=?", (uid, day_start_ts())).fetchone()[0]
    conn.close()
    limit = base + bonus
    return {"used": used, "base": base, "bonus": bonus, "limit": limit, "credits": credits, "elite": elite,
            "remaining": max(0, limit - used) + credits}


def trading_gate(db, uid: int, force_elite: bool = False):
    s = get_settings(db)
    if not s["trading_enabled"]:
        return False, "Le trading est suspendu par le mentor." + ((" " + s["notice"]) if s["notice"] else "")
    if not window_open(s):
        return False, "Trading fermé. Ouvert de %s à %s (heure de Goma)." % (fmt_min(s["open_min"]), fmt_min(s["close_min"]))
    lim = daily_limit(db, uid, s, force_elite)
    if lim["remaining"] <= 0:
        hint = "" if lim["elite"] else " Relève des défis, ou échange des énergies de parrainage, pour en débloquer plus."
        return False, "Limite de %d trades atteinte pour aujourd'hui.%s" % (lim["limit"], hint)
    return True, ""


def after_trade(db, uid: int, force_elite: bool = False):
    """Call after a trade row is inserted: beyond the daily limit, a trade uses one credit."""
    lim = daily_limit(db, uid, None, force_elite)
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
