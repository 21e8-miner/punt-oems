#!/usr/bin/env python3
"""Local Solana punt OEMS: live scanner, execution planner, paper/live child-order runner."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import random
import statistics
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

UA = "OpenAI File Downloader, XaiImageApiFetch/1.0"
RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JUP_V2 = "https://api.jup.ag/swap/v2"
JUP_LITE = "https://lite-api.jup.ag/swap/v1/quote"
DEX = "https://api.dexscreener.com/token-pairs/v1/solana"
GECKO = "https://api.geckoterminal.com/api/v2/networks/solana/tokens"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
HOST = "127.0.0.1"
PORT = 8770
BASE = Path(__file__).resolve().parent
JOURNAL = BASE / "oems_journal.jsonl"
STATE_PATH = BASE / "oems_state.json"

DEFAULT_INSTRUMENTS = {
    "ZCAT": {
        "name": "ZCAT",
        "mint": "HcRLc9VDgjLeK154xDawfb1dmVJ98DoSqcwTHGqiDeJR",
        "rewardMint": "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS",
        "expectedTransferFeeBps": 300,
        "positionTokens": 225600.0,
    },
    "BTCAT": {
        "name": "Buy The Cat (BTC)",
        "mint": "E4Ap4icMLwKot8rkkTbq5JkS5kZxt5XCE3yfxbzYBjHx",
        "rewardMint": "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
        "expectedTransferFeeBps": None,
        "positionTokens": 0.0,
    },
}

LOCK = threading.RLock()
CACHE: dict[str, tuple[float, Any]] = {}
MARKET: dict[str, dict[str, Any]] = {}
PLANS: dict[str, dict[str, Any]] = {}
FILLS: deque[dict[str, Any]] = deque(maxlen=500)
RUNNER: threading.Thread | None = None
STOP = threading.Event()
CONFIG: dict[str, Any] = {}


def ts() -> float:
    return time.time()


def iso(t: float | None = None) -> str:
    return datetime.fromtimestamp(t or ts(), timezone.utc).isoformat()


def num(v: Any, default: float | None = None) -> float | None:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def http_json(url: str, *, method: str = "GET", body: Any = None, headers: dict[str, str] | None = None, timeout: int = 10) -> Any:
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def cached(key: str, ttl: float, fn) -> Any:
    now = ts()
    with LOCK:
        old = CACHE.get(key)
    if old and now - old[0] < ttl:
        return old[1]
    try:
        value = fn()
        with LOCK:
            CACHE[key] = (now, value)
        return value
    except Exception:
        if old:
            return old[1]
        raise


def rpc(method: str, params: list[Any]) -> Any:
    out = http_json(RPC_URL, method="POST", body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if out.get("error"):
        raise RuntimeError(str(out["error"]))
    return out.get("result")


def contract_state(mint: str) -> dict[str, Any]:
    acc = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    epoch = rpc("getEpochInfo", [{"commitment": "confirmed"}])
    if not acc or not acc.get("value"):
        raise RuntimeError("mint not found")
    value = acc["value"]
    parsed = ((value.get("data") or {}).get("parsed") or {})
    info = parsed.get("info") or {}
    exts = info.get("extensions") or parsed.get("extensions") or []
    tf = next((e.get("state") or e for e in exts if str(e.get("extension", "")).lower() == "transferfeeconfig"), None)
    decimals = int(info.get("decimals", 0) or 0)
    supply_raw = num(info.get("supply"))
    supply = supply_raw / 10**decimals if supply_raw is not None else None
    out: dict[str, Any] = {
        "programOwner": value.get("owner"),
        "isToken2022": value.get("owner") == TOKEN_2022,
        "decimals": decimals,
        "supply": supply,
        "transferFee": None,
    }
    if tf:
        current_epoch = int((epoch or {}).get("epoch", 0))
        older = tf.get("olderTransferFee") or {}
        newer = tf.get("newerTransferFee") or {}
        active = newer if current_epoch >= int(num(newer.get("epoch"), 0) or 0) else older
        bps = int(num(active.get("transferFeeBasisPoints"), 0) or 0)
        max_raw = num(active.get("maximumFee"))
        max_tokens = max_raw / 10**decimals if max_raw is not None else None
        state = {
            "config": tf.get("transferFeeConfigAuthority"),
            "withdraw": tf.get("withdrawWithheldAuthority"),
            "older": older,
            "newer": newer,
        }
        out["transferFee"] = {
            "bps": bps,
            "maximumFeeRaw": active.get("maximumFee"),
            "maximumFeeTokens": max_tokens,
            "configAuthority": tf.get("transferFeeConfigAuthority"),
            "withdrawAuthority": tf.get("withdrawWithheldAuthority"),
            "stateHash": hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }
    return out


def dex_pair(mint: str) -> dict[str, Any]:
    rows = http_json(f"{DEX}/{mint}") or []
    sol = [r for r in rows if r.get("chainId") == "solana"]
    if not sol:
        raise RuntimeError("no DexScreener pools")
    p = max(sol, key=lambda r: num((r.get("liquidity") or {}).get("usd"), 0) or 0)
    b = p.get("baseToken") or {}
    q = p.get("quoteToken") or {}
    token_is_base = b.get("address") == mint
    px = num(p.get("priceUsd")) if token_is_base else None
    if not token_is_base and q.get("address") == mint:
        base_usd = num(p.get("priceUsd"))
        native = num(p.get("priceNative"))
        px = base_usd / native if base_usd is not None and native not in (None, 0) else None
    v = p.get("volume") or {}
    return {
        "pair": p.get("pairAddress"),
        "dex": p.get("dexId"),
        "symbol": b.get("symbol") if token_is_base else q.get("symbol"),
        "price": px,
        "marketCap": num(p.get("marketCap")) or num(p.get("fdv")),
        "liquidity": num((p.get("liquidity") or {}).get("usd")),
        "volume5m": num(v.get("m5")),
        "volume1h": num(v.get("h1")),
        "volume24h": num(v.get("h24")),
        "change1h": num((p.get("priceChange") or {}).get("h1")),
        "url": p.get("url"),
    }


def gecko_pair(mint: str) -> dict[str, Any]:
    obj = http_json(f"{GECKO}/{mint}/pools?page=1")
    rows = obj.get("data") or []
    if not rows:
        raise RuntimeError("no GeckoTerminal pools")
    p = max(rows, key=lambda r: num((r.get("attributes") or {}).get("reserve_in_usd"), 0) or 0)
    a = p.get("attributes") or {}
    base_id = ((((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or "").split("_", 1)[-1]
    quote_id = ((((p.get("relationships") or {}).get("quote_token") or {}).get("data") or {}).get("id") or "").split("_", 1)[-1]
    px = num(a.get("base_token_price_usd")) if base_id == mint else num(a.get("quote_token_price_usd")) if quote_id == mint else None
    vol = a.get("volume_usd") or {}
    return {
        "pair": (p.get("id") or "").split("_", 1)[-1],
        "price": px,
        "marketCap": num(a.get("market_cap_usd")) or num(a.get("fdv_usd")),
        "liquidity": num(a.get("reserve_in_usd")),
        "volume5m": num(vol.get("m5")),
        "volume1h": num(vol.get("h1")),
        "volume24h": num(vol.get("h24")),
    }


def jup_quote(input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int = 50) -> dict[str, Any]:
    q = urllib.parse.urlencode({
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": str(slippage_bps),
        "instructionVersion": "V2",
    })
    out = http_json(f"{JUP_LITE}?{q}")
    if out.get("error"):
        raise RuntimeError(str(out.get("error")))
    route = []
    for row in out.get("routePlan") or []:
        label = ((row.get("swapInfo") or {}).get("label"))
        if label and label not in route:
            route.append(label)
    return {
        "inAmount": int(out.get("inAmount") or amount_raw),
        "outAmount": int(out.get("outAmount") or 0),
        "minOut": int(out.get("otherAmountThreshold") or 0),
        "priceImpactPct": (num(out.get("priceImpactPct"), 0) or 0) * 100.0,
        "route": " → ".join(route),
        "contextSlot": out.get("contextSlot"),
    }


def refresh_market(symbol: str) -> dict[str, Any]:
    inst = CONFIG["instruments"][symbol]
    mint = inst["mint"]
    d = cached(f"dex:{mint}", 12, lambda: dex_pair(mint))
    g = cached(f"gecko:{mint}", 25, lambda: gecko_pair(mint))
    c = cached(f"contract:{mint}", 120, lambda: contract_state(mint))
    dp = d.get("price")
    gp = g.get("price")
    divergence = abs(dp / gp - 1) * 100 if dp and gp else None
    mc = d.get("marketCap") or (dp * c.get("supply") if dp and c.get("supply") else None)
    liq = d.get("liquidity")
    v1 = d.get("volume1h")
    v5 = d.get("volume5m")
    v24 = d.get("volume24h")
    row = {
        "symbol": symbol,
        "name": inst["name"],
        "mint": mint,
        "rewardMint": inst.get("rewardMint"),
        "price": dp,
        "marketCap": mc,
        "liquidity": liq,
        "volume5m": v5,
        "volume1h": v1,
        "volume24h": v24,
        "turnoverMc": v24 / mc if v24 and mc else None,
        "liquidityMc": liq / mc if liq and mc else None,
        "geckoPrice": gp,
        "sourceDivergencePct": divergence,
        "contract": c,
        "dex": d,
        "gecko": g,
        "updated": ts(),
    }
    with LOCK:
        MARKET[symbol] = row
    return row


def refresh_all() -> None:
    while not STOP.is_set():
        for s in list(CONFIG["instruments"]):
            try:
                refresh_market(s)
            except Exception as e:
                with LOCK:
                    old = MARKET.get(s, {"symbol": s})
                    old["error"] = str(e)
                    old["updated"] = ts()
                    MARKET[s] = old
        STOP.wait(12)


def decimals(mint: str) -> int:
    if mint == USDC:
        return 6
    for row in MARKET.values():
        if row.get("mint") == mint:
            return int((row.get("contract") or {}).get("decimals", 0))
    return int(contract_state(mint)["decimals"])


def quote_usd(symbol: str, side: str, usd: float) -> dict[str, Any]:
    m = MARKET.get(symbol) or refresh_market(symbol)
    inst = CONFIG["instruments"][symbol]
    token_dec = int((m.get("contract") or {}).get("decimals", 0))
    price = m.get("price")
    if not price or price <= 0:
        raise RuntimeError("no token price")
    if side == "BUY":
        raw = max(1, int(usd * 1_000_000))
        q = jup_quote(USDC, inst["mint"], raw)
        out_tokens = q["outAmount"] / 10**token_dec
        return {**q, "usd": usd, "tokens": out_tokens, "effectivePrice": usd / out_tokens if out_tokens else None}
    token_amount = usd / price
    raw = max(1, int(token_amount * 10**token_dec))
    q = jup_quote(inst["mint"], USDC, raw)
    out_usd = q["outAmount"] / 1_000_000
    return {**q, "usd": usd, "tokens": token_amount, "outUsd": out_usd, "effectivePrice": out_usd / token_amount if token_amount else None}


def depth_curve(symbol: str, side: str, points: list[float] | None = None) -> list[dict[str, Any]]:
    points = points or [250, 500, 1000, 2500, 5000, 10000, 25000, 50000]
    out = []
    for usd in points:
        try:
            q = cached(f"depth:{symbol}:{side}:{usd}", 15, lambda s=symbol, d=side, u=usd: quote_usd(s, d, u))
            out.append({"usd": usd, "impactBps": q["priceImpactPct"] * 100, "effectivePrice": q.get("effectivePrice"), "route": q.get("route")})
        except Exception as e:
            out.append({"usd": usd, "error": str(e)})
    return out


def max_slice_from_curve(curve: list[dict[str, Any]], max_impact_bps: float) -> float:
    good = [r["usd"] for r in curve if not r.get("error") and num(r.get("impactBps"), 1e9) <= max_impact_bps]
    return max(good) if good else 0.0


def build_plan(req: dict[str, Any]) -> dict[str, Any]:
    symbol = req.get("symbol") or "ZCAT"
    side = str(req.get("side") or "BUY").upper()
    if symbol not in CONFIG["instruments"] or side not in {"BUY", "SELL"}:
        raise ValueError("invalid instrument or side")
    total = float(req.get("notionalUsd") or 0)
    if total <= 0:
        raise ValueError("notionalUsd must be positive")
    mode = str(req.get("mode") or "ADAPTIVE").upper()
    max_impact = float(req.get("maxImpactBps") or 50)
    max_participation = float(req.get("maxParticipationPct") or 2.0)
    min_slice = float(req.get("minSliceUsd") or 250)
    max_slice = float(req.get("maxSliceUsd") or 5000)
    duration = int(req.get("durationMinutes") or 30)
    m = MARKET.get(symbol) or refresh_market(symbol)
    curve = depth_curve(symbol, side)
    impact_cap = max_slice_from_curve(curve, max_impact)
    volume5m = m.get("volume5m") or 0
    participation_cap = volume5m * max_participation / 100 if volume5m > 0 else max_slice
    if mode == "IMMEDIATE":
        slice_usd = total
    else:
        candidates = [max_slice]
        if impact_cap > 0:
            candidates.append(impact_cap)
        if participation_cap > 0:
            candidates.append(participation_cap)
        slice_usd = max(min_slice, min(candidates))
        slice_usd = min(slice_usd, total)
    n_slices = max(1, math.ceil(total / slice_usd))
    if mode == "IMMEDIATE":
        interval = 0
    else:
        interval = max(15, int(duration * 60 / max(1, n_slices - 1))) if n_slices > 1 else 0
    pid = f"{int(ts())}-{random.randrange(1000, 9999)}"
    plan = {
        "id": pid,
        "created": ts(),
        "symbol": symbol,
        "side": side,
        "mode": mode,
        "status": "PLANNED",
        "notionalUsd": total,
        "remainingUsd": total,
        "filledUsd": 0.0,
        "sliceUsd": slice_usd,
        "slices": n_slices,
        "filledSlices": 0,
        "intervalSec": interval,
        "maxImpactBps": max_impact,
        "maxParticipationPct": max_participation,
        "minSliceUsd": min_slice,
        "maxSliceUsd": max_slice,
        "durationMinutes": duration,
        "arrivalPrice": m.get("price"),
        "maxAdverseMovePct": float(req.get("maxAdverseMovePct") or 2.5),
        "maxSourceDivergencePct": float(req.get("maxSourceDivergencePct") or 5.0),
        "maxQuoteDeteriorationBps": float(req.get("maxQuoteDeteriorationBps") or 75.0),
        "minLiquidityUsd": float(req.get("minLiquidityUsd") or 250000),
        "curve": curve,
        "lastAction": None,
        "nextAction": None,
        "error": None,
    }
    with LOCK:
        PLANS[pid] = plan
    journal("PLAN", plan)
    save_state()
    return plan


def guard(plan: dict[str, Any]) -> tuple[bool, str]:
    m = MARKET.get(plan["symbol"]) or refresh_market(plan["symbol"])
    inst = CONFIG["instruments"][plan["symbol"]]
    if (m.get("liquidity") or 0) < plan["minLiquidityUsd"]:
        return False, "liquidity below floor"
    div = m.get("sourceDivergencePct")
    if div is not None and div > plan["maxSourceDivergencePct"]:
        return False, f"source divergence {div:.2f}%"
    px = m.get("price")
    arrival = plan.get("arrivalPrice")
    if px and arrival:
        move = (px / arrival - 1) * 100
        adverse = move if plan["side"] == "BUY" else -move
        if adverse > plan["maxAdverseMovePct"]:
            return False, f"adverse move {adverse:.2f}% from arrival"
    expected = inst.get("expectedTransferFeeBps")
    actual = (((m.get("contract") or {}).get("transferFee") or {}).get("bps"))
    if expected is not None and actual is not None and int(actual) != int(expected):
        return False, f"transfer fee changed: expected {expected}, saw {actual} bps"
    return True, "ok"


def child_notional(plan: dict[str, Any]) -> float:
    remaining = float(plan["remainingUsd"])
    target = min(float(plan["sliceUsd"]), remaining)
    m = MARKET.get(plan["symbol"]) or {}
    v5 = m.get("volume5m") or 0
    if v5 > 0:
        target = min(target, v5 * plan["maxParticipationPct"] / 100)
    return max(0.0, target)


def fit_child(plan: dict[str, Any], usd: float) -> tuple[float, dict[str, Any]]:
    trial = usd
    while trial >= plan["minSliceUsd"] * 0.99:
        q = quote_usd(plan["symbol"], plan["side"], trial)
        if q["priceImpactPct"] * 100 <= plan["maxImpactBps"]:
            return trial, q
        trial /= 2
    raise RuntimeError("no child size satisfies impact guard")


def keypair_pubkey() -> str | None:
    if not CONFIG.get("live") or not CONFIG.get("keypairPath"):
        return None
    kp = load_keypair()
    return str(kp.pubkey())


def load_keypair():
    try:
        from solders.keypair import Keypair
    except ImportError as e:
        raise RuntimeError("live mode requires: python3 -m pip install solders") from e
    raw = json.loads(Path(CONFIG["keypairPath"]).expanduser().read_text())
    if not isinstance(raw, list) or len(raw) != 64:
        raise RuntimeError("keypair must be a Solana CLI 64-byte JSON array")
    return Keypair.from_bytes(bytes(raw))


def sign_transaction(tx_b64: str) -> str:
    try:
        from solders.message import to_bytes_versioned
        from solders.transaction import VersionedTransaction
    except ImportError as e:
        raise RuntimeError("live mode requires: python3 -m pip install solders") from e
    kp = load_keypair()
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    keys = list(tx.message.account_keys)
    try:
        idx = keys.index(kp.pubkey())
    except ValueError as e:
        raise RuntimeError("configured keypair is not a signer in Jupiter transaction") from e
    sigs = list(tx.signatures)
    sigs[idx] = kp.sign_message(to_bytes_versioned(tx.message))
    signed = VersionedTransaction.populate(tx.message, sigs)
    return base64.b64encode(bytes(signed)).decode()


def jupiter_live_order(symbol: str, side: str, usd: float, preview: dict[str, Any], max_deterioration_bps: float) -> dict[str, Any]:
    api_key = CONFIG.get("jupiterApiKey")
    if not api_key:
        raise RuntimeError("JUPITER_API_KEY is required for live mode")
    taker = keypair_pubkey()
    m = MARKET.get(symbol) or refresh_market(symbol)
    inst = CONFIG["instruments"][symbol]
    token_dec = int((m.get("contract") or {}).get("decimals", 0))
    if side == "BUY":
        input_mint, output_mint = USDC, inst["mint"]
        amount = max(1, int(usd * 1_000_000))
    else:
        price = m.get("price")
        if not price:
            raise RuntimeError("no token price")
        input_mint, output_mint = inst["mint"], USDC
        amount = max(1, int((usd / price) * 10**token_dec))
    params = urllib.parse.urlencode({"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount), "taker": taker})
    order = http_json(f"{JUP_V2}/order?{params}", headers={"x-api-key": api_key})
    if not order.get("transaction") or not order.get("requestId"):
        raise RuntimeError(f"Jupiter order failed: {order}")
    live_out = num(order.get("outAmount"))
    preview_out = num(preview.get("outAmount"))
    if live_out is not None and preview_out not in (None, 0):
        deterioration_bps = max(0.0, (1.0 - live_out / preview_out) * 10000.0)
        if deterioration_bps > max_deterioration_bps:
            raise RuntimeError(f"quote deteriorated {deterioration_bps:.1f} bps before signing")
    signed = sign_transaction(order["transaction"])
    result = http_json(
        f"{JUP_V2}/execute",
        method="POST",
        headers={"x-api-key": api_key},
        body={"signedTransaction": signed, "requestId": order["requestId"]},
        timeout=25,
    )
    if result.get("status") != "Success":
        raise RuntimeError(f"Jupiter execute failed: {result}")
    return {"order": order, "result": result}


def execute_child(plan: dict[str, Any]) -> dict[str, Any]:
    ok, why = guard(plan)
    if not ok:
        raise RuntimeError(why)
    usd = child_notional(plan)
    if usd <= 0:
        raise RuntimeError("nothing remaining")
    fitted_usd, q = fit_child(plan, usd)
    if CONFIG.get("live"):
        live = jupiter_live_order(plan["symbol"], plan["side"], fitted_usd, q, plan["maxQuoteDeteriorationBps"])
        result = live["result"]
        actual_in = num(result.get("inputAmountResult"))
        actual_out = num(result.get("outputAmountResult"))
        fill = {
            "time": ts(),
            "planId": plan["id"],
            "symbol": plan["symbol"],
            "side": plan["side"],
            "usd": fitted_usd,
            "paper": False,
            "signature": result.get("signature"),
            "inputRaw": actual_in,
            "outputRaw": actual_out,
            "impactBps": q["priceImpactPct"] * 100,
            "route": q.get("route"),
        }
    else:
        fill = {
            "time": ts(),
            "planId": plan["id"],
            "symbol": plan["symbol"],
            "side": plan["side"],
            "usd": fitted_usd,
            "paper": True,
            "signature": None,
            "impactBps": q["priceImpactPct"] * 100,
            "route": q.get("route"),
            "effectivePrice": q.get("effectivePrice"),
        }
    with LOCK:
        FILLS.appendleft(fill)
        plan["filledUsd"] += fitted_usd
        plan["remainingUsd"] = max(0.0, plan["notionalUsd"] - plan["filledUsd"])
        plan["filledSlices"] += 1
        plan["lastAction"] = ts()
        plan["error"] = None
        if plan["remainingUsd"] <= max(1.0, plan["minSliceUsd"] * 0.05):
            plan["status"] = "FILLED"
        else:
            jitter = random.uniform(0.82, 1.18)
            plan["nextAction"] = ts() + plan["intervalSec"] * jitter
    journal("FILL", fill)
    save_state()
    return fill


def runner_loop() -> None:
    while not STOP.is_set():
        with LOCK:
            work = [p for p in PLANS.values() if p.get("status") == "WORKING" and (p.get("nextAction") or 0) <= ts()]
        for plan in work:
            try:
                execute_child(plan)
            except Exception as e:
                with LOCK:
                    plan["status"] = "PAUSED"
                    plan["error"] = str(e)
                    plan["lastAction"] = ts()
                journal("PAUSE", {"planId": plan["id"], "error": str(e)})
                save_state()
        STOP.wait(1)


def start_plan(pid: str, live_confirm: str | None = None) -> dict[str, Any]:
    with LOCK:
        plan = PLANS[pid]
        if CONFIG.get("live") and live_confirm != "LIVE":
            raise ValueError("type LIVE to arm live execution")
        if CONFIG.get("live") and plan["notionalUsd"] > CONFIG["maxLivePlanUsd"]:
            raise ValueError(f"plan exceeds live notional cap ${CONFIG['maxLivePlanUsd']:,.0f}")
        if plan["status"] in {"FILLED", "CANCELED"}:
            raise ValueError("plan is closed")
        plan["status"] = "WORKING"
        plan["nextAction"] = ts()
        plan["error"] = None
    journal("START", {"planId": pid, "live": bool(CONFIG.get("live"))})
    save_state()
    return plan


def set_plan_status(pid: str, status: str) -> dict[str, Any]:
    if status not in {"PAUSED", "CANCELED", "WORKING"}:
        raise ValueError("bad status")
    with LOCK:
        p = PLANS[pid]
        p["status"] = status
        if status == "WORKING":
            p["nextAction"] = ts()
    journal(status, {"planId": pid})
    save_state()
    return p


def journal(kind: str, payload: dict[str, Any]) -> None:
    row = {"time": iso(), "kind": kind, **payload}
    with JOURNAL.open("a") as f:
        f.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")


def save_state() -> None:
    with LOCK:
        data = {"plans": PLANS, "fills": list(FILLS), "saved": ts()}
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(STATE_PATH)


def load_state() -> None:
    if not STATE_PATH.exists():
        return
    try:
        data = json.loads(STATE_PATH.read_text())
        with LOCK:
            PLANS.update(data.get("plans") or {})
            FILLS.extend(data.get("fills") or [])
            for p in PLANS.values():
                if p.get("status") == "WORKING":
                    p["status"] = "PAUSED"
                    p["error"] = "paused after restart"
    except Exception:
        return


INDEX_HTML_FILE = BASE / "index.html"
HTML = INDEX_HTML_FILE.read_text(encoding="utf-8") if INDEX_HTML_FILE.exists() else r'''<!doctype html><html><head><meta charset="utf-8"><title>Punt OEMS</title></head><body><h1>Punt OEMS</h1></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        return

    def send_json(self, obj: Any, status: int = 200) -> None:
        raw = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-api-key")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-api-key")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode() or "{}")

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/":
                index_file = BASE / "index.html"
                raw = index_file.read_bytes() if index_file.exists() else HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if u.path == "/api/state":
                with LOCK:
                    self.send_json({
                        "live": bool(CONFIG.get("live")),
                        "pubkey": keypair_pubkey() if CONFIG.get("live") else None,
                        "instruments": CONFIG["instruments"],
                        "market": MARKET,
                        "plans": PLANS,
                        "fills": list(FILLS),
                    })
                return
            if u.path == "/api/market":
                self.send_json(refresh_market((q.get("symbol") or ["ZCAT"])[0]))
                return
            if u.path == "/api/depth":
                self.send_json(depth_curve((q.get("symbol") or ["ZCAT"])[0], (q.get("side") or ["BUY"])[0]))
                return
            self.send_json({"error": "not found"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            if self.path == "/api/plan":
                self.send_json(build_plan(self.body()))
                return
            if self.path == "/api/plan/start":
                b = self.body()
                self.send_json(start_plan(b["id"], b.get("confirm")))
                return
            if self.path == "/api/plan/status":
                b = self.body()
                self.send_json(set_plan_status(b["id"], b["status"]))
                return
            if self.path == "/api/kill":
                with LOCK:
                    for p in PLANS.values():
                        if p.get("status") in {"WORKING", "PAUSED", "PLANNED"}:
                            p["status"] = "CANCELED"
                journal("KILL", {})
                save_state()
                self.send_json({"ok": True})
                return
            self.send_json({"error": "not found"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 400)


def main() -> None:
    global CONFIG, RUNNER
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--live", action="store_true", help="enable live Jupiter execution")
    ap.add_argument("--keypair", help="Solana CLI keypair JSON path; required with --live")
    ap.add_argument("--max-live-plan-usd", type=float, default=50000)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    if args.live and not args.keypair:
        ap.error("--live requires --keypair")
    CONFIG = {
        "live": args.live,
        "keypairPath": args.keypair,
        "jupiterApiKey": os.getenv("JUPITER_API_KEY"),
        "maxLivePlanUsd": args.max_live_plan_usd,
        "instruments": DEFAULT_INSTRUMENTS,
    }
    if args.live:
        load_keypair()
        if not CONFIG["jupiterApiKey"]:
            ap.error("--live requires JUPITER_API_KEY in the environment")
    load_state()
    threading.Thread(target=refresh_all, daemon=True).start()
    RUNNER = threading.Thread(target=runner_loop, daemon=True)
    RUNNER.start()
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    url = f"http://{HOST}:{args.port}"
    print(f"Punt OEMS {'LIVE' if args.live else 'PAPER'}: {url}")
    if not args.no_open:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        save_state()
        server.server_close()


if __name__ == "__main__":
    main()
