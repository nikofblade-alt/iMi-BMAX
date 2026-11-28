"""Tendance récente des paires, calculée à partir des prix reçus du SDK Pocket Option.

C'est une lecture de la direction des dernières minutes (indicatif) : elle ne prédit pas le prochain trade.
Rien ici ne doit jamais gêner le trading : toute erreur est absorbée.
"""
import logging
import math
import re
import time
from collections import deque

logger = logging.getLogger(__name__)

TFS = (('c', 5, 40), ('m', 10, 40), ('l', 30, 40))   # (durée, secondes par bougie, nombre de bougies) : court, moyen, long
TF_WEIGHT = {'c': 0.3, 'm': 0.4, 'l': 0.3}
MIN_CANDLES = 20    # bougies nécessaires pour lire une durée
STALE_AFTER = 45    # sans prix depuis 45 s : tendance indisponible


def _norm(code) -> str:
    c = str(getattr(code, "value", code)).lower().lstrip("#")
    c = re.sub(r"_otc$", "", c)
    return re.sub(r"[^a-z0-9]", "", c)


class TrendTracker:
    Z_VOTE = 1.2      # force minimale de la pente pour voter
    GAP_K = 0.25      # écart moyennes mobiles / bruit
    MACD_K = 0.10
    POS_K = 0.25
    TF_DIR = 0.6      # score minimal pour qu'une durée compte comme haussière/baissière
    AGG_MIN = 0.80    # score global minimal
    RSI_EXT = 78

    def __init__(self):
        self.ticks = {}        # code normalisé -> deque[(ts, prix)]
        self.codes = {}        # code normalisé -> code SDK d'origine
        self.owner = None      # la connexion qui alimente les prix
        self.samples_logged = 0
        self._cache = {}
        self.received = 0
        self.events = 0          # évènements reçus du SDK (même si illisibles)
        self.last_raw = ""       # dernier évènement brut (pour le diagnostic admin)
        self.subscribed = 0
        self.events_by = {}

    def register(self, sdk_codes):
        for c in sdk_codes:
            self.codes[_norm(c)] = c

    # ---------- réception ----------
    def add(self, code, price, ts=None):
        key = _norm(code)
        if key not in self.codes:
            return
        try:
            price = float(price)
        except (TypeError, ValueError):
            return
        if not math.isfinite(price) or price <= 0:
            return
        dq = self.ticks.setdefault(key, deque(maxlen=5000))
        dq.append((ts or time.time(), price))
        self.received += 1

    def feed(self, items, source="?"):
        """Reçoit les données d'un évènement du SDK (format inconnu : lecture tolérante, à plusieurs niveaux)."""
        self.events += 1
        self.events_by[source] = self.events_by.get(source, 0) + 1
        self.last_raw = "[%s] %s" % (source, repr(items)[:400])
        if self.samples_logged < 3:
            self.samples_logged += 1
            logger.info("TENDANCE exemple (%s) : %s", source, repr(items)[:300])
        self._walk(items, None, 0)

    @staticmethod
    def _num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def _code_in(self, values):
        for v in values:
            c = getattr(v, "value", v)
            if isinstance(c, str) and _norm(c) in self.codes:
                return c
        return None

    @staticmethod
    def _ts(v):
        v = float(v)
        return v / 1000.0 if v > 1e11 else v

    def _walk(self, obj, code, depth):
        if depth > 3 or obj is None:
            return
        if hasattr(obj, "model_dump"):
            try:
                obj = obj.model_dump()
            except Exception:
                return
        if isinstance(obj, dict):
            c = self._code_in(obj.values()) or code
            ts, price = None, None
            floats = []
            for k, v in obj.items():
                if self._num(v):
                    kl = str(k).lower()
                    if kl in ("timestamp", "time", "ts", "t", "time_ms", "date") or float(v) >= 1e9:
                        ts = ts or self._ts(v)
                    else:
                        floats.append((kl, float(v)))
            for pref in ("close", "value", "price", "close_value", "closevalue", "last", "rate", "quote"):
                for k, v in floats:
                    if k == pref:
                        price = v
                        break
                if price is not None:
                    break
            if price is None and floats:
                price = floats[-1][1]
            if c and price is not None:
                self.add(c, price, ts)
            for v in obj.values():
                if isinstance(v, (list, tuple, dict)) or hasattr(v, "model_dump"):
                    self._walk(v, c, depth + 1)
            return
        if isinstance(obj, (list, tuple)):
            if obj and all((self._num(x) or isinstance(getattr(x, "value", x), str)) for x in obj):
                # une ligne du type ["EURUSD_otc", 1700000000.5, 1.0853] ou [ts, prix, ...]
                c = self._code_in(obj) or code
                nums = [float(x) for x in obj if self._num(x)]
                ts, price = None, None
                for i, v in enumerate(nums):
                    if v >= 1e9 and ts is None:
                        ts = self._ts(v)
                        rest = [x for x in nums[i + 1:] if 0 < x < 1e9]
                        price = rest[0] if rest else None
                        break
                if ts is None:
                    cand = [x for x in nums if 0 < x < 1e9]
                    price = cand[-1] if cand else None
                if c and price is not None:
                    self.add(c, price, ts)
                return
            for it in obj:
                self._walk(it, code, depth + 1)

    # ---------- analyse multi-durées ----------
    def last_price(self, sdk_code):
        dq = self.ticks.get(_norm(sdk_code))
        return dq[-1][1] if dq else None

    def price_at(self, sdk_code, t, tolerance=8.0):
        """Prix connu le plus proche de l'instant t (None si aucun tick à moins de `tolerance` secondes)."""
        dq = self.ticks.get(_norm(sdk_code))
        if not dq:
            return None
        best = min(dq, key=lambda x: abs(x[0] - t))
        return best[1] if abs(best[0] - t) <= tolerance else None

    def analyze(self, sdk_code) -> dict:
        key = _norm(sdk_code)
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < 2.0:
            return hit[1]
        res = self._analyze(key, now)
        self._cache[key] = (now, res)
        return res

    def _analyze(self, key, now) -> dict:
        dq = self.ticks.get(key)
        if not dq or now - dq[-1][0] > STALE_AFTER:
            return {"d": "none", "s": 0, "n": 0}
        scores, rsis = {}, {}
        for name, period, count in TFS:
            closes = self._closes(dq, period, count, now)
            if len(closes) >= MIN_CANDLES:
                scores[name], rsis[name] = self._tf_score(closes)
        n_mid = len(self._closes(dq, 10, 40, now))
        if len(scores) < 2:
            return {"d": "wait", "s": 0, "n": n_mid}
        wsum = sum(TF_WEIGHT[t] for t in scores)
        agg = sum(TF_WEIGHT[t] * sc for t, sc in scores.items()) / wsum
        tfdir = {t: ("up" if sc >= self.TF_DIR else "down" if sc <= -self.TF_DIR else "flat") for t, sc in scores.items()}
        ups = sum(1 for d in tfdir.values() if d == "up")
        downs = sum(1 for d in tfdir.values() if d == "down")
        direction = "flat"
        if agg >= self.AGG_MIN and ups >= 2 and downs == 0:
            direction = "up"
        elif agg <= -self.AGG_MIN and downs >= 2 and ups == 0:
            direction = "down"
        rsi = rsis.get("m", next(iter(rsis.values())))
        ext = (direction == "up" and max(rsis.values()) >= self.RSI_EXT) or (direction == "down" and min(rsis.values()) <= 100 - self.RSI_EXT)
        score = 0 if direction == "flat" else int(round(100 * min(1.0, abs(agg))))
        return {"d": direction, "s": score, "n": n_mid, "rsi": int(round(rsi)), "ext": bool(ext),
                "tf": [tfdir.get(t[0], "-") for t in TFS]}

    @staticmethod
    def _closes(dq, period, count, now):
        horizon = period * count
        buckets = {}
        for ts, p in dq:
            if now - ts <= horizon:
                buckets[int(ts // period)] = p
        return [buckets[k] for k in sorted(buckets)]

    @staticmethod
    def _ema_series(x, span):
        k = 2.0 / (span + 1)
        e = x[0]
        out = [e]
        for p in x[1:]:
            e = p * k + e * (1 - k)
            out.append(e)
        return out

    @staticmethod
    def _rsi(x, n=14):
        d = [b - a for a, b in zip(x, x[1:])][-n:]
        gain = sum(v for v in d if v > 0)
        loss = -sum(v for v in d if v < 0)
        if gain + loss == 0:
            return 50.0
        return 100.0 * gain / (gain + loss)

    def _tf_score(self, closes):
        diffs = [b - a for a, b in zip(closes, closes[1:])]
        mean = sum(diffs) / len(diffs)
        sigma = math.sqrt(sum((v - mean) ** 2 for v in diffs) / max(len(diffs) - 1, 1)) or 1e-12
        e_fast = self._ema_series(closes, 5)
        e_slow = self._ema_series(closes, 13)
        gap = e_fast[-1] - e_slow[-1]
        v_ema = 1 if gap > self.GAP_K * sigma else -1 if gap < -self.GAP_K * sigma else 0
        z = (closes[-1] - closes[0]) / (sigma * math.sqrt(len(diffs)))
        v_slope = 1 if z >= self.Z_VOTE else -1 if z <= -self.Z_VOTE else 0
        m1, m2 = self._ema_series(closes, 6), self._ema_series(closes, 13)
        macd = [a - b for a, b in zip(m1, m2)]
        # ligne MACD (et non l'histogramme) : un trend régulier garde une ligne MACD nette, alors que l'histogramme tombe à zéro
        v_macd = 1 if macd[-1] > self.MACD_K * sigma else -1 if macd[-1] < -self.MACD_K * sigma else 0
        rsi = self._rsi(closes)
        v_rsi = 1 if rsi >= 55 else -1 if rsi <= 45 else 0
        off = closes[-1] - e_slow[-1]
        v_pos = 1 if off > self.POS_K * sigma else -1 if off < -self.POS_K * sigma else 0
        return (v_ema + v_slope + v_macd + v_rsi + v_pos) / 5.0, rsi


class SignalEngine:
    """Repère les paires dont la tendance est nette ET confirmée sur plusieurs durées, de façon stable."""
    MIN_SCORE = 95           # score minimal pour une ALERTE d'entrée (la liste des paires, elle, montre dès 70)
    CONFIRM_STEPS = 8        # 8 contrôles de suite (~40 s) avant de signaler
    COOLDOWN = 600           # une même paire n'est re-signalée qu'après 10 minutes
    GLOBAL_GAP = 120         # au plus une alerte toutes les 2 minutes, la plus nette d'abord

    def __init__(self, tracker):
        self.t = tracker
        self.streak = {}     # code -> (sens, nombre de contrôles de suite)
        self.cool = {}       # (code, sens) -> instant du dernier signal envoyé
        self.live = {}       # code -> signal en cours
        self.last_emit = 0.0

    def step(self, pairs):
        """pairs : liste de (code sdk, nom). Retourne les NOUVEAUX signaux à annoncer."""
        now = time.time()
        fresh = []
        for sdk, name in pairs:
            r = self.t.analyze(sdk)
            ok = r["d"] in ("up", "down") and r["s"] >= self.MIN_SCORE
            prev = self.streak.get(sdk, (None, 0))
            if ok:
                cnt = prev[1] + 1 if prev[0] == r["d"] else 1
                self.streak[sdk] = (r["d"], cnt)
            else:
                self.streak[sdk] = (None, 0)
                self.live.pop(sdk, None)
                continue
            if cnt >= self.CONFIRM_STEPS:
                sig = {"sdk": sdk, "name": name, "d": r["d"], "s": r["s"], "tf": r.get("tf"), "ext": bool(r.get("ext")), "since": self.live.get(sdk, {}).get("since", now)}
                if self.live.get(sdk, {}).get("d") != r["d"]:
                    sig["since"] = now
                self.live[sdk] = sig
                if now - self.cool.get((sdk, r["d"]), 0) > self.COOLDOWN:
                    fresh.append(dict(sig, price=self.t.last_price(sdk), ts=now))
        if fresh and now - self.last_emit >= self.GLOBAL_GAP:
            best = max(fresh, key=lambda x: x["s"])
            self.cool[(best["sdk"], best["d"])] = now
            self.last_emit = now
            return [best]
        return []

    def live_list(self, limit=5):
        return sorted(self.live.values(), key=lambda x: -x["s"])[:limit]


TRACKER = TrendTracker()

SIGNALS = SignalEngine(TRACKER)
