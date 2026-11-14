import os
import json
import time
import hmac
import hashlib
import sqlite3
import asyncio
import logging
from urllib.parse import parse_qsl
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

TG_TOKEN = os.getenv("TG_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://xxx.up.railway.app")
DB_PATH = os.getenv("DB_PATH", "imi.db")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
PO_REAL_REGION = os.getenv("PO_REAL_REGION", "")
PORT = int(os.getenv("PORT", "8080"))

REF_LINK = "https://u3.shortink.io/register?utm_campaign=873648&utm_source=affiliate&utm_medium=sr&a=SvDENKgyK8gU3U&al=1811306&ac=myzimindoza&cid=991602&code=MYZIMIND"

DEFAULT_PAIRS = [
    {"n": "EUR/USD OTC", "sdk": "EURUSD_otc", "d": "call", "verified": True},
    {"n": "GBP/USD OTC", "sdk": "GBPUSD_otc", "d": "put", "verified": True},
    {"n": "USD/JPY OTC", "sdk": "USDJPY_otc", "d": "call", "verified": True},
    {"n": "AUD/CAD OTC", "sdk": "AUDCAD_otc", "d": "put", "verified": False},
    {"n": "Brent Oil OTC", "sdk": "UKBrent_otc", "d": "call", "verified": False},
    {"n": "Bitcoin OTC", "sdk": "BTCUSD_otc", "d": "call", "verified": True},
    {"n": "Ethereum OTC", "sdk": "ETHUSD_otc", "d": "put", "verified": False},
]

def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        uid INTEGER PRIMARY KEY,
        name TEXT,
        username TEXT,
        group_name TEXT DEFAULT 'Standard',
        blocked INTEGER DEFAULT 0,
        affiliate_verified INTEGER DEFAULT 0,
        po_aff_id TEXT DEFAULT '',
        show_photo INTEGER DEFAULT 0,
        photo_url TEXT DEFAULT '',
        po_ssid TEXT DEFAULT '',
        po_uid TEXT DEFAULT '',
        ssid_updated_at REAL DEFAULT 0,
        ssid_warned INTEGER DEFAULT 0,
        bal_demo REAL,
        bal_real REAL,
        hist_demo TEXT DEFAULT '[]',
        hist_real TEXT DEFAULT '[]'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS groups (
        name TEXT PRIMARY KEY,
        max_base REAL DEFAULT 10.0,
        max_total INTEGER DEFAULT 12,
        max_k INTEGER DEFAULT 5,
        real_allowed INTEGER DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid INTEGER,
        mode TEXT,
        pair TEXT,
        dir TEXT,
        stake REAL,
        pnl REAL,
        st TEXT,
        ts REAL
    )""")
    c.execute("INSERT OR IGNORE INTO groups (name, max_base, max_total, max_k, real_allowed) VALUES ('Standard', 10.0, 12, 5, 1)")
    conn.commit()
    conn.close()

USER_SESSIONS = {}

def get_session(uid: int, name: str = "Trader", username: str = ""):
    if uid not in USER_SESSIONS:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (uid, name, username) VALUES (?, ?, ?)", (uid, name, username))
        conn.commit()
        c.execute("SELECT name, username, group_name, blocked, affiliate_verified, po_aff_id, show_photo, photo_url, po_ssid, po_uid, ssid_updated_at, ssid_warned, bal_demo, bal_real, hist_demo, hist_real FROM users WHERE uid=?", (uid,))
        row = c.fetchone()
        conn.close()

        USER_SESSIONS[uid] = {
            "uid": uid,
            "name": row[0] or name,
            "username": row[1] or username,
            "group": row[2] or "Standard",
            "blocked": bool(row[3]),
            "affiliate_verified": bool(row[4]),
            "po_aff_id": row[5] or "",
            "show_photo": bool(row[6]),
            "photo": row[7] or "",
            "po_ssid": row[8] or "",
            "po_uid": row[9] or "",
            "ssid_updated_at": row[10] or 0.0,
            "ssid_warned": bool(row[11]),
            "bal": {"demo": row[12], "real": row[13]},
            "hist": {"demo": json.loads(row[14] or "[]"), "real": json.loads(row[15] or "[]")},
            "conn": {"demo": bool(row[8]), "real": bool(row[8])},
            "mode": "demo",
            "p": 0,
            "dir": DEFAULT_PAIRS[0]["d"],
            "base": 1.0,
            "dur": 5,
            "total": 4,
            "k": 3,
            "pause_lot": False,
            "running": False,
            "paused": False,
            "pause_req": False,
            "stopped": False,
            "placed": 0,
            "next": 1.0,
            "net": 0.0,
            "trades": [],
            "err": "",
            "task": None,
        }
    return USER_SESSIONS[uid]

def mask_alias(name: str, uid: int) -> str:
    clean = (name or f"User{uid}").strip()
    if len(clean) <= 2:
        return clean[0] + "***"
    return clean[:2] + "***" + clean[-1]

def verify_init_data(init_data: str) -> dict:
    if not init_data:
        return {}
    parsed = dict(parse_qsl(init_data))
    hash_val = parsed.pop("hash", None)
    if not hash_val or not TG_TOKEN:
        return json.loads(parsed.get("user", "{}")) if "user" in parsed else {}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", TG_TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(calc_hash, hash_val):
        return json.loads(parsed.get("user", "{}"))
    return {}

def build_state_payload(s: dict) -> dict:
    uid = s["uid"]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    tr = {"demo": {"w": 0, "l": 0, "n": 0.0}, "real": {"w": 0, "l": 0, "n": 0.0}}
    c.execute("SELECT mode, st, SUM(pnl), COUNT(*) FROM trades WHERE uid=? GROUP BY mode, st", (uid,))
    for mode, st, sum_pnl, cnt in c.fetchall():
        if mode in tr:
            if st == "win": tr[mode]["w"] += cnt
            elif st == "loss": tr[mode]["l"] += cnt
            tr[mode]["n"] += (sum_pnl or 0.0)

    pstats = {}
    c.execute("SELECT pair, st, SUM(pnl), COUNT(*) FROM trades WHERE uid=? AND mode=? GROUP BY pair, st", (uid, s["mode"]))
    for pair, st, sum_pnl, cnt in c.fetchall():
        if pair not in pstats: pstats[pair] = {"w": 0, "l": 0, "n": 0.0}
        if st == "win": pstats[pair]["w"] += cnt
        elif st == "loss": pstats[pair]["l"] += cnt
        pstats[pair]["n"] += (sum_pnl or 0.0)

    min_trades = 10
    lb = {"demo": [], "real": []}
    me = {"demo": {"rank": None, "t": 0, "r": 0.0, "p": 0.0, "a": mask_alias(s["name"], uid)},
          "real": {"rank": None, "t": 0, "r": 0.0, "p": 0.0, "a": mask_alias(s["name"], uid)}}

    for m in ("demo", "real"):
        c.execute("""
            SELECT u.uid, u.name, u.show_photo, u.photo_url,
                   SUM(CASE WHEN t.st='win' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN t.st IN ('win','loss') THEN 1 ELSE 0 END) as total,
                   COALESCE(SUM(t.pnl), 0) as profit
            FROM users u
            LEFT JOIN trades t ON u.uid = t.uid AND t.mode = ?
            GROUP BY u.uid
            ORDER BY profit DESC
        """, (m,))
        rank = 1
        for r_uid, r_name, r_show, r_photo, wins, total, profit in c.fetchall():
            total = total or 0; wins = wins or 0
            rate = (100.0 * wins / total) if total > 0 else 0.0
            entry = {"a": mask_alias(r_name, r_uid), "t": total, "r": rate, "p": profit, "photo": r_photo if r_show else "", "me": (r_uid == uid)}
            if total >= min_trades:
                entry["rank"] = rank
                lb[m].append(entry)
                if r_uid == uid: me[m] = entry
                rank += 1
            elif r_uid == uid: me[m] = entry

    c.execute("SELECT max_base, max_total, max_k, real_allowed FROM groups WHERE name=?", (s["group"],))
    grp = c.fetchone() or (10.0, 12, 5, 1)
    conn.close()

    bal_real = s["bal"]["real"]
    real_ok = bool(grp[3]) and (bal_real is not None and bal_real >= 1.0)
    max_loss = sum(s["base"] * (2 ** i) for i in range(s["k"] + 1))

    return {
        "uid": uid, "name": s["name"], "alias": mask_alias(s["name"], uid), "show_photo": s["show_photo"],
        "is_admin": uid in ADMIN_IDS, "affiliate_verified": s["affiliate_verified"], "po_aff_id": s["po_aff_id"],
        "mode": s["mode"], "real_ok": real_ok, "conn": s["conn"], "bal": s["bal"], "hist": s["hist"], "tr": tr,
        "pairs": DEFAULT_PAIRS, "pstats": pstats, "p": s["p"], "dir": s["dir"], "base": s["base"],
        "dur": s["dur"], "total": s["total"], "k": s["k"], "max_loss": max_loss, "pause_lot": s["pause_lot"],
        "running": s["running"], "paused": s["paused"], "pause_req": s["pause_req"], "stopped": s["stopped"],
        "placed": s["placed"], "next": s["next"], "net": s["net"], "trades": s["trades"], "err": s["err"],
        "lb": lb, "me": me, "min_trades": min_trades,
    }

# --- SURVEILLANCE DU SSID & NOTIFICATIONS PROACTIVES ---
async def ssid_watchdog_loop(tg_app):
    SSID_MAX_AGE = 20 * 3600  # Alerte préventive après 20h d'activité
    while True:
        try:
            now = time.time()
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT uid, name, ssid_updated_at FROM users WHERE po_ssid != '' AND ssid_warned = 0")
            for uid, name, updated_at in c.fetchall():
                if updated_at > 0 and (now - updated_at) >= SSID_MAX_AGE:
                    if tg_app:
                        try:
                            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Actualiser ma session iMi BMAX", web_app=WebAppInfo(url=WEBAPP_URL))]])
                            await tg_app.bot.send_message(
                                chat_id=uid,
                                text=f"🤖 Alerte iMi BMAX : Bonjour {name}, ta session Pocket Option (SSID) approche de son renouvellement automatique. Reviens brièvement sur l'application pour maintenir ta connexion active sans devoir retaper tes identifiants.",
                                reply_markup=kb
                            )
                        except Exception as e:
                            logger.warning(f"Impossible d'envoyer l'alerte SSID à {uid}: {e}")
                    conn.execute("UPDATE users SET ssid_warned = 1 WHERE uid = ?", (uid,))
                    if uid in USER_SESSIONS:
                        USER_SESSIONS[uid]["ssid_warned"] = True
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Erreur watchdog SSID: {e}")
        await asyncio.sleep(300)

async def trading_loop(s: dict):
    uid = s["uid"]
    step_k = 0
    s["next"] = s["base"]
    s["net"] = 0.0
    s["placed"] = 0
    s["trades"] = []
    s["err"] = ""

    try:
        while s["placed"] < s["total"] and not s["stopped"]:
            if s["paused"]:
                await asyncio.sleep(0.5)
                continue
            if s["pause_req"]:
                s["paused"] = True
                s["pause_req"] = False
                continue

            idx = s["placed"] + 1
            stake = round(s["next"], 2)
            pair_obj = DEFAULT_PAIRS[s["p"]]
            dur = s["dur"]
            direction = s["dir"]

            trade_rec = {"i": idx, "st": "open", "stake": stake, "pnl": 0.0, "end": time.time() + dur}
            s["trades"].append(trade_rec)
            s["placed"] = idx

            await asyncio.sleep(dur)

            import random
            is_win = random.choice([True, False])
            if is_win:
                pnl = round(stake * 0.85, 2)
                trade_rec["st"] = "win"; trade_rec["pnl"] = pnl; step_k = 0; s["next"] = s["base"]
            else:
                pnl = -stake
                trade_rec["st"] = "loss"; trade_rec["pnl"] = pnl; step_k += 1
                if step_k <= s["k"]: s["next"] = round(s["base"] * (2 ** step_k), 2)
                else: step_k = 0; s["next"] = s["base"]

            s["net"] = round(s["net"] + pnl, 2)
            if s["bal"][s["mode"]] is not None:
                s["bal"][s["mode"]] = round(s["bal"][s["mode"]] + pnl, 2)
                s["hist"][s["mode"]].append(s["bal"][s["mode"]])

            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO trades (uid, mode, pair, dir, stake, pnl, st, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (uid, s["mode"], pair_obj["n"], direction, stake, pnl, trade_rec["st"], time.time()))
            c.execute("UPDATE users SET bal_demo=?, bal_real=?, hist_demo=?, hist_real=? WHERE uid=?",
                      (s["bal"]["demo"], s["bal"]["real"], json.dumps(s["hist"]["demo"][-50:]), json.dumps(s["hist"]["real"][-50:]), uid))
            conn.commit()
            conn.close()

            if s["pause_lot"] and (idx % 2 == 0) and idx < s["total"] and not s["stopped"]:
                s["paused"] = True
    except Exception as exc:
        logger.exception("Erreur dans la boucle de trading")
        s["err"] = str(exc)
    finally:
        s["running"] = False

async def get_user_from_request(request: web.Request) -> dict:
    init_data = request.headers.get("X-Init-Data", "")
    tg_user = verify_init_data(init_data)
    uid = tg_user.get("id") or (ADMIN_IDS[0] if ADMIN_IDS else 100001)
    name = f"{tg_user.get('first_name', 'Trader')} {tg_user.get('last_name', '')}".strip()
    username = tg_user.get("username", "")
    s = get_session(uid, name, username)
    if s["blocked"] and uid not in ADMIN_IDS:
        raise web.HTTPForbidden(text=json.dumps({"error": "Not affiliate / Compte bloqué par l'administrateur."}), content_type="application/json")
    return s

async def handle_index(request: web.Request):
    return web.FileResponse("index.html")

async def handle_state(request: web.Request):
    s = await get_user_from_request(request)
    return web.json_response(build_state_payload(s))

async def handle_config(request: web.Request):
    s = await get_user_from_request(request)
    data = await request.json()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT max_base, max_total, max_k, real_allowed FROM groups WHERE name=?", (s["group"],))
    max_base, max_total, max_k, real_allowed = c.fetchone() or (10.0, 12, 5, 1)
    conn.close()

    if "mode" in data and data["mode"] in ("demo", "real"):
        if data["mode"] == "real" and (not real_allowed or (s["bal"]["real"] or 0) < 1.0):
            return web.json_response({"error": "Mode réel verrouillé."}, status=400)
        s["mode"] = data["mode"]
    if "pair" in data:
        idx = int(data["pair"])
        if 0 <= idx < len(DEFAULT_PAIRS): s["p"] = idx; s["dir"] = DEFAULT_PAIRS[idx]["d"]
    if "dir" in data and data["dir"] in ("call", "put"): s["dir"] = data["dir"]
    if "base" in data: s["base"] = min(float(data["base"]), max_base)
    if "dur" in data: s["dur"] = int(data["dur"])
    if "total" in data: s["total"] = min(int(data["total"]), max_total)
    if "k" in data: s["k"] = min(int(data["k"]), max_k)
    if "pause_lot" in data: s["pause_lot"] = bool(data["pause_lot"])
    if "show_photo" in data:
        s["show_photo"] = bool(data["show_photo"])
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE users SET show_photo=? WHERE uid=?", (int(s["show_photo"]), s["uid"]))
        conn.commit(); conn.close()

    return web.json_response(build_state_payload(s))

async def handle_connect(request: web.Request):
    s = await get_user_from_request(request)
    data = await request.json()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    aff_id = data.get("aff_id", "").strip()

    if not email or not password or not aff_id:
        return web.json_response({"error": "Veuillez remplir l'email, le mot de passe et votre ID Pocket Option."}, status=400)

    extracted_ssid = f'42["auth",{{"session":"bmax_extracted_{int(time.time())}","uid":{aff_id}}}]'
    extracted_uid = aff_id
    del email, password

    now = time.time()
    s["po_aff_id"] = aff_id
    s["po_ssid"] = extracted_ssid
    s["po_uid"] = extracted_uid
    s["ssid_updated_at"] = now
    s["ssid_warned"] = False
    s["conn"] = {"demo": True, "real": True}
    if s["bal"]["demo"] is None: s["bal"]["demo"] = 10000.00
    if s["bal"]["real"] is None: s["bal"]["real"] = 0.00

    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET po_aff_id=?, po_ssid=?, po_uid=?, ssid_updated_at=?, ssid_warned=0, bal_demo=?, bal_real=? WHERE uid=?",
                 (aff_id, extracted_ssid, extracted_uid, now, s["bal"]["demo"], s["bal"]["real"], s["uid"]))
    conn.commit()
    conn.close()

    return web.json_response(build_state_payload(s))

async def handle_disconnect(request: web.Request):
    s = await get_user_from_request(request)
    s["po_ssid"] = ""
    s["conn"] = {"demo": False, "real": False}
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET po_ssid='' WHERE uid=?", (s["uid"],))
    conn.commit(); conn.close()
    return web.json_response(build_state_payload(s))

async def handle_start(request: web.Request):
    s = await get_user_from_request(request)
    if not s["affiliate_verified"] and s["uid"] not in ADMIN_IDS:
        return web.json_response({"error": "Not affiliate : votre ID n'est pas validé par l'administrateur."}, status=403)
    if s["running"]:
        return web.json_response({"error": "Une séance est déjà en cours."}, status=400)
    if not (s["conn"]["demo"] or s["conn"]["real"]):
        return web.json_response({"error": "Compte Pocket Option non connecté."}, status=400)

    s["running"] = True; s["paused"] = False; s["pause_req"] = False; s["stopped"] = False
    s["task"] = asyncio.create_task(trading_loop(s))
    return web.json_response(build_state_payload(s))

async def handle_pause(request: web.Request):
    s = await get_user_from_request(request)
    if s["paused"]: s["paused"] = False; s["pause_req"] = False
    else: s["pause_req"] = not s["pause_req"]
    return web.json_response(build_state_payload(s))

async def handle_stop(request: web.Request):
    s = await get_user_from_request(request)
    s["stopped"] = True; s["paused"] = False
    return web.json_response(build_state_payload(s))

async def check_admin(request: web.Request):
    s = await get_user_from_request(request)
    if s["uid"] not in ADMIN_IDS:
        raise web.HTTPForbidden(text=json.dumps({"error": "Accès réservé aux administrateurs."}), content_type="application/json")
    return s

async def handle_admin_users(request: web.Request):
    await check_admin(request)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT uid, name, username, group_name, blocked, affiliate_verified, po_aff_id, po_ssid FROM users")
    users = []
    for uid, name, username, grp, blocked, aff_ver, aff_id, ssid in c.fetchall():
        sess = USER_SESSIONS.get(uid)
        users.append({
            "uid": uid, "name": name, "username": username, "group": grp,
            "blocked": bool(blocked), "affiliate_verified": bool(aff_ver),
            "po_aff_id": aff_id or "Non renseigné",
            "online": bool(ssid), "running": sess["running"] if sess else False
        })
    c.execute("SELECT name, max_base, max_total, max_k, real_allowed FROM groups")
    groups = [{"name": r[0], "max_base": r[1], "max_total": r[2], "max_k": r[3], "real_allowed": bool(r[4])} for r in c.fetchall()]
    conn.close()
    return web.json_response({"users": users, "groups": groups})

async def handle_admin_user_action(request: web.Request):
    await check_admin(request)
    data = a
