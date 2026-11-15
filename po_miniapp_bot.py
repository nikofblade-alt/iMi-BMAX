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
from po_live import POLive, POError, parse_ssid
import community

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
# httpx logs full request URLs, which contain the Telegram bot token
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

TG_TOKEN = os.getenv("TG_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://xxx.up.railway.app")
DB_PATH = os.getenv("DB_PATH", "imi.db")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
PO_REAL_REGION = os.getenv("PO_REAL_REGION", "")
PORT = int(os.getenv("PORT", "8080"))
BOT_USERNAME = ""
SUB_ADMINS = set()  # admins added by the owner from the app (ADMIN_IDS are the owners)
USER_VIEW = set()  # admins currently previewing the app as a plain student

def is_admin(uid):
    return uid in ADMIN_IDS or uid in SUB_ADMINS

def admin_limits(uid):
    """True when uid gets the admin/élite limits (an admin not in student view)."""
    return is_admin(uid) and uid not in USER_VIEW

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
    community.init(DB_PATH)

USER_SESSIONS = {}

def conn_from_ssid(ssid: str) -> dict:
    try:
        is_demo = int(json.loads(ssid).get("isDemo", 1))
        return {"demo": is_demo == 1, "real": is_demo == 0}
    except Exception:
        return {"demo": False, "real": False}

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
            "conn": conn_from_ssid(row[8] or ""),
            "po": None,
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
    if not TG_TOKEN:
        return {}
    parsed = dict(parse_qsl(init_data))
    hash_val = parsed.pop("hash", None)
    if not hash_val:
        return {}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", TG_TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(calc_hash, hash_val):
        try:
            if time.time() - int(parsed.get("auth_date", "0")) > 7 * 86400:
                return {}
        except ValueError:
            return {}
        return json.loads(parsed.get("user", "{}"))
    return {}

def is_validated(s: dict) -> bool:
    return bool(s["affiliate_verified"]) or is_admin(s["uid"])

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
        "is_admin": is_admin(uid), "is_owner": uid in ADMIN_IDS, "user_view": uid in USER_VIEW, "affiliate_verified": s["affiliate_verified"], "po_aff_id": s["po_aff_id"],
        "mode": s["mode"], "real_ok": real_ok, "conn": s["conn"], "bal": s["bal"], "hist": s["hist"], "tr": tr,
        "pairs": DEFAULT_PAIRS, "pstats": pstats, "p": s["p"], "dir": s["dir"], "base": s["base"],
        "dur": s["dur"], "total": s["total"], "k": s["k"], "max_loss": max_loss, "pause_lot": s["pause_lot"],
        "running": s["running"], "paused": s["paused"], "pause_req": s["pause_req"], "stopped": s["stopped"],
        "placed": s["placed"], "next": s["next"], "net": s["net"], "trades": s["trades"], "err": s["err"],
        "lb": lb, "me": me, "min_trades": min_trades,
        "validated": is_validated(s),
        "settings": {**community.get_settings(DB_PATH), "open": community.fmt_min(community.get_settings(DB_PATH)["open_min"]), "close": community.fmt_min(community.get_settings(DB_PATH)["close_min"])},
        "limit": community.daily_limit(DB_PATH, uid, None, admin_limits(uid)),
        "elite": community.daily_limit(DB_PATH, uid, None, admin_limits(uid))["elite"],
        "ref": community.referral_info(DB_PATH, uid, BOT_USERNAME),
        "gate": dict(zip(("ok", "reason"), community.trading_gate(DB_PATH, uid, admin_limits(uid)))),
        "challenges": community.challenges(DB_PATH, uid)[0],
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

            ok, reason = community.trading_gate(DB_PATH, uid, admin_limits(uid))
            if not ok:
                s["err"] = reason
                break

            idx = s["placed"] + 1
            stake = round(s["next"], 2)
            pair_obj = DEFAULT_PAIRS[s["p"]]
            dur = s["dur"]
            direction = s["dir"]

            trade_rec = {"i": idx, "st": "open", "stake": stake, "pnl": 0.0, "end": time.time() + dur}
            s["trades"].append(trade_rec)
            s["placed"] = idx

            try:
                status, pnl, new_bal = await s["po"].trade(pair_obj["sdk"], direction, stake, dur)
            except Exception:
                s["trades"].remove(trade_rec)
                s["placed"] = idx - 1
                raise

            trade_rec["st"] = status
            trade_rec["pnl"] = pnl
            if status == "win":
                step_k = 0; s["next"] = s["base"]
            elif status == "loss":
                step_k += 1
                if step_k <= s["k"]: s["next"] = round(s["base"] * (2 ** step_k), 2)
                else: step_k = 0; s["next"] = s["base"]

            s["net"] = round(s["net"] + pnl, 2)
            if new_bal is not None:
                s["bal"][s["mode"]] = round(new_bal, 2)
            elif s["bal"][s["mode"]] is not None:
                s["bal"][s["mode"]] = round(s["bal"][s["mode"]] + pnl, 2)
            if s["bal"][s["mode"]] is not None:
                s["hist"][s["mode"]].append(s["bal"][s["mode"]])

            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO trades (uid, mode, pair, dir, stake, pnl, st, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (uid, s["mode"], pair_obj["n"], direction, stake, pnl, trade_rec["st"], time.time()))
            c.execute("UPDATE users SET bal_demo=?, bal_real=?, hist_demo=?, hist_real=? WHERE uid=?",
                      (s["bal"]["demo"], s["bal"]["real"], json.dumps(s["hist"]["demo"][-50:]), json.dumps(s["hist"]["real"][-50:]), uid))
            conn.commit()
            conn.close()
            community.after_trade(DB_PATH, uid, admin_limits(uid))

            if s["pause_lot"] and (idx % 2 == 0) and idx < s["total"] and not s["stopped"]:
                s["paused"] = True
    except Exception as exc:
        logger.exception("Erreur dans la boucle de trading")
        s["err"] = "Séance interrompue : %s" % exc
    finally:
        s["running"] = False

async def get_user_from_request(request: web.Request) -> dict:
    init_data = request.headers.get("X-Init-Data", "")
    tg_user = verify_init_data(init_data)
    uid = tg_user.get("id")
    if not uid:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Ouvre l'application depuis Telegram."}), content_type="application/json")
    name = f"{tg_user.get('first_name', 'Trader')} {tg_user.get('last_name', '')}".strip()
    username = tg_user.get("username", "")
    s = get_session(uid, name, username)
    if s["blocked"] and not is_admin(uid):
        raise web.HTTPForbidden(text=json.dumps({"error": "Accès bloqué par le mentor."}), content_type="application/json")
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

async def po_restore(s: dict) -> POLive:
    if not s["po_ssid"]:
        raise POError("Compte Pocket Option non connecté.")
    try:
        auth = parse_ssid(s["po_ssid"])
    except POError:
        raise POError("Session Pocket Option invalide : reconnecte ton compte.")
    live = POLive(auth)
    await live.connect()
    s["po"] = live
    return live

async def handle_connect(request: web.Request):
    s = await get_user_from_request(request)
    if not is_validated(s):
        return web.json_response({"error": "Ton accès au trading n'est pas encore validé par le mentor."}, status=403)
    data = await request.json()
    if not data.get("consent"):
        return web.json_response({"error": "Coche la case pour confirmer que tu as compris les risques."}, status=400)
    try:
        auth = parse_ssid(data.get("ssid", ""))
        live = POLive(auth)
        await live.connect()
    except POError as e:
        return web.json_response({"error": str(e)}, status=400)

    if s.get("po"):
        await s["po"].close()
    s["po"] = live
    acct = "demo" if live.is_demo else "real"
    now = time.time()
    s["po_ssid"] = json.dumps(auth)
    s["po_uid"] = str(auth["uid"])
    s["po_aff_id"] = str(auth["uid"])
    s["ssid_updated_at"] = now
    s["ssid_warned"] = False
    s["conn"] = {"demo": bool(live.is_demo), "real": not live.is_demo}
    s["mode"] = acct
    if live.balance is not None:
        s["bal"][acct] = live.balance

    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET po_aff_id=?, po_ssid=?, po_uid=?, ssid_updated_at=?, ssid_warned=0, bal_demo=?, bal_real=? WHERE uid=?",
                 (s["po_aff_id"], s["po_ssid"], s["po_uid"], now, s["bal"]["demo"], s["bal"]["real"], s["uid"]))
    conn.commit()
    conn.close()
    return web.json_response(build_state_payload(s))

async def handle_disconnect(request: web.Request):
    s = await get_user_from_request(request)
    s["stopped"] = True
    if s.get("po"):
        await s["po"].close()
        s["po"] = None
    s["po_ssid"] = ""
    s["conn"] = {"demo": False, "real": False}
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET po_ssid='' WHERE uid=?", (s["uid"],))
    conn.commit(); conn.close()
    return web.json_response(build_state_payload(s))

async def handle_start(request: web.Request):
    s = await get_user_from_request(request)
    if not is_validated(s):
        return web.json_response({"error": "Ton accès au trading n'est pas encore validé par le mentor."}, status=403)
    ok, reason = community.trading_gate(DB_PATH, s["uid"], admin_limits(s["uid"]))
    if not ok:
        return web.json_response({"error": reason}, status=403)
    if s["running"]:
        return web.json_response({"error": "Une séance est déjà en cours."}, status=400)
    if not (s["conn"]["demo"] or s["conn"]["real"]):
        return web.json_response({"error": "Compte Pocket Option non connecté."}, status=400)

    live = s.get("po")
    if live is None or not live.connected:
        try:
            live = await po_restore(s)
        except POError as e:
            return web.json_response({"error": str(e)}, status=400)
    if bool(live.is_demo) != (s["mode"] == "demo"):
        kind = "démo" if live.is_demo else "réel"
        return web.json_response({"error": "Ton SSID est celui du compte %s. Choisis ce mode, ou reconnecte avec le SSID de l'autre compte." % kind}, status=400)

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

async def check_owner(request: web.Request):
    s = await get_user_from_request(request)
    if s["uid"] not in ADMIN_IDS:
        raise web.HTTPForbidden(text=json.dumps({"error": "Réservé au propriétaire de l'app."}), content_type="application/json")
    return s

async def check_admin(request: web.Request):
    s = await get_user_from_request(request)
    if not is_admin(s["uid"]):
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
    actor = await check_admin(request)
    data = await request.json()
    try:
        uid = int(data.get("uid"))
    except (TypeError, ValueError):
        return web.json_response({"error": "uid invalide."}, status=400)
    action = data.get("action", "")
    if action in ("block", "affiliate", "elite", "group") and is_admin(uid) and actor["uid"] not in ADMIN_IDS:
        return web.json_response({"error": "Seul le propriétaire peut modifier un admin."}, status=403)
    value = data.get("value")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    sess = USER_SESSIONS.get(uid)
    reward = False
    if action == "block":
        blocked = 1 if value else 0
        c.execute("UPDATE users SET blocked=? WHERE uid=?", (blocked, uid))
        if sess:
            sess["blocked"] = bool(blocked)
            if blocked: sess["stopped"] = True
    elif action == "affiliate":
        ok = 1 if value else 0
        c.execute("UPDATE users SET affiliate_verified=? WHERE uid=?", (ok, uid))
        if sess: sess["affiliate_verified"] = bool(ok)
        reward = bool(ok)
    elif action == "subadmin":
        conn.close()
        await check_owner(request)
        if uid in ADMIN_IDS:
            return web.json_response({"error": "Le propriétaire est déjà admin."}, status=400)
        try:
            community.set_sub_admin(DB_PATH, uid, bool(value))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        SUB_ADMINS.clear(); SUB_ADMINS.update(community.sub_admin_ids(DB_PATH))
        if value:
            community.reward_referrer(DB_PATH, uid)
            if sess: sess["affiliate_verified"] = True
        else:
            USER_VIEW.discard(uid)
        return web.json_response({"ok": True})
    elif action == "reward":
        conn.close()
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = 0
        if not community.give_credits(DB_PATH, uid, n):
            return web.json_response({"error": "Quantité invalide (1 à 100) ou utilisateur inconnu."}, status=400)
        return web.json_response({"ok": True})
    elif action == "elite":
        conn.close()
        try:
            community.set_elite(DB_PATH, uid, bool(value))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        if value:
            community.reward_referrer(DB_PATH, uid)
            if sess: sess["affiliate_verified"] = True
        return web.json_response({"ok": True, "elite_count": community.elite_count(DB_PATH)})
    elif action == "group":
        grp = str(value or "Standard")
        c.execute("SELECT 1 FROM groups WHERE name=?", (grp,))
        if not c.fetchone():
            conn.close()
            return web.json_response({"error": "Groupe inconnu."}, status=400)
        c.execute("UPDATE users SET group_name=? WHERE uid=?", (grp, uid))
        if sess: sess["group"] = grp
    else:
        conn.close()
        return web.json_response({"error": "Action inconnue."}, status=400)
    conn.commit()
    conn.close()
    if reward:
        community.reward_referrer(DB_PATH, uid)
    return web.json_response({"ok": True})

async def handle_admin_group(request: web.Request):
    await check_admin(request)
    data = await request.json()
    name = str(data.get("name", "")).strip()
    if not name:
        return web.json_response({"error": "Nom de groupe requis."}, status=400)
    try:
        max_base = float(data.get("max_base") or 10.0)
        max_total = int(data.get("max_total") or 12)
        max_k = int(data.get("max_k") or 5)
    except (TypeError, ValueError):
        return web.json_response({"error": "Valeurs invalides."}, status=400)
    real_allowed = 1 if data.get("real_allowed") else 0
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO groups (name, max_base, max_total, max_k, real_allowed) VALUES (?, ?, ?, ?, ?)",
                 (name, max_base, max_total, max_k, real_allowed))
    conn.commit()
    conn.close()
    return web.json_response({"ok": True})

# --- COURS, DISCUSSION, RÉGLAGES MENTOR ---
CHAT_LAST = {}

def db_rows(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows

def db_exec(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(sql, args)
    conn.commit()
    last = cur.lastrowid
    conn.close()
    return last

async def handle_lessons(request: web.Request):
    s = await get_user_from_request(request)
    rows = db_rows("""SELECT l.id, l.title, l.body, l.ts, COALESCE(v.seconds, 0) FROM lessons l
                      LEFT JOIN lesson_views v ON v.lesson_id = l.id AND v.uid = ?
                      ORDER BY l.id ASC""", (s["uid"],))
    out = []
    for lid, title, body, ts, sec in rows:
        req = community.lesson_required(body)
        out.append({"id": lid, "title": title, "body": body, "ts": ts, "seconds": int(sec), "required": req, "read": sec >= req})
    return web.json_response({"lessons": out})

async def handle_lesson_ping(request: web.Request):
    s = await get_user_from_request(request)
    data = await request.json()
    try:
        lid = int(data.get("lesson_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "Leçon invalide."}, status=400)
    if not db_rows("SELECT 1 FROM lessons WHERE id=?", (lid,)):
        return web.json_response({"error": "Leçon introuvable."}, status=404)
    now = time.time()
    row = db_rows("SELECT seconds, last_seen FROM lesson_views WHERE uid=? AND lesson_id=?", (s["uid"], lid))
    if row:
        add = min(max(now - (row[0][1] or now), 0), 15)
        db_exec("UPDATE lesson_views SET seconds=seconds+?, last_seen=? WHERE uid=? AND lesson_id=?", (add, now, s["uid"], lid))
        seconds = row[0][0] + add
    else:
        db_exec("INSERT INTO lesson_views (uid, lesson_id, seconds, first_seen, last_seen) VALUES (?, ?, 0, ?, ?)", (s["uid"], lid, now, now))
        seconds = 0
    return web.json_response({"seconds": int(seconds)})

async def handle_chat_get(request: web.Request):
    s = await get_user_from_request(request)
    if not is_validated(s):
        return web.json_response({"error": "La discussion est réservée aux élèves validés."}, status=403)
    try:
        after = int(request.query.get("after", "0"))
    except ValueError:
        after = 0
    rows = db_rows("SELECT id, uid, name, text, is_admin, ts FROM chat WHERE id>? ORDER BY id DESC LIMIT 100", (after,))
    msgs = [{"id": r[0], "me": r[1] == s["uid"], "name": r[2], "text": r[3], "mentor": bool(r[4]), "ts": r[5]} for r in reversed(rows)]
    return web.json_response({"messages": msgs})

async def handle_chat_post(request: web.Request):
    s = await get_user_from_request(request)
    if not is_validated(s):
        return web.json_response({"error": "La discussion est réservée aux élèves validés."}, status=403)
    data = await request.json()
    text = str(data.get("text", "")).strip()[:500]
    if not text:
        return web.json_response({"error": "Message vide."}, status=400)
    now = time.time()
    if now - CHAT_LAST.get(s["uid"], 0) < 3:
        return web.json_response({"error": "Attends quelques secondes entre deux messages."}, status=429)
    CHAT_LAST[s["uid"]] = now
    db_exec("INSERT INTO chat (uid, name, text, is_admin, ts) VALUES (?, ?, ?, ?, ?)",
            (s["uid"], s["name"], text, 1 if is_admin(s["uid"]) else 0, now))
    return web.json_response({"ok": True})

async def handle_admin_settings(request: web.Request):
    await check_admin(request)
    data = await request.json()
    try:
        community.set_settings(DB_PATH, data)
    except (ValueError, TypeError) as e:
        return web.json_response({"error": str(e) or "Valeur invalide."}, status=400)
    return web.json_response({"ok": True})

async def handle_admin_lesson(request: web.Request):
    await check_admin(request)
    data = await request.json()
    action = data.get("action")
    if action == "add":
        title = str(data.get("title", "")).strip()[:120]
        body = str(data.get("body", "")).strip()[:20000]
        if not title or not body:
            return web.json_response({"error": "Titre et texte requis."}, status=400)
        db_exec("INSERT INTO lessons (title, body, ts) VALUES (?, ?, ?)", (title, body, time.time()))
    elif action == "delete":
        lid = int(data.get("id", 0))
        db_exec("DELETE FROM lessons WHERE id=?", (lid,))
        db_exec("DELETE FROM lesson_views WHERE lesson_id=?", (lid,))
    else:
        return web.json_response({"error": "Action inconnue."}, status=400)
    return web.json_response({"ok": True})

async def handle_admin_reading(request: web.Request):
    await check_admin(request)
    lessons = db_rows("SELECT id, body FROM lessons")
    req = {lid: community.lesson_required(body) for lid, body in lessons}
    views = {}
    for uid, lid, sec, last in db_rows("SELECT uid, lesson_id, seconds, last_seen FROM lesson_views"):
        if lid in req:
            views.setdefault(uid, []).append((lid, sec, last))
    out = []
    for uid, name, username, blocked, aff, elite, sub in db_rows("SELECT uid, name, username, blocked, affiliate_verified, elite, sub_admin FROM users"):
        v = views.get(uid, [])
        out.append({
            "uid": uid, "name": name, "username": username, "blocked": bool(blocked), "validated": bool(aff) or is_admin(uid), "elite": bool(elite), "sub_admin": bool(sub), "owner": uid in ADMIN_IDS,
            "seen": len(v), "read": sum(1 for lid, sec, _ in v if sec >= req[lid]), "total": len(req),
            "seconds": int(sum(sec for _, sec, _ in v)), "last_seen": max([x[2] or 0 for x in v], default=0),
        })
    out.sort(key=lambda r: (-r["seconds"], r["name"] or ""))
    return web.json_response({"readers": out, "settings": {**community.get_settings(DB_PATH)},
                              "elite_count": community.elite_count(DB_PATH), "elite_max": community.ELITE_MAX})

async def handle_admin_view(request: web.Request):
    s = await get_user_from_request(request)
    await check_admin(request)
    data = await request.json()
    if data.get("user_view"):
        USER_VIEW.add(s["uid"])
    else:
        USER_VIEW.discard(s["uid"])
    return web.json_response({"ok": True, "user_view": s["uid"] in USER_VIEW})

async def handle_admin_team(request: web.Request):
    await check_admin(request)
    return web.json_response({"team": community.team_report(DB_PATH), "max": community.SUB_ADMIN_MAX})

async def handle_energy_exchange(request: web.Request):
    s = await get_user_from_request(request)
    data = await request.json()
    try:
        amount = int(data.get("amount"))
    except (TypeError, ValueError):
        return web.json_response({"error": "Quantité invalide."}, status=400)
    if not community.exchange_energy(DB_PATH, s["uid"], amount):
        return web.json_response({"error": "Pas assez d'énergies."}, status=400)
    return web.json_response({"ok": True, "ref": community.referral_info(DB_PATH, s["uid"], BOT_USERNAME)})

async def handle_admin_chat_delete(request: web.Request):
    await check_admin(request)
    data = await request.json()
    db_exec("DELETE FROM chat WHERE id=?", (int(data.get("id", 0)),))
    return web.json_response({"ok": True})

# --- BOT TELEGRAM ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_session(user.id, user.first_name or "Trader", user.username or "")
    arg = (context.args[0] if context.args else "")
    if arg.startswith("ref_") and arg[4:].isdigit():
        community.register_referral(DB_PATH, user.id, int(arg[4:]))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Ouvrir iMi BMAX", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await update.message.reply_text(
        f"Bienvenue {user.first_name or ''} sur iMi BMAX. Appuie sur le bouton pour ouvrir l'application.",
        reply_markup=kb,
    )

def make_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_state)
    app.router.add_post("/api/config", handle_config)
    app.router.add_post("/api/connect", handle_connect)
    app.router.add_post("/api/disconnect", handle_disconnect)
    app.router.add_post("/api/start", handle_start)
    app.router.add_post("/api/pause", handle_pause)
    app.router.add_post("/api/stop", handle_stop)
    app.router.add_get("/api/admin/users", handle_admin_users)
    app.router.add_post("/api/admin/user", handle_admin_user_action)
    app.router.add_post("/api/admin/group", handle_admin_group)
    app.router.add_get("/api/lessons", handle_lessons)
    app.router.add_post("/api/lesson/ping", handle_lesson_ping)
    app.router.add_get("/api/chat", handle_chat_get)
    app.router.add_post("/api/chat", handle_chat_post)
    app.router.add_post("/api/admin/settings", handle_admin_settings)
    app.router.add_post("/api/admin/lesson", handle_admin_lesson)
    app.router.add_get("/api/admin/reading", handle_admin_reading)
    app.router.add_get("/api/admin/team", handle_admin_team)
    app.router.add_post("/api/admin/view", handle_admin_view)
    app.router.add_post("/api/energy/exchange", handle_energy_exchange)
    app.router.add_post("/api/admin/chat/delete", handle_admin_chat_delete)
    return app

async def main():
    global BOT_USERNAME
    init_db()
    SUB_ADMINS.update(community.sub_admin_ids(DB_PATH))

    runner = web.AppRunner(make_web_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info(f"Serveur web démarré sur le port {PORT} (base: {DB_PATH})")

    tg_app = None
    if TG_TOKEN:
        tg_app = Application.builder().token(TG_TOKEN).build()
        tg_app.add_handler(CommandHandler("start", cmd_start))
        await tg_app.initialize()
        BOT_USERNAME = tg_app.bot.username or ""
        await tg_app.start()
        await tg_app.updater.start_polling()
        logger.info("Bot Telegram démarré")
    else:
        logger.error("TG_TOKEN manquant: le bot Telegram n'est pas démarré")

    watchdog = asyncio.create_task(ssid_watchdog_loop(tg_app))
    try:
        await asyncio.Event().wait()
    finally:
        watchdog.cancel()
        if tg_app:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
