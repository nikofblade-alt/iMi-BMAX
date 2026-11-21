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

BUCKET = 5          # secondes : on garde un prix toutes les 5 s pour lisser le bruit
WINDOW = 240        # on regarde les 4 dernières minutes
MIN_BUCKETS = 12    # au moins 1 minute de données avant d'afficher une tendance
STALE_AFTER = 45    # sans prix depuis 45 s : tendance indisponible


def _norm(code) -> str:
    c = str(getattr(code, "value", code)).lower().lstrip("#")
    c = re.sub(r"_otc$", "", c)
    return re.sub(r"[^a-z0-9]", "", c)


class TrendTracker:
    def __init__(self):
        self.ticks = {}        # code normalisé -> deque[(ts, prix)]
        self.codes = {}        # code normalisé -> code SDK d'origine
        self.owner = None      # la connexion qui alimente les prix
        self.samples_logged = 0
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
        dq = self.ticks.setdefault(key, deque(maxlen=1500))
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

    # ---------- analyse ----------
    def analyze(self, sdk_code) -> dict:
        key = _norm(sdk_code)
        dq = self.ticks.get(key)
        if not dq:
            return {"d": "none", "s": 0, "n": 0}
        now = time.time()
        if now - dq[-1][0] > STALE_AFTER:
            return {"d": "none", "s": 0, "n": 0}
        buckets = {}
        for ts, p in dq:
            if now - ts <= WINDOW:
                buckets[int(ts // BUCKET)] = p
        series = [buckets[k] for k in sorted(buckets)]
        n = len(series)
        if n < MIN_BUCKETS:
            return {"d": "wait", "s": 0, "n": n}
        diffs = [b - a for a, b in zip(series, series[1:])]
        mean = sum(diffs) / len(diffs)
        var = sum((x - mean) ** 2 for x in diffs) / max(len(diffs) - 1, 1)
        sigma = math.sqrt(var) or 1e-12
        z = (series[-1] - series[0]) / (sigma * math.sqrt(len(diffs)))
        fast = self._ema(series, 3)
        slow = self._ema(series, 10)
        direction = "flat"
        if z >= 1.4 and fast > slow:
            direction = "up"
        elif z <= -1.4 and fast < slow:
            direction = "down"
        strength = min(100, int(round(abs(z) / 3.5 * 100))) if direction != "flat" else 0
        return {"d": direction, "s": strength, "n": n}

    @staticmethod
    def _ema(series, span):
        k = 2.0 / (span + 1)
        e = series[0]
        for p in series[1:]:
            e = p * k + e * (1 - k)
        return e


TRACKER = TrendTracker()
