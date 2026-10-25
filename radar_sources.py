"""Bounded public-market polling and optional, explicitly enabled social/chain streams."""

import datetime as dt
import json
import os
import re
import time
import urllib.request
from urllib.parse import urlsplit

from news_engine import UTC, canonical_url, digest, timestamp
from radar_store import RadarStore
from research import NoRedirect, read_json
from signals import ADDRESS, label, normalize_dex

DEX = "https://api.dexscreener.com"
X_RULES = "https://api.x.com/2/tweets/search/stream/rules"
X_STREAM = "https://api.x.com/2/tweets/search/stream?tweet.fields=created_at,author_id,edit_history_tweet_ids&expansions=author_id&user.fields=username"
MAX_BODY = 4_000_000


def load_radar_config(path, engine_config):
    config = read_json(path)
    if config.get("schema_version") != 1:
        raise ValueError("Unsupported radar configuration version.")
    integer_limits = {"poll_seconds": (15, 300), "discovery_seconds": (60, 1800),
        "source_stale_seconds": (30, 600), "candidate_ttl_seconds": (60, 3600),
        "subject_cooldown_seconds": (300, 86400), "watch_ttl_seconds": (300, 86400),
        "max_watched_tokens": (1, 120), "new_pool_max_age_seconds": (60, 86400),
        "auto_research_score": (50, 100), "urgent_research_score": (50, 100), "background_interval_seconds": (3600, 86400),
        "background_calls_per_day": (0, 24), "triggered_calls_per_day": (0, 24),
        "triggered_min_spacing_seconds": (60, 86400), "x_max_posts_per_day": (1, 10000),
        "solana_max_events_per_day": (1, 20000)}
    for field, (minimum, maximum) in integer_limits.items():
        if type(config.get(field)) is not int or not minimum <= config[field] <= maximum:
            raise ValueError(f"Invalid radar configuration: {field}.")
    for field in ("min_liquidity_usd", "min_volume_m5_usd", "volume_acceleration", "price_move_m5_pct", "liquidity_change_pct", "min_liquidity_change_usd"):
        value = config.get(field)
        if type(value) not in (int, float) or not 0 < value <= 1e12:
            raise ValueError(f"Invalid radar threshold: {field}.")
    if config["background_calls_per_day"] + config["triggered_calls_per_day"] > engine_config["daily_research_calls"]:
        raise ValueError("Radar lane allocations exceed the engine's daily research cap.")
    if config["urgent_research_score"] < config["auto_research_score"]:
        raise ValueError("Urgent research threshold must be at least the automatic research threshold.")
    if config["source_stale_seconds"] < config["poll_seconds"] * 2:
        raise ValueError("Allow at least two poll intervals before declaring market data stale.")
    for field, limit, pattern in (("watch_tokens", 120, ADDRESS), ("solana_mentions", 10, ADDRESS),
                                   ("x_handles", 20, re.compile(r"[A-Za-z0-9_]{1,15}\Z"))):
        values = config.get(field)
        if not isinstance(values, list) or len(values) > limit or any(not isinstance(v, str) or not pattern.fullmatch(v) for v in values):
            raise ValueError(f"Invalid radar watchlist: {field}.")
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate radar watchlist entries: {field}.")
    return config


def request_json(url, *, headers=None, payload=None):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ("api.dexscreener.com", "api.x.com") or parsed.username or parsed.password:
        raise ValueError("Radar HTTP source must be a supported public API.")
    request = urllib.request.Request(url, headers={"User-Agent": "ExitLiquidityRadar/1.0", "Accept": "application/json", **(headers or {})},
                                     data=json.dumps(payload).encode() if payload is not None else None)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.build_opener(NoRedirect).open(request, timeout=6) as response:
        body = response.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise ValueError("Source response exceeds the size limit.")
    return json.loads(body)


class MarketCollector:
    def __init__(self, store, config, fetch=request_json, clock=None):
        self.store, self.config, self.fetch = store, config, fetch
        self.clock = clock or (lambda: dt.datetime.now(UTC))
        self.next_discovery = self.next_poll = 0
        self.poll_failures = 0
        self.last_poll_at = None

    def discover(self, now):
        profiles_url = DEX + "/token-profiles/latest/v1"
        try:
            body = self.fetch(profiles_url)
            if not isinstance(body, list):
                raise ValueError("Expected token profiles.")
            tokens = [p["tokenAddress"] for p in body if isinstance(p, dict) and p.get("chainId") == "solana"
                      and isinstance(p.get("tokenAddress"), str) and ADDRESS.fullmatch(p["tokenAddress"])]
            self.store.add_tokens(tokens[:120], now, "dexscreener_profiles")
            self.store.health("dex_discovery", "ok", now, discovered=len(tokens), note="Profiles are discovery leads, not endorsements.")
        except (OSError, ValueError, TypeError, KeyError):
            self.store.health("dex_discovery", "error", now, note="Profile discovery failed; no old response was substituted.")

    def poll(self, now):
        tokens = self.store.watched_tokens(self.config, now)
        if not tokens:
            self.store.health("dexscreener", "waiting", now, note="Waiting for Solana tokens from discovery or the explicit watchlist.")
            return
        successes, failures, valid = 0, 0, 0
        known_pools = {row[0] for row in self.store.db.execute("SELECT key FROM radar_pools")}
        for index in range(0, len(tokens), 30):
            url = DEX + "/tokens/v1/solana/" + ",".join(tokens[index:index + 30])
            try:
                body = self.fetch(url)
                if not isinstance(body, list):
                    raise ValueError("Expected market pairs.")
                fetched = self.clock()
                allowed_tokens = set(tokens[index:index + 30])
                for raw in body[:400]:
                    pool = normalize_dex(raw, url, timestamp(fetched))
                    if pool and pool["token"] in allowed_tokens and (pool["liquidity_usd"] >= self.config["min_liquidity_usd"] or pool["key"] in known_pools):
                        self.store.ingest_pool(pool, self.config, fetched)
                        valid += 1
                successes += 1
            except (OSError, ValueError, TypeError, AttributeError):
                failures += 1
        self.poll_failures = self.poll_failures + 1 if not successes else 0
        status = "degraded" if successes and failures else ("ok" if successes and valid else "error")
        self.store.health("dexscreener", status, self.clock(), pairs=valid, batches=successes,
                          failed_batches=failures, note="Fetch time is known; the API does not supply a snapshot update timestamp.")

    def tick(self, now, monotonic=None):
        clock = time.monotonic() if monotonic is None else monotonic
        if clock >= self.next_discovery:
            self.discover(now)
            self.next_discovery = clock + self.config["discovery_seconds"]
        if clock >= self.next_poll:
            self.poll(now)
            self.last_poll_at = timestamp(self.clock())
            self.next_poll = clock + min(600, self.config["poll_seconds"] * 2 ** min(4, self.poll_failures))


def x_signal(message, handles, now):
    post = message.get("data") or {}
    post_id = post.get("id", "")
    if not isinstance(post_id, str) or not post_id.isdigit():
        return None
    authors = {user.get("id"): user.get("username", "") for user in message.get("includes", {}).get("users", [])}
    handle = authors.get(post.get("author_id"), "")
    if handle.lower() not in {h.lower() for h in handles}:
        return None
    try:
        created = dt.datetime.fromisoformat(post["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None or not -60 <= (now - created).total_seconds() <= 600:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    text = label(post.get("text"), 1600)
    material = bool(re.search(r"\b(launch(?:ed|ing)?|migrat\w*|fee[s]?|exploit\w*|incident|halt\w*|paus\w*|outage|upgrad\w*|security)\b", text, re.I))
    history = post.get("edit_history_tweet_ids") or [post_id]
    original = history[0] if isinstance(history[0], str) and history[0].isdigit() else post_id
    return {"event_key": f"x:{original}", "subject": f"x:{original}", "token": "", "symbol": "",
            "source": "x_stream", "source_url": f"https://x.com/i/status/{post_id}",
            "fetched_at": timestamp(now), "provider_updated_at": timestamp(created),
            "evidence_type": "firsthand_post", "kind": "source_post", "score": 85 if material else 50,
            "headline": f"@{handle}: {text[:140]}", "reason": "A watched account published this post. Its claims still require investigation.",
            "metrics": {"Author": "@" + handle, "Published": timestamp(created)},
            "post_text": text, "basis": {"post_id": post_id, "text": text}}


def run_x_stream(state_dir, config, stop):
    store = RadarStore(state_dir)
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if os.environ.get("RADAR_ENABLE_X") != "1" or not token or not config["x_handles"]:
        store.health("x_stream", "unconfigured", dt.datetime.now(UTC), note="Needs explicitly enabled X API access and an account watchlist.")
        store.close()
        return
    headers = {"Authorization": "Bearer " + token}
    failures = 0
    try:
        while not stop.is_set():
            now = dt.datetime.now(UTC)
            meter = store.meta("x_received", {"day": now.date().isoformat(), "count": 0})
            if meter["day"] != now.date().isoformat():
                meter = {"day": now.date().isoformat(), "count": 0}
            if meter["count"] >= config["x_max_posts_per_day"]:
                store.health("x_stream", "capped", now, note="Daily received-post cap reached. Upstream pricing is separate from the Grok quota.")
                stop.wait(30)
                continue
            try:
                # Add only this application's missing rules; never delete another consumer's rules.
                existing = request_json(X_RULES, headers=headers).get("data", [])
                values = {r.get("value") for r in existing}
                rules = [{"value": f"from:{handle} -is:retweet", "tag": "elfm-" + digest(handle.lower())[:12]}
                         for handle in config["x_handles"] if f"from:{handle} -is:retweet" not in values]
                if rules:
                    request_json(X_RULES, headers=headers, payload={"add": rules})
                request = urllib.request.Request(X_STREAM, headers=headers)
                store.health("x_stream", "connecting", now, note="Connecting to the configured account stream.")
                with urllib.request.build_opener(NoRedirect).open(request, timeout=35) as response:
                    failures = 0
                    while not stop.is_set():
                        line = response.readline(512001)
                        if not line:
                            raise OSError("Stream closed")
                        if len(line) > 512000:
                            raise ValueError("Oversized stream record")
                        now = dt.datetime.now(UTC)
                        store.health("x_stream", "ok", now, note="Stream keep-alives indicate transport health, not claim verification.")
                        if not line.strip():
                            continue
                        if meter["day"] != now.date().isoformat():
                            meter = {"day": now.date().isoformat(), "count": 0}
                        meter["count"] += 1
                        store.set_meta("x_received", meter)
                        message = json.loads(line)
                        signal = x_signal(message, config["x_handles"], now)
                        if signal and store.stream_event("x-" + signal["basis"]["post_id"], "x_stream", signal, now):
                            store.upsert_signal(signal, config, now)
                        if meter["count"] >= config["x_max_posts_per_day"]:
                            break
            except (OSError, ValueError, TypeError, AttributeError):
                failures += 1
                store.health("x_stream", "error", dt.datetime.now(UTC), note="Stream disconnected. An ingestion gap may exist; repeated posts are deduplicated.")
                stop.wait(min(300, 5 * 2 ** min(failures, 6)))
    finally:
        store.close()


def solana_signal(message, mention, now):
    result = message.get("params", {}).get("result", {})
    value = result.get("value", {})
    signature = value.get("signature", "")
    if value.get("err") is not None or not isinstance(signature, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{64,90}", signature):
        return None
    logs = [label(line, 500) for line in (value.get("logs") or [])[:40] if isinstance(line, str)]
    return {"event_key": "solana:" + digest({"mention": mention, "signature": signature})[:32], "subject": f"solana:{mention}", "token": "", "symbol": "",
            "source": "solana_stream", "source_url": f"https://solscan.io/tx/{signature}",
            "fetched_at": timestamp(now), "provider_updated_at": None, "evidence_type": "transaction_log",
            "kind": "watched_transaction", "score": 60,
            "headline": f"Transaction mentions watched address {mention[:6]}…{mention[-4:]}",
            "reason": "A confirmed transaction log mentions a watched address. Mention alone establishes neither wallet ownership nor a sale, migration or exploit.",
            "metrics": {"Slot": result.get("context", {}).get("slot"), "Watched address": mention},
            "logs": logs, "basis": {"signature": signature, "mention": mention}}


def run_solana_stream(state_dir, config, stop):
    store = RadarStore(state_dir)
    endpoint = os.environ.get("SOLANA_RPC_WS_URL", "").strip()
    if os.environ.get("RADAR_ENABLE_SOLANA") != "1" or not endpoint or not config["solana_mentions"]:
        store.health("solana_stream", "unconfigured", dt.datetime.now(UTC), note="Needs an explicitly enabled Solana WebSocket endpoint and watched addresses.")
        store.close()
        return
    try:
        parsed = urlsplit(endpoint)
        canonical_url("https://" + parsed.netloc + (parsed.path or "/"))
        if parsed.scheme != "wss":
            raise ValueError("Secure WebSocket required")
        import websocket
    except (ImportError, ValueError):
        store.health("solana_stream", "error", dt.datetime.now(UTC), note="Secure endpoint or optional websocket-client dependency is unavailable.")
        store.close()
        return
    failures = 0
    try:
        while not stop.is_set():
            connection = None
            if store.stream_count("solana_stream", dt.datetime.now(UTC)) >= config["solana_max_events_per_day"]:
                store.health("solana_stream", "capped", dt.datetime.now(UTC), note="Daily stored-event cap reached; collection resumes on the next UTC day.")
                stop.wait(30)
                continue
            try:
                store.health("solana_stream", "connecting", dt.datetime.now(UTC), note="Subscribing to configured addresses at confirmed commitment.")
                connection = websocket.create_connection(endpoint, timeout=20, redirect_limit=0)
                subscriptions, requests = {}, {}
                for index, mention in enumerate(config["solana_mentions"], 1):
                    requests[index] = mention
                    connection.send(json.dumps({"jsonrpc": "2.0", "id": index, "method": "logsSubscribe",
                                                "params": [{"mentions": [mention]}, {"commitment": "confirmed"}]}))
                failures = 0
                connection.settimeout(5)
                while not stop.is_set():
                    try:
                        raw = connection.recv()
                    except websocket.WebSocketTimeoutException:
                        connection.ping()
                        continue
                    if not raw or len(raw) > 512000:
                        raise ValueError("Invalid stream record")
                    message = json.loads(raw)
                    now = dt.datetime.now(UTC)
                    if "error" in message:
                        raise ValueError("Subscription rejected")
                    if message.get("id") in requests and isinstance(message.get("result"), int):
                        subscriptions[message["result"]] = requests[message["id"]]
                    mention = subscriptions.get(message.get("params", {}).get("subscription"))
                    signal = solana_signal(message, mention, now) if mention else None
                    if signal and store.stream_event("solana-" + digest(signal["event_key"]), "solana_stream", signal, now):
                        store.upsert_signal(signal, config, now)
                    if store.stream_count("solana_stream", now) >= config["solana_max_events_per_day"]:
                        break
                    store.health("solana_stream", "ok", now, subscriptions=len(subscriptions),
                                 note="Confirmed transaction mentions only. Reconnect gaps require investigation; this source does not backfill history.")
            except (OSError, ValueError, TypeError, AttributeError, websocket.WebSocketException):
                failures += 1
                store.health("solana_stream", "error", dt.datetime.now(UTC), note="Connection lost; a gap in transaction coverage may exist.")
                stop.wait(min(300, 5 * 2 ** min(failures, 6)))
            finally:
                if connection:
                    connection.close()
    finally:
        store.close()
