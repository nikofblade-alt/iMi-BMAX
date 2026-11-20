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

    def feed(self, items):
        """Reçoit la liste d'éléments de l'évènement update_close_value (format inconnu : lecture tolérante)."""
        for it in (items if isinstance(items, (list, tuple)) else [items]):
            try:
                d = it.model_dump() if hasattr(it, "model_dump") else (dict(it) if isinstance(it, dict) else vars(it))
            except Exception:
                continue
            if self.samples_logged < 3:
                self.samples_logged += 1
                logger.info("TENDANCE exemple de données du SDK : %s", str(d)[:300])
            code, price = None, None
            floats = []
            for k, v in d.items():
                if code is None and isinstance(v, (str,)) or (code is None and hasattr(v, "value") and isinstance(getattr(v, "value", None), str)):
                    if _norm(v) in self.codes:
                        code = v
                        continue
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)) and 0 < float(v) < 1e9:
                    floats.append((k, float(v)))
            for pref in ("close", "value", "price", "close_value", "closevalue", "last"):
                for k, v in floats:
                    if k.lower() == pref:
                        price = v
                        break
                if price is not None:
                    break
            if price is None and floats:
                price = floats[-1][1]
            if code is not None and price is not None:
                self.add(code, price)

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
