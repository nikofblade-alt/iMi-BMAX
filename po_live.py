"""Pocket Option live access (unofficial SDK), used for the owner's account only."""
import asyncio
import json
import logging
import os

from pocket_option import PocketOptionClient
from pocket_option.constants import Regions
from pocket_option.contrib.default_init import default_init
from pocket_option.models import Asset, AuthorizationData, DealAction

logger = logging.getLogger(__name__)

try:
    from trend import TRACKER
except Exception:  # la tendance est un bonus : jamais bloquante
    TRACKER = None

REAL_REGION = os.getenv("PO_REGION", "EUROPA")


class POError(Exception):
    pass


class AssetError(POError):
    """La paire est inconnue du SDK ou refusée par Pocket Option (fermée, indisponible)."""


def parse_ssid(text: str) -> dict:
    """Accepts the browser message `42["auth",{...}]` or the bare JSON object."""
    t = (text or "").strip()
    try:
        if t.startswith("42"):
            data = json.loads(t[2:])[1]
        else:
            data = json.loads(t)
    except Exception:
        raise POError("SSID illisible : colle le message complet qui commence par 42[\"auth\",...")
    if not isinstance(data, dict) or not data.get("session") or not data.get("uid"):
        raise POError("SSID incomplet : il faut au moins session et uid.")
    return {
        "session": str(data["session"]),
        "isDemo": 1 if int(data.get("isDemo", 1)) else 0,
        "uid": int(data["uid"]),
        "platform": int(data.get("platform", 2)),
    }


class POLive:
    def __init__(self, auth: dict):
        self.auth = auth
        self.is_demo = auth["isDemo"]
        self.client = None
        self.balance = None

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_authorized

    async def connect(self, timeout: float = 15.0):
        await self.close()
        client = PocketOptionClient()
        authorization = AuthorizationData.model_validate(
            {**self.auth, "isFastHistory": True, "isOptimized": True}
        )
        default_init(client, authorization=authorization, sub_assets=[])

        @client.on.balance_success_update
        async def _on_balance(ev):
            if int(ev.is_demo) == self.is_demo:
                self.balance = float(ev.balance)

        self.client = client
        try:
            region = Regions.DEMO if self.is_demo else Regions[REAL_REGION]
            await client.connect(region)
            await client.wait_for_authorization(timeout)
        except Exception as exc:
            await self.close()
            raise POError("Connexion Pocket Option impossible ou SSID expiré : %s" % exc)
        try:
            await client.emit.update_balance()
            for _ in range(60):
                if self.balance is not None:
                    break
                await asyncio.sleep(0.1)
        except Exception:
            logger.exception("Lecture du solde impossible")
        try:
            await self._start_trend()
        except Exception:
            logger.exception("Tendance indisponible")

    async def _start_trend(self):
        """Écoute les prix pour la tendance. Totalement isolé du trading : toute erreur est ignorée."""
        if TRACKER is None or not TRACKER.codes or self.client is None:
            return
        client = self.client

        @client.on.update_close_value
        async def _on_close(items):
            try:
                TRACKER.feed(items)
            except Exception:
                pass

        owner = TRACKER.owner
        if owner is not None and owner is not self and owner.connected:
            return  # une autre connexion alimente déjà les prix
        TRACKER.owner = self
        done = 0
        for code in list(TRACKER.codes.values()):
            try:
                asset = Asset(code)
            except Exception:
                continue
            for args in ((asset,), (asset, 5)):
                try:
                    await client.emit.subscribe_to_asset(*args)
                    done += 1
                    break
                except TypeError:
                    continue
                except Exception as exc:
                    logger.info("TENDANCE abonnement refusé pour %s : %s", code, exc)
                    break
            await asyncio.sleep(0.05)
        logger.info("TENDANCE : %d paires suivies", done)

    async def close(self):
        c, self.client = self.client, None
        if TRACKER is not None and TRACKER.owner is self:
            TRACKER.owner = None
        if c is not None:
            try:
                await c.disconnect()
            except Exception:
                pass

    async def trade(self, sdk_asset: str, direction: str, stake: float, duration: int):
        """Opens one deal and waits for its close. Returns (status, pnl, balance)."""
        if not self.connected:
            raise POError("Non connecté à Pocket Option.")
        try:
            asset = Asset(sdk_asset)
        except ValueError:
            raise AssetError("Actif inconnu du SDK : %s" % sdk_asset)
        action = DealAction.CALL if direction == "call" else DealAction.PUT
        try:
            deal = await self.client.deals.open_deal(
                asset=asset, amount=stake, action=action, time=duration, is_demo=self.is_demo
            )
        except asyncio.TimeoutError:
            raise POError("Pocket Option n'a pas répondu à temps.")
        except Exception as exc:
            msg = str(exc) or type(exc).__name__
            if any(w in msg.lower() for w in ("balance", "solde", "insufficient", "connect", "auth", "timeout")):
                raise POError(msg)
            raise AssetError("Actif indisponible : %s" % msg)
        res = await self.client.deals.check_deal_result(wait_time=duration + 20, deal=deal)
        profit = float(res.profit)
        if profit > 0:
            status, pnl = "win", round(profit, 2)
        elif profit < 0:
            status, pnl = "loss", round(profit, 2)
        else:
            op, cl = float(res.open_price or 0), float(res.close_price or 0)
            if op and cl and op == cl:
                status, pnl = "draw", 0.0
            else:
                status, pnl = "loss", -round(stake, 2)
        try:
            await self.client.emit.update_balance()
            await asyncio.sleep(0.7)
        except Exception:
            pass
        return status, pnl, self.balance
