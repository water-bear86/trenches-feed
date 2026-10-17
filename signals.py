"""Deterministic market signals. A change in a provider value is not proof of its cause."""

import datetime as dt
import hashlib
import math
import re

UTC = dt.timezone.utc
ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}\Z")


def number(value, *, signed=False):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(value) or abs(value) > 1e20 or (not signed and value < 0):
        return None
    return value


def label(value, limit=80):
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).split())[:limit]


def normalize_dex(raw, source_url, fetched_at):
    if not isinstance(raw, dict) or raw.get("chainId") != "solana":
        return None
    base = raw.get("baseToken") or {}
    if not isinstance(base, dict) or any(not isinstance(raw.get(field) or {}, dict) for field in ("liquidity", "volume", "priceChange", "boosts")):
        return None
    token, pair = base.get("address", ""), raw.get("pairAddress", "")
    if not isinstance(token, str) or not isinstance(pair, str) or not ADDRESS.fullmatch(token) or not ADDRESS.fullmatch(pair):
        return None
    liquidity = number((raw.get("liquidity") or {}).get("usd"))
    m5 = number((raw.get("volume") or {}).get("m5"))
    if liquidity is None or m5 is None:
        return None
    created = number(raw.get("pairCreatedAt"))
    try:
        created_at = dt.datetime.fromtimestamp(created / 1000, UTC).isoformat() if created else None
    except (ValueError, OverflowError, OSError):
        created_at = None
    return {"key": f"solana:{pair}", "chain": "solana", "pair": pair, "token": token,
            "event_key": "market:solana:" + hashlib.sha256(pair.encode()).hexdigest()[:20],
            "symbol": label(base.get("symbol"), 24) or token[:8], "name": label(base.get("name")),
            "dex": label(raw.get("dexId"), 40), "liquidity_usd": liquidity,
            "volume_m5": m5, "volume_h1": number((raw.get("volume") or {}).get("h1")),
            "price_change_m5": number((raw.get("priceChange") or {}).get("m5"), signed=True),
            "price_usd": number(raw.get("priceUsd")), "created_at": created_at,
            "boosts": number((raw.get("boosts") or {}).get("active")) or 0,
            "source": "dexscreener", "source_url": source_url,
            "pair_url": f"https://dexscreener.com/solana/{pair}",
            "fetched_at": fetched_at, "provider_updated_at": None}


def detect(pool, previous, config, now):
    """Use rolling provider windows and comparable local observations, with explicit evidence."""
    if pool["liquidity_usd"] < config["min_liquidity_usd"]:
        # A large fall below the discovery floor is still worth surfacing.
        if not previous or previous["liquidity_usd"] < config["min_liquidity_usd"]:
            return []
    symbol = pool["symbol"]
    common = {"subject": pool["key"], "token": pool["token"], "symbol": symbol,
              "source": pool["source"], "source_url": pool["source_url"],
              "fetched_at": pool["fetched_at"], "provider_updated_at": pool["provider_updated_at"],
              "evidence_type": "provider_observation", "pool": pool}
    found = []

    def add(kind, headline, reason, score, metrics, basis):
        if pool["boosts"]:
            metrics = {**metrics, "Paid boosts": pool["boosts"]}
        found.append({**common, "kind": kind, "headline": headline, "reason": reason,
                      "score": score, "metrics": metrics, "basis": basis})

    m5, h1 = pool["volume_m5"], pool["volume_h1"]
    # The hourly total includes the newest five minutes: remove them before comparing paces.
    preceding_pace = (h1 - m5) / 11 if h1 is not None and h1 > m5 else None
    ratio = m5 / preceding_pace if preceding_pace and preceding_pace > 0 else None
    if pool["liquidity_usd"] >= config["min_liquidity_usd"] and m5 >= config["min_volume_m5_usd"]:
        if ratio is not None and ratio >= config["volume_acceleration"]:
            add("volume_acceleration", f"{symbol}: five-minute volume accelerates",
                f"The latest five-minute window is {ratio:.1f}× the average five-minute pace of the preceding 55 minutes. Activity alone does not establish organic demand.",
                min(95, 75 + int(math.log2(ratio / config["volume_acceleration"]) * 5)),
                {"5m volume": m5, "Previous 5m pace": preceding_pace, "Acceleration": round(ratio, 2),
                 "Liquidity": pool["liquidity_usd"]}, {"ratio_band": min(20, int(ratio))})
        change = pool["price_change_m5"]
        if change is not None and abs(change) >= config["price_move_m5_pct"]:
            add("price_move", f"{symbol}: price moves {change:+.1f}% in five minutes",
                "The provider reports a sharp five-minute price change with activity above the minimum volume and liquidity thresholds. Its cause is unverified.",
                min(90, 75 + int(abs(change) / 10)),
                {"5m change": change, "5m volume": m5, "Liquidity": pool["liquidity_usd"]},
                {"direction": "up" if change > 0 else "down", "change_band": int(abs(change) / 15)})
        if pool["created_at"]:
            created = dt.datetime.fromisoformat(pool["created_at"])
            age = (now - created).total_seconds()
            if 0 <= age <= config["new_pool_max_age_seconds"]:
                add("new_active_pool", f"{symbol}: a new pool clears the activity threshold",
                    "A recently created pool has cleared the configured activity and liquidity floors. Discovery is not endorsement or evidence of a safe token.",
                    65, {"Pool age (minutes)": round(age / 60, 1), "5m volume": m5,
                         "Liquidity": pool["liquidity_usd"]}, {"created_at": pool["created_at"]})
    if previous and previous["source"] == pool["source"]:
        elapsed = (now - dt.datetime.fromisoformat(previous["fetched_at"])).total_seconds()
        before, after = previous["liquidity_usd"], pool["liquidity_usd"]
        if 10 <= elapsed <= config["source_stale_seconds"] and before > 0:
            change = (after - before) / before * 100
            if abs(change) >= config["liquidity_change_pct"] and abs(after - before) >= config["min_liquidity_change_usd"]:
                add("liquidity_change", f"{symbol}: quoted liquidity changes {change:+.1f}%",
                    "USD liquidity changed between two local fetches. Token prices can change this value; a liquidity withdrawal or rug has not been established.",
                    90 if change < 0 else 75,
                    {"Previous liquidity": before, "Liquidity": after, "Change (%)": round(change, 2),
                     "Elapsed (seconds)": round(elapsed)},
                    {"direction": "up" if change > 0 else "down", "previous_fetched_at": previous["fetched_at"]})
    return found
