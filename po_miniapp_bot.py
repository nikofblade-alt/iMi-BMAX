import os
import json
import math
import re
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
try:
    from po_live import AssetError
except ImportError:  # ancien po_live.py : le bot démarre quand même
    class AssetError(POError):
        pass
import community
import secretbox

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

# 24 paires, 8 par nature. Gains (en %) relevés dans Pocket Option, entre 92 % et 80 %.
# (nom affiché, codes possibles dans le SDK, gain en %, vérifiée)
PAIR_CANDIDATES = {
    "dev": [  # Devises
        ("EUR/USD OTC", ["EURUSD_otc"], 92, True),
        ("GBP/USD OTC", ["GBPUSD_otc"], 84, True),
        ("USD/JPY OTC", ["USDJPY_otc"], 85, True),
        ("AUD/CAD OTC", ["AUDCAD_otc"], 92, False),
        ("GBP/JPY OTC", ["GBPJPY_otc"], 92, False),
        ("AUD/CHF OTC", ["AUDCHF_otc"], 86, False),
        ("GBP/AUD OTC", ["GBPAUD_otc"], 84, False),
        ("CAD/JPY OTC", ["CADJPY_otc"], 81, False),
        ("EUR/TRY OTC", ["EURTRY_otc"], 92, False),
        ("USD/RUB OTC", ["USDRUB_otc"], 84, False),
        ("USD/BRL OTC", ["USDBRL_otc"], 92, False),
        ("AED/CNY OTC", ["AEDCNY_otc"], 92, False),
    ],
    "cry": [  # Crypto et matières premières
        ("Bitcoin OTC", ["BTCUSD_otc"], 80, True),
        ("BNB OTC", ["BNBUSD_otc", "BNB_otc"], 92, False),
        ("Polkadot OTC", ["DOTUSD_otc", "Polkadot_otc"], 92, False),
        ("Litecoin OTC", ["LTCUSD_otc", "Litecoin_otc"], 92, False),
        ("Solana OTC", ["SOLUSD_otc", "Solana_otc"], 92, False),
        ("Chainlink OTC", ["LINKUSD_otc", "Chainlink_otc"], 92, False),
        ("Brent Oil OTC", ["UKBrent_otc", "Brent_otc"], 80, False),
        ("Gold OTC", ["XAUUSD_otc", "Gold_otc"], 80, False),
        ("WTI Crude Oil OTC", ["USCrude_otc", "WTI_otc"], 80, False),
        ("Ethereum OTC", ["ETHUSD_otc"], 78, False),
    ],
    "act": [  # Actions
        ("Facebook OTC", ["FB_otc", "META_otc", "Facebook_otc"], 92, False),
        ("Microsoft OTC", ["MSFT_otc", "Microsoft_otc"], 92, False),
        ("Tesla OTC", ["TSLA_otc", "Tesla_otc"], 92, False),
        ("AMD OTC", ["AMD_otc"], 92, False),
        ("Coinbase OTC", ["COIN_otc", "Coinbase_otc"], 92, False),
        ("VISA OTC", ["V_otc", "VISA_otc"], 92, False),
        ("Johnson & Johnson OTC", ["JNJ_otc", "Johnson_Johnson_otc"], 89, False),
        ("Cisco OTC", ["CSCO_otc", "Cisco_otc"], 88, False),
        ("Alibaba OTC", ["BABA_otc", "Alibaba_otc"], 88, False),
        ("Apple OTC", ["AAPL_otc", "Apple_otc"], 86, False),
        ("Intel OTC", ["INTC_otc", "Intel_otc"], 86, False),
    ],
}
PAIRS_PER_GROUP = 8
PAIR_LIVE_PAY: dict = {}   # gain réel (en %) relevé après un trade gagné, par paire

def _norm_code(x) -> str:
    return re.sub(r"[^a-z0-9]", "", str(x).lower())

def _sdk_codes():
    """Table {code normalisé: code exact} des actifs que le SDK sait lire (None si le SDK est absent)."""
    try:
        from pocket_option.models import Asset
        table = {}
        for a in Asset:
            val = str(getattr(a, "value", a))
            table[_norm_code(val)] = val
            table.setdefault(_norm_code(getattr(a, "name", val)), val)
        return table
    except Exception as exc:
        logger.warning("Liste des paires du SDK indisponible : %s", exc)
        return None

def build_pairs() -> list:
    """Garde, dans chaque nature, les 8 premières paires que le SDK sait lire."""
    codes = _sdk_codes()
    pairs = []
    for group, cands in PAIR_CANDIDATES.items():
        found = []
        for name, aliases, pay, verified in cands:
            code = aliases[0] if codes is None else next((codes[_norm_code(a)] for a in aliases if _norm_code(a) in codes), None)
            if not code:
                logger.info("Paire absente du SDK, ignorée : %s", name)
                continue
            found.append({"n": name, "sdk": code, "g": group, "pay": pay, "verified": verified})
        if not found:  # aucune reconnue : on garde la liste (les paires refusées se retireront seules)
            found = [{"n": n, "sdk": a[0], "g": group, "pay": p, "verified": v} for n, a, p, v in cands]
        pairs += found
    return pairs

DEFAULT_PAIRS = build_pairs()

# Paires retirées automatiquement de la liste : refusées au moment de l'ordre.
# Inconnue du SDK = retirée pour toujours ; refusée par Pocket Option (fermée…) = retirée 6 h puis réessayée.
HIDDEN: dict = {}   # {code sdk: timestamp de fin du retrait}
HIDE_TEMP_SECONDS = 6 * 3600

def hide_pair(sdk: str, permanent: bool, reason: str):
    until = 4102444800.0 if permanent else time.time() + HIDE_TEMP_SECONDS
    HIDDEN[sdk] = until
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR REPLACE INTO hidden_pairs (sdk, until, reason) VALUES (?, ?, ?)", (sdk, until, reason[:200]))
        conn.commit(); conn.close()
    except Exception:
        logger.exception("Retrait de paire non enregistré")

def unhide_pair(sdk: str):
    if HIDDEN.pop(sdk, None) is None:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM hidden_pairs WHERE sdk=?", (sdk,))
        conn.commit(); conn.close()
    except Exception:
        logger.exception("Paire non réactivée")

def pairs_payload() -> list:
    """Toutes les paires avec un drapeau hide : retirées, ou au-delà des 8 premières de leur nature."""
    now, count, out = time.time(), {}, []
    for p in DEFAULT_PAIRS:
        hid = HIDDEN.get(p["sdk"], 0) > now
        if not hid:
            count[p["g"]] = count.get(p["g"], 0) + 1
            hid = count[p["g"]] > PAIRS_PER_GROUP
        out.append(dict(p, pay=PAIR_LIVE_PAY.get(p["sdk"], p["pay"]), hide=hid))
    return out

# Martingale : le gain d'un trade est d'environ 85 % de la mise.
PAYOUT = 0.85
LOSS_STREAK_STOP = 3         # un gain après au moins 3 pertes de suite arrête la séance
MAX_STAKE = 20000.0          # plafond technique de Pocket Option par ordre
BASE_LOCK_STAKE = 10.0       # la mise de départ de $10 est verrouillée tant que le solde réel est sous le seuil
BASE_LOCK_BALANCE = 1000.0

def martingale_stake(base: float, series_loss: float, balance, payout: float = PAYOUT) -> float:
    """Mise qui récupère les pertes de la série et rapporte 85 % de la mise de départ.
    Aucune limite de paliers ; seulement le solde disponible et le plafond technique."""
    stake = base if series_loss <= 0 else base + series_loss / payout
    stake = min(math.ceil(stake * 100) / 100, MAX_STAKE)
    if balance is not None and stake > balance:
        stake = math.floor(balance * 100) / 100
    return stake

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
    c.execute("CREATE TABLE IF NOT EXISTS hidden_pairs (sdk TEXT PRIMARY KEY, until REAL, reason TEXT)")
    for sdk, until in c.execute("SELECT sdk, until FROM hidden_pairs").fetchall():
        if (until or 0) > time.time():
            HIDDEN[sdk] = until
    conn.commit()
    conn.close()
    community.init(DB_PATH)
    secretbox.init(DB_PATH)
    n = secretbox.migrate(DB_PATH)
    if n:
        logger.info(f"{n} session(s) Pocket Option chiffrée(s) au repos")

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
        c.execute("SELECT name, username, group_name, blocked, affiliate_verified, po_aff_id, show_photo, photo_url, po_ssid, po_uid, ssid_updated_at, ssid_warned, bal_demo, bal_real, hist_demo, hist_real, notice_ack FROM users WHERE uid=?", (uid,))
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
            "po_ssid": secretbox.decrypt(row[8] or ""),
            "po_uid": row[9] or "",
            "ssid_updated_at": row[10] or 0.0,
            "ssid_warned": bool(row[11]),
            "bal": {"demo": row[12], "real": row[13]},
            "hist": {"demo": json.loads(row[14] or "[]"), "real": json.loads(row[15] or "[]")},
            "conn": conn_from_ssid(row[8] or ""),
            "notice_ack": bool(row[16]),
            "po": None,
            "mode": "demo",
            "p": 0,
            "dir": "call",
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
    clean = (name or f"User{uid}").strip() or f"User{uid}"
    return clean[:2] + "***"

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

    lvls = community.levels(DB_PATH)
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
            entry = {"a": mask_alias(r_name, r_uid), "lv": lvls.get(r_uid, {}).get("level", 0), "t": total, "r": rate, "p": profit, "photo": r_photo if r_show else "", "me": (r_uid == uid)}
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

    return {
        "uid": uid, "name": s["name"], "alias": mask_alias(s["name"], uid), "show_photo": s["show_photo"],
        "is_admin": is_admin(uid), "is_owner": uid in ADMIN_IDS, "user_view": uid in USER_VIEW, "affiliate_verified": s["affiliate_verified"], "po_aff_id": s["po_aff_id"],
        "mode": s["mode"], "real_ok": real_ok, "conn": s["conn"], "bal": s["bal"], "hist": s["hist"], "tr": tr,
        "pairs": pairs_payload(), "end_reason": s.get("end_reason", ""), "pstats": pstats, "p": s["p"], "dir": s["dir"], "base": s["base"],
        "dur": s["dur"], "total": s["total"], "k": s["k"], "pause_lot": s["pause_lot"],
        "running": s["running"], "paused": s["paused"], "pause_req": s["pause_req"], "stopped": s["stopped"],
        "placed": s["placed"], "next": s["next"], "net": s["net"], "trades": s["trades"], "err": s["err"],
        "lb": lb, "me": me, "min_trades": min_trades,
        "level": {"n": lvls.get(uid, {}).get("level", 0), "name": community.level_name(lvls.get(uid, {}).get("level", 0))},
        "validated": is_validated(s),
        "settings": {**community.get_settings(DB_PATH), "open": community.fmt_min(community.get_settings(DB_PATH)["open_min"]), "close": community.fmt_min(community.get_settings(DB_PATH)["close_min"])},
        "limit": community.daily_limit(DB_PATH, uid, None, admin_limits(uid)),
        "elite": community.daily_limit(DB_PATH, uid, None, admin_limits(uid))["elite"],
        "ref": community.referral_info(DB_PATH, uid, BOT_USERNAME),
        "gate": dict(zip(("ok", "reason"), community.trading_gate(DB_PATH, uid, admin_limits(uid)))),
        "game": community.game_status(DB_PATH, uid),
        "notice_ack": s["notice_ack"],
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
    series_loss = 0.0
    loss_streak = 0
    s["end_reason"] = ""
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
            except AssetError as exc:
                s["trades"].remove(trade_rec)
                s["placed"] = idx - 1
                hide_pair(pair_obj["sdk"], str(exc).startswith("Actif inconnu"), str(exc))
                s["err"] = "La paire %s n'est pas disponible pour ton compte : elle est retirée de la liste. Choisis-en une autre." % pair_obj["n"]
                break
            except Exception:
                s["trades"].remove(trade_rec)
                s["placed"] = idx - 1
                raise
            unhide_pair(pair_obj["sdk"])

            trade_rec["st"] = status
            trade_rec["pnl"] = pnl
            recovered_streak = 0
            if status == "win":
                if stake > 0 and pnl > 0 and 0.4 <= pnl / stake <= 0.95:
                    PAIR_LIVE_PAY[pair_obj["sdk"]] = round(100 * pnl / stake, 1)
                if loss_streak >= LOSS_STREAK_STOP:
                    recovered_streak = loss_streak
                series_loss = 0.0
                loss_streak = 0
            elif status == "loss":
                series_loss += (-pnl) if pnl < 0 else stake
                loss_streak += 1

            s["net"] = round(s["net"] + pnl, 2)
            if new_bal is not None:
                s["bal"][s["mode"]] = round(new_bal, 2)
            elif s["bal"][s["mode"]] is not None:
                s["bal"][s["mode"]] = round(s["bal"][s["mode"]] + pnl, 2)
            if s["bal"][s["mode"]] is not None:
                s["hist"][s["mode"]].append(s["bal"][s["mode"]])
            pay_now = PAIR_LIVE_PAY.get(pair_obj["sdk"], pair_obj["pay"]) / 100.0
            s["next"] = martingale_stake(s["base"], series_loss, s["bal"][s["mode"]], pay_now)

            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO trades (uid, mode, pair, dir, stake, pnl, st, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (uid, s["mode"], pair_obj["n"], direction, stake, pnl, trade_rec["st"], time.time()))
            c.execute("UPDATE users SET bal_demo=?, bal_real=?, hist_demo=?, hist_real=? WHERE uid=?",
                      (s["bal"]["demo"], s["bal"]["real"], json.dumps(s["hist"]["demo"][-50:]), json.dumps(s["hist"]["real"][-50:]), uid))
            conn.commit()
            conn.close()
            community.after_trade(DB_PATH, uid, admin_limits(uid))

            if recovered_streak:
                s["end_reason"] = "Série de %d pertes récupérée : séance arrêtée sur ce gain." % recovered_streak
                break

            if s["next"] < 1.0 and idx < s["total"]:
                s["err"] = "Solde insuffisant pour la prochaine mise : séance arrêtée."
                break

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
        if 0 <= idx < len(DEFAULT_PAIRS): s["p"] = idx
    if "dir" in data and data["dir"] in ("call", "put"): s["dir"] = data["dir"]
    if "base" in data:
        new_base = min(float(data["base"]), max_base)
        if new_base >= BASE_LOCK_STAKE and (s["bal"]["real"] or 0) < BASE_LOCK_BALANCE:
            return web.json_response({"error": "La mise de $10 se débloque quand ton solde réel atteint $1000."}, status=400)
        s["base"] = new_base
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
        return web.json_response({"error": "Coche la case pour confirmer que tu as lu la notice."}, status=400)
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
                 (s["po_aff_id"], secretbox.encrypt(s["po_ssid"]), s["po_uid"], now, s["bal"]["demo"], s["bal"]["real"], s["uid"]))
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
    if not s["notice_ack"]:
        return web.json_response({"error": "Lis la notice (onglet Notice) et coche la case avant de commencer."}, status=403)
    if s["base"] >= BASE_LOCK_STAKE and (s["bal"]["real"] or 0) < BASE_LOCK_BALANCE:
        return web.json_response({"error": "La mise de $10 se débloque quand ton solde réel atteint $1000. Choisis une mise plus petite."}, status=400)
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

async def handle_notice_ack(request: web.Request):
    s = await get_user_from_request(request)
    community.set_notice_ack(DB_PATH, s["uid"])
    s["notice_ack"] = True
    return web.json_response(build_state_payload(s))

async def handle_game_play(request: web.Request):
    s = await get_user_from_request(request)
    try:
        res = community.play_game(DB_PATH, s["uid"])
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response(res)

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
    if action in ("block", "affiliate", "elite", "group", "level") and is_admin(uid) and actor["uid"] not in ADMIN_IDS:
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
    elif action == "level":
        conn.close()
        try:
            community.set_level_unlock(DB_PATH, uid, int(value))
        except (TypeError, ValueError) as e:
            return web.json_response({"error": str(e) or "Niveau invalide (0 à 4)."}, status=400)
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
    lvls = community.levels(DB_PATH)
    for uid, name, username, blocked, aff, elite, sub in db_rows("SELECT uid, name, username, blocked, affiliate_verified, elite, sub_admin FROM users"):
        v = views.get(uid, [])
        out.append({
            "uid": uid, "name": name, "username": username, "blocked": bool(blocked), "validated": bool(aff) or is_admin(uid), "elite": bool(elite), "sub_admin": bool(sub), "owner": uid in ADMIN_IDS,
            "score": lvls.get(uid, {}).get("score", 0), "level": lvls.get(uid, {}).get("level", 0), "level_computed": lvls.get(uid, {}).get("computed", 0), "level_unlock": lvls.get(uid, {}).get("unlock", 0),
            "seen": len(v), "read": sum(1 for lid, sec, _ in v if sec >= req[lid]), "total": len(req),
            "seconds": int(sum(sec for _, sec, _ in v)), "last_seen": max([x[2] or 0 for x in v], default=0),
        })
    out.sort(key=lambda r: (-r["seconds"], r["name"] or ""))
    return web.json_response({"readers": out, "settings": {**community.get_settings(DB_PATH)},
                              "elite_count": community.elite_count(DB_PATH), "elite_max": community.ELITE_MAX})

async def handle_admin_game(request: web.Request):
    await check_owner(request)
    if request.method == "POST":
        data = await request.json()
        community.set_game_queue(DB_PATH, data.get("queue") or [])
    return web.json_response({"queue": community.get_game_queue(DB_PATH), "recent": community.recent_plays(DB_PATH)})

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
    app.router.add_post("/api/notice/ack", handle_notice_ack)
    app.router.add_post("/api/game/play", handle_game_play)
    app.router.add_get("/api/admin/game", handle_admin_game)
    app.router.add_post("/api/admin/game", handle_admin_game)
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
