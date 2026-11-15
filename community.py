"""Settings, daily limits, trading hours, lessons and challenges (stored in the same SQLite file)."""
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Africa/Lubumbashi")  # heure de Goma (UTC+2)
DEFAULTS = {"trading_enabled": "1", "open_min": "0", "close_min": "0", "base_daily": "5", "notice": ""}


def init(db):
    conn = sqlite3.connect(db)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, body TEXT, ts REAL)")
    c.execute("""CREATE TABLE IF NOT EXISTS lesson_views (
        uid INTEGER, lesson_id INTEGER, seconds REAL DEFAULT 0, first_seen REAL, last_seen REAL,
        PRIMARY KEY (uid, lesson_id))""")
    c.execute("CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, name TEXT, text TEXT, is_admin INTEGER DEFAULT 0, ts REAL)")
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


def daily_limit(db, uid: int, settings: dict = None) -> dict:
    settings = settings or get_settings(db)
    _, bonus = challenges(db, uid)
    conn = sqlite3.connect(db)
    used = conn.execute("SELECT COUNT(*) FROM trades WHERE uid=? AND ts>=?", (uid, day_start_ts())).fetchone()[0]
    conn.close()
    limit = settings["base_daily"] + bonus
    return {"used": used, "base": settings["base_daily"], "bonus": bonus, "limit": limit, "remaining": max(0, limit - used)}


def trading_gate(db, uid: int):
    s = get_settings(db)
    if not s["trading_enabled"]:
        return False, "Le trading est suspendu par le mentor." + ((" " + s["notice"]) if s["notice"] else "")
    if not window_open(s):
        return False, "Trading fermé. Ouvert de %s à %s (heure de Goma)." % (fmt_min(s["open_min"]), fmt_min(s["close_min"]))
    lim = daily_limit(db, uid, s)
    if lim["remaining"] <= 0:
        return False, "Limite de %d trades atteinte pour aujourd'hui. Relève des défis pour en débloquer plus." % lim["limit"]
    return True, ""
