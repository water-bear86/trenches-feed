#!/usr/bin/env python3
"""Persistent, inspectable research-to-bulletin pipeline. No broadcasting or automatic publishing."""

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
import sqlite3
import sys
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from build_feed import SOURCES, collect
from render_feed import FORMATS, parse_time, render_to, spoken_wc, validate_copy, write_artifacts
from research import ResearchError, read_json, run_research

BASE = Path(__file__).resolve().parent
UTC = dt.timezone.utc
COPY_FIELDS = ("text_medium", "text_extra", "text_short", "text_flash")
SEGMENTS = [
    {"id": "trenches", "name": "Memecoin Markets", "intro": "First, developments in memecoin markets."},
    {"id": "broader", "name": "Crypto Markets and Policy", "intro": "In the wider cryptocurrency market."},
    {"id": "metas", "name": "Emerging Trends", "intro": "Turning to emerging themes."},
]


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def timestamp(now):
    return now.astimezone(UTC).isoformat(timespec="seconds")


def market_source_url(value):
    if value in {canonical_url(url) for url in SOURCES.values()}:
        return True
    url = urlsplit(value)
    if url.scheme != "https" or url.username or url.password or url.port not in (None, 443):
        return False
    address = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
    if url.query:
        return False
    if url.hostname == "api.dexscreener.com":
        return bool(re.fullmatch(rf"/(?:tokens/v1/solana/{address}(?:,{address}){{0,29}}|latest/dex/pairs/solana/{address})", url.path))
    if url.hostname == "dexscreener.com":
        return bool(re.fullmatch(rf"/solana/{address}", url.path))
    return False


def canonical_url(value, event=False):
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\s\x00-\x1f]", value):
        raise ValueError("Invalid source URL.")
    url = urlsplit(value)
    host = (url.hostname or "").lower().rstrip(".")
    if url.scheme != "https" or not host or url.username or url.password or url.port not in (None, 443):
        raise ValueError("Sources must use HTTPS without credentials or custom ports.")
    if host == "localhost" or "." not in host or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError("Source URL must identify a public host.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Source URL must identify a public host.")
    if host.startswith("www."):
        host = host[4:]
    if host == "twitter.com":
        host = "x.com"
    path = url.path.rstrip("/") or "/"
    if host == "x.com":
        match = re.fullmatch(r"/(?:i|[A-Za-z0-9_]+)/status/(\d+)", path)
        if not match:
            raise ValueError("X sources must be specific posts, not profiles or trending pages.")
        path = f"/i/status/{match[1]}"
    if event and path == "/" and not url.query:
        raise ValueError("Event URL must identify a specific development, not a homepage.")
    query = sorted((key, val) for key, val in parse_qsl(url.query, keep_blank_values=True)
                   if not key.lower().startswith("utm_") and key.lower() not in ("fbclid", "gclid")
                   and not (host == "x.com" and key in ("s", "t")))
    return urlunsplit(("https", host, path, urlencode(query), ""))


def load_config(path=BASE / "engine-config.json"):
    config = read_json(path)
    limits = {"lookback_hours": (1, 72), "edition_ttl_minutes": (1, 180), "max_stories": (1, 30),
              "story_cooldown_minutes": (0, 180), "daily_research_calls": (0, 96),
              "max_turns": (1, 6), "max_output_tokens": (1000, 20000), "timeout_seconds": (10, 300)}
    if config.get("schema_version") != 1:
        raise ValueError("Unsupported engine configuration version.")
    if config.get("backend") not in ("xai", "grok-cli"):
        raise ValueError("Research backend must be xai or grok-cli.")
    if config.get("reasoning_effort") not in ("low", "medium", "high"):
        raise ValueError("CLI reasoning_effort must be low, medium or high.")
    for field, (low, high) in limits.items():
        if type(config.get(field)) is not int or not low <= config[field] <= high:
            raise ValueError(f"Invalid configuration: {field} must be between {low} and {high}.")
    if type(config.get("reuse_target")) not in (float, int) or not 0 <= config["reuse_target"] <= 1:
        raise ValueError("reuse_target must be between zero and one.")
    observation_ttl = config.get("market_observation_ttl_minutes", config["edition_ttl_minutes"])
    if type(observation_ttl) is not int or not 1 <= observation_ttl <= config["edition_ttl_minutes"]:
        raise ValueError("Market observation lifetime must fit within the edition lifetime.")
    if not isinstance(config.get("model"), str) or len(config["model"]) > 100:
        raise ValueError("Invalid model setting.")
    for field, limit in (("breaking_story_ids", 30), ("allowed_x_handles", 20), ("web_domains", 5), ("coverage_terms", 50)):
        values = config.get(field)
        if not isinstance(values, list) or len(values) > limit or any(not isinstance(v, str) or not v for v in values):
            raise ValueError(f"Invalid configuration: {field}.")
    if not config["coverage_terms"] or any(len(term) > 60 for term in config["coverage_terms"]):
        raise ValueError("coverage_terms must contain bounded, explicit editorial terms.")
    for handle in config["allowed_x_handles"]:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", handle):
            raise ValueError("X handles must be written without @.")
    for domain in config["web_domains"]:
        canonical_url(f"https://{domain}/")
    return config


def validate_snapshot(snapshot, config, now):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("sources"), dict):
        raise ValueError("Invalid market snapshot.")
    collected = parse_time(snapshot["collected_at"])
    cutoff = now - dt.timedelta(minutes=config["edition_ttl_minutes"])
    if not cutoff < collected <= now + dt.timedelta(seconds=60):
        raise ValueError("Market snapshot is future or expired.")
    if len(snapshot["sources"]) > 32:
        raise ValueError("Too many market sources.")
    for source in snapshot["sources"].values():
        if source.get("status") not in ("ok", "error"):
            raise ValueError("Invalid market source status.")
        if source["status"] == "ok":
            observed = parse_time(source["observed_at"])
            if not cutoff < observed <= collected + dt.timedelta(seconds=60):
                raise ValueError("A market observation is future or expired.")
            canonical_url(source["source_url"])
            if not isinstance(source.get("data"), (dict, list)) or not source["data"]:
                raise ValueError("Successful market source has no data.")


def normalized_story(raw, citations, config, researched_at, now):
    if not isinstance(raw, dict):
        raise ValueError("Research story must be an object.")
    required = {"event_key", "event_url", "segment", "priority", "headline", "occurred_at", "status",
                "correction_of", "claims", *COPY_FIELDS}
    if not required <= raw.keys():
        raise ValueError("Research story is missing required fields.")
    key = raw["event_key"]
    if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9:._/-]{2,159}", key):
        raise ValueError("event_key must be a stable lowercase development key.")
    event_url = canonical_url(raw["event_url"], event=True)
    if event_url not in citations:
        raise ValueError("Event URL was not encountered in the research citations.")
    occurred = parse_time(raw["occurred_at"])
    if occurred > min(now, researched_at) + dt.timedelta(seconds=60):
        raise ValueError("Research contains a future event.")
    if occurred + dt.timedelta(hours=config["lookback_hours"]) <= now:
        raise ValueError("Research contains an expired event.")
    if raw["status"] not in ("reported", "unverified", "withdrawn"):
        raise ValueError("Unknown research status.")
    if raw["segment"] not in {s["id"] for s in SEGMENTS} or type(raw["priority"]) is not int or raw["priority"] not in (1, 2, 3):
        raise ValueError("Invalid story segment or priority.")
    if not isinstance(raw["correction_of"], str) or len(raw["correction_of"]) > 100:
        raise ValueError("Invalid correction target.")
    claims = raw["claims"]
    if not isinstance(claims, list) or not 1 <= len(claims) <= 12:
        raise ValueError("Every story needs between one and twelve sourced claims.")
    facts, urls, all_claims = [], set(), []
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("fact"), str) or not 1 <= len(claim["fact"]) <= 1000:
            raise ValueError("Invalid claim text.")
        references = claim.get("source_urls")
        if not isinstance(references, list) or not 1 <= len(references) <= 8:
            raise ValueError("Every claim needs source URLs.")
        references = sorted({canonical_url(url) for url in references})
        if not set(references) <= citations:
            raise ValueError("A claim cites a URL absent from the research citations.")
        fact = " ".join(claim["fact"].split())
        if not fact:
            raise ValueError("Claim text is empty.")
        all_claims.append({"fact": fact, "source_urls": references})
        facts.append(re.sub(r"\W+", " ", fact.casefold()).strip())
        urls.update(references)
    if event_url not in urls:
        raise ValueError("The original event URL must support at least one claim.")
    for field in ("headline", *COPY_FIELDS):
        validate_copy(raw[field], allow_empty=field == "text_extra" or raw["status"] == "withdrawn")
    for field, maximum in zip(COPY_FIELDS, (180, 100, 85, 40)):
        if spoken_wc(raw[field]) > maximum:
            raise ValueError(f"Research copy exceeds the {field} word limit.")
    if len(raw["headline"]) > 240:
        raise ValueError("Headline exceeds the length limit.")
    # A still-valid announcement can carry into later editions, without renewing its event deadline.
    expires = occurred + dt.timedelta(hours=config["lookback_hours"])
    observation = market_source_url(event_url)
    if observation:
        expires = min(expires, occurred + dt.timedelta(minutes=config.get("market_observation_ttl_minutes", config["edition_ttl_minutes"])))
    body = {field: raw[field].strip() for field in ("headline", *COPY_FIELDS)}
    body.update(event_key=key, event_url=event_url, segment=raw["segment"], priority=raw["priority"],
                timestamp=timestamp(occurred), status=raw["status"], claims=all_claims,
                source_urls=sorted(urls), expires_at=timestamp(expires),
                researched_at=timestamp(researched_at), verification="source_attributed",
                evidence_type="market_observation" if observation else "report")
    # Headlines and prose do not create new factual revisions. Evidence changes do.
    fingerprint = digest({"facts": sorted(facts), "sources": sorted(urls), "status": body["status"]})
    aliases = {f"event:{key}", f"url:{event_url}", f"facts:{digest(sorted(facts))}"}
    if event_url.startswith("https://api.dexscreener.com/tokens/v1/solana/"):
        # A batch can contain many independent pools, so its URL cannot be their shared identity.
        aliases.discard(f"url:{event_url}")
    return body, fingerprint, aliases, raw["correction_of"]


class Engine:
    def __init__(self, state_dir, config):
        self.state_dir, self.config = Path(state_dir), config
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.state_dir / "newsroom.sqlite", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS stories(id TEXT PRIMARY KEY, fingerprint TEXT, body TEXT);
            CREATE INDEX IF NOT EXISTS stories_current ON stories(
                json_extract(body, '$.status'), json_extract(body, '$.expires_at'));
            CREATE TABLE IF NOT EXISTS aliases(alias TEXT PRIMARY KEY, story_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions(story_id TEXT, revision INTEGER, body TEXT,
                PRIMARY KEY(story_id, revision));
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, fingerprint TEXT, report TEXT);
            CREATE TABLE IF NOT EXISTS editions(sequence INTEGER PRIMARY KEY, pool TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, day TEXT, status TEXT, usage TEXT);
        """)

    def close(self):
        self.db.close()

    def stories(self):
        return [json.loads(row["body"]) for row in self.db.execute("SELECT body FROM stories ORDER BY rowid")]

    def active_stories(self, now, status="reported"):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT body FROM stories WHERE json_extract(body, '$.status')=? "
            "AND json_extract(body, '$.expires_at')>? ORDER BY rowid", (status, timestamp(now)))]

    def previous_pool(self):
        row = self.db.execute("SELECT pool FROM editions ORDER BY sequence DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def reserve_request(self, research_id, now, *, lane="manual", lane_limit=None):
        if lane not in ("manual", "background", "triggered"):
            raise ValueError("Unknown research lane.")
        if lane_limit is not None and (type(lane_limit) is not int or lane_limit < 0):
            raise ValueError("Invalid research-lane limit.")
        day = now.astimezone(UTC).date().isoformat()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            count = self.db.execute("SELECT count(*) FROM requests WHERE day=?", (day,)).fetchone()[0]
            if count >= self.config["daily_research_calls"]:
                raise ResearchError("Daily research-call limit reached or live research is disabled.")
            lane_count = self.db.execute("SELECT count(*) FROM requests WHERE day=? AND json_extract(usage,'$.lane')=?", (day, lane)).fetchone()[0]
            if lane_limit is not None and lane_count >= lane_limit:
                raise ResearchError(f"Daily {lane} research limit reached.")
            self.db.execute("INSERT INTO requests VALUES (?,?,?,?)", (research_id, day, "reserved", encoded({"lane": lane})))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def finish_request(self, research_id, status, usage=None):
        row = self.db.execute("SELECT usage FROM requests WHERE id=?", (research_id,)).fetchone()
        previous = json.loads(row[0]) if row else {}
        provided = usage if isinstance(usage, dict) else {"usage_validation": "unavailable_or_invalid"}
        metadata = {**provided, "lane": previous.get("lane", "manual")}
        try:
            usage_json = encoded(metadata)
        except (TypeError, ValueError):
            usage_json = encoded({"lane": previous.get("lane", "manual"), "usage_validation": "invalid_provider_usage"})
        self.db.execute("UPDATE requests SET status=?,usage=? WHERE id=?", (status, usage_json, research_id))
        self.db.commit()

    def recent(self):
        return [{k: story[k] for k in ("id", "event_key", "event_url", "headline", "claims", "status", "revision")}
                for story in (json.loads(row[0]) for row in self.db.execute("SELECT body FROM stories ORDER BY rowid DESC LIMIT 24"))]

    def apply(self, envelope, output_dir, now=None, snapshot_at=None):
        now = now or dt.datetime.now(UTC)
        if envelope.get("schema_version") != 1:
            raise ValueError("Unsupported research envelope version.")
        research_id = envelope.get("research_id")
        if not isinstance(research_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", research_id):
            raise ValueError("Research ID is invalid.")
        input_hash = digest(envelope)
        existing = self.db.execute("SELECT * FROM runs WHERE id=?", (research_id,)).fetchone()
        if existing:
            if existing["fingerprint"] != input_hash:
                raise ValueError("Research ID has already been used for different input.")
            report = json.loads(existing["report"])
            # Recover a crash between committing the ledger and promoting its validated files.
            latest = Path(output_dir) / "latest.json"
            published_id = None
            if latest.exists():
                published_id = read_json(latest).get("research_id")
            if published_id != research_id:
                pool = self.previous_pool()
                if report["status"] == "rendered" and pool and pool["research_id"] == research_id:
                    render_to(pool, output_dir, now=now)
                elif report["status"] == "unavailable" and not self.select(pool, now):
                    self.invalidate_output(output_dir, envelope, now, report)
            return {**report, "status": "already_processed", "output_dir": str(Path(output_dir).resolve())}
        researched_at = parse_time(envelope["researched_at"])
        if researched_at > now + dt.timedelta(seconds=60) or researched_at + dt.timedelta(
                minutes=self.config["edition_ttl_minutes"]) <= now:
            raise ValueError("Research timestamp is future or expired.")
        provenance = envelope.get("provenance")
        if provenance not in ("xai_search", "grok_cli", "operator"):
            raise ValueError("Research provenance must be xai_search, grok_cli or operator.")
        raw_citations = envelope.get("citations")
        if not isinstance(raw_citations, list) or len(raw_citations) > 500:
            raise ValueError("Research citations must be a bounded URL list.")
        citations = set()
        for url in raw_citations:
            try:
                citations.add(canonical_url(url))
            except ValueError:
                # Providers include unused profiles/search pages too. Claimed references still fail closed below.
                continue
        raw_stories = envelope.get("stories")
        if not isinstance(raw_stories, list) or len(raw_stories) > self.config["max_stories"]:
            raise ValueError("Research stories exceed the configured limit.")
        validated, rejected = [], []
        coverage = re.compile(r"\b(?:" + "|".join(re.escape(term) for term in self.config["coverage_terms"]) + r")\b", re.I)
        for raw in raw_stories:
            if not isinstance(raw, dict):
                raise ValueError("Research story must be an object.")
            occurred = parse_time(raw["occurred_at"])
            if occurred + dt.timedelta(hours=self.config["lookback_hours"]) <= now:
                rejected.append({"event_key": str(raw.get("event_key", ""))[:160], "reason": "outside_lookback"})
                continue
            facts = " ".join(str(claim.get("fact", "")) for claim in raw.get("claims", []) if isinstance(claim, dict))
            known = self.db.execute("SELECT 1 FROM aliases WHERE alias IN (?,?)", (
                f"event:{raw.get('event_key', '')}", f"url:{canonical_url(raw.get('event_url', ''), event=True)}")).fetchone()
            if raw.get("correction_of"):
                known = known or self.db.execute("SELECT 1 FROM stories WHERE id=?", (raw["correction_of"],)).fetchone()
            if not known and not coverage.search(facts):
                rejected.append({"event_key": str(raw.get("event_key", ""))[:160], "reason": "outside_crypto_scope"})
                continue
            validated.append(normalized_story(raw, citations, self.config, researched_at, now))
        snapshot_at = snapshot_at or envelope.get("data_snapshot_at", envelope["researched_at"])
        if not now - dt.timedelta(minutes=self.config["edition_ttl_minutes"]) < parse_time(snapshot_at) <= now + dt.timedelta(seconds=60):
            raise ValueError("Market snapshot is future or expired.")
        report = {"research_id": research_id, "provenance": provenance, "new": [], "updated": [],
                  "unchanged": [], "withheld": [], "withdrawn": [], "rejected": rejected,
                  "output_dir": str(Path(output_dir).resolve())}
        staging = tempfile.TemporaryDirectory(prefix=".pending-", dir=self.state_dir)
        output_prepared = False
        self.db.execute("BEGIN IMMEDIATE")
        try:
            processed = {}
            for body, fingerprint, aliases, correction in validated:
                matches = {row[0] for alias in aliases for row in self.db.execute(
                    "SELECT story_id FROM aliases WHERE alias=?", (alias,))}
                if correction:
                    if not self.db.execute("SELECT 1 FROM stories WHERE id=?", (correction,)).fetchone():
                        raise ValueError("Correction target is unknown.")
                    matches.add(correction)
                if len(matches) > 1:
                    raise ValueError("Research ambiguously merges existing developments.")
                identity = {"event_url": body["event_url"], "event_key": body["event_key"]} if body["event_url"].startswith(
                    "https://api.dexscreener.com/tokens/v1/solana/") else body["event_url"]
                story_id = next(iter(matches), "story-" + digest(identity)[:20])
                if story_id in processed and processed[story_id] != fingerprint:
                    raise ValueError("Conflicting versions of one event in a research result.")
                processed[story_id] = fingerprint
                row = self.db.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
                old = json.loads(row["body"]) if row else None
                if old and researched_at < parse_time(old["last_seen_at"]):
                    raise ValueError("Older research cannot replace a more recent observation.")
                if row and row["fingerprint"] == fingerprint:
                    old["last_seen_at"] = timestamp(researched_at)
                    # Repeated coverage does not renew expiry, revision or its authored copy.
                    self.db.execute("UPDATE stories SET body=? WHERE id=?", (encoded(old), story_id))
                    report["unchanged"].append(story_id)
                else:
                    body.update(id=story_id, revision=(old["revision"] + 1 if old else 1),
                                first_seen_at=(old["first_seen_at"] if old else timestamp(researched_at)),
                                last_seen_at=timestamp(researched_at),
                                last_selected_at=(old.get("last_selected_at") if old else None))
                    self.db.execute("INSERT OR REPLACE INTO stories VALUES (?,?,?)", (story_id, fingerprint, encoded(body)))
                    self.db.execute("INSERT INTO revisions VALUES (?,?,?)", (story_id, body["revision"], encoded(body)))
                    report["updated" if old else "new"].append(story_id)
                for alias in aliases:
                    self.db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (alias, story_id))
                if body["status"] == "withdrawn":
                    report["withdrawn"].append(story_id)
                elif body["status"] == "unverified":
                    report["withheld"].append(story_id)
            previous = self.previous_pool()
            selected = self.select(previous, now)
            previous_versions = [(s["id"], s["revision"]) for s in previous["stories"]] if previous else []
            current_versions = [(s["id"], s["revision"]) for s in selected]
            if not selected and not previous:
                report["status"] = "withheld" if report["withheld"] else ("no_eligible_stories" if rejected else "empty_research")
            elif current_versions == previous_versions or not raw_stories:
                report["status"] = "unchanged" if raw_stories else "empty_research"
            elif not selected:
                report["status"] = "unavailable"
                if previous:
                    self.invalidate_output(staging.name, envelope, now, report)
                    output_prepared = True
            else:
                pool = self.pool(selected, previous, envelope, snapshot_at, now)
                bundle = render_to(pool, staging.name, now=now)
                output_prepared = True
                self.db.execute("INSERT INTO editions(pool) VALUES (?)", (encoded(pool),))
                for story in selected:
                    story["last_selected_at"] = timestamp(now)
                    self.db.execute("UPDATE stories SET body=? WHERE id=?", (encoded(story), story["id"]))
                # Immutable research IDs avoid the old same-day pool overwrite.
                pool_dir = self.state_dir / "editions"
                pool_dir.mkdir(exist_ok=True, mode=0o700)
                (pool_dir / f"{research_id}.pool.json").write_text(json.dumps(pool, indent=2, ensure_ascii=False) + "\n")
                report.update(status="rendered", selected=[s["id"] for s in selected], reuse=pool["reuse"],
                              formats={fmt: {k: ed[k] for k in ("word_count", "target_words", "est_read_seconds", "underfilled")}
                                       for fmt, ed in bundle["formats"].items()})
            self.db.execute("INSERT INTO runs VALUES (?,?,?)", (research_id, input_hash, encoded(report)))
            self.db.commit()
            if output_prepared:
                write_artifacts(output_dir, {path.name: path.read_text(encoding="utf-8")
                                            for path in Path(staging.name).iterdir()})
        except Exception:
            self.db.rollback()
            raise
        finally:
            staging.cleanup()
        (self.state_dir / "last-run.json").write_text(json.dumps(report, indent=2) + "\n")
        return report

    def select(self, previous, now):
        active = {s["id"]: s for s in self.active_stories(now)}
        old_ids = [s["id"] for s in previous["stories"]] if previous else []
        breaking_ids = set(self.config["breaking_story_ids"])
        breaking = [s for s in active.values() if s["id"] in breaking_ids]
        old = [active[sid] for sid in old_ids if sid in active and sid not in breaking_ids]
        cutoff = now - dt.timedelta(minutes=self.config["story_cooldown_minutes"])
        new = [s for sid, s in active.items() if sid not in old_ids and sid not in breaking_ids
               and (not s.get("last_selected_at") or parse_time(s["last_selected_at"]) <= cutoff)]
        new.sort(key=lambda s: (s["priority"], s["first_seen_at"], s["id"]))
        maximum = self.config["max_stories"]
        breaking = breaking[:maximum]
        space = maximum - len(breaking)
        new_slots = min(len(new), max(1, maximum - math.ceil(maximum * self.config["reuse_target"]))) if new else 0
        keep = min(len(old), max(0, space - new_slots))
        selected = (breaking + old[:keep] + new[:maximum - len(breaking) - keep])[:maximum]
        for story in selected:
            story["breaking"] = story["id"] in breaking_ids
        return selected

    def pool(self, stories, previous, envelope, snapshot_at, now):
        previous_versions = {(s["id"], s["revision"]) for s in previous["stories"]} if previous else set()
        words = lambda s: spoken_wc(s["text_medium"] + " " + s["text_extra"])
        total = sum(words(s) for s in stories)
        reused = sum(words(s) for s in stories if (s["id"], s["revision"]) in previous_versions)
        pool = {"edition": now.astimezone(ZoneInfo("America/Vancouver")).date().isoformat(),
                "title_base": "{{STATION_NAME}} News: Crypto and Digital Markets",
                "generated_at": envelope["researched_at"], "researched_at": envelope["researched_at"],
                "data_snapshot_at": snapshot_at, "research_id": envelope["research_id"],
                "provenance": envelope["provenance"], "expires_at": timestamp(min(
                    parse_time(envelope["researched_at"]) + dt.timedelta(minutes=self.config["edition_ttl_minutes"]),
                    min(parse_time(s["expires_at"]) for s in stories))),
                "style": "Source-attributed public-broadcaster copy; character dialogue belongs to the station",
                "disclaimer": "This report is for information only. Token mentions are news, not recommendations.",
                "segments": SEGMENTS, "stories": stories,
                "withdrawn_story_ids": [s["id"] for s in self.active_stories(now, status="withdrawn")],
                "reuse": {"target": self.config["reuse_target"], "actual": round(reused / total, 3) if total else 0,
                          "basis": "estimated_spoken_words", "audio_reuse_measured": False}}
        for style in ("long", "short", "flash"):
            pool[f"intro_{style}"] = "{{TIME_OF_DAY_GREETING}}. It's {{TIME_SPOKEN}}. This is {{STATION_NAME}} News. I'm {{ANCHOR_NAME}}."
            pool[f"outro_{style}"] = "That's the news. I'm {{ANCHOR_NAME}}."
        return pool

    @staticmethod
    def invalidate_output(output_dir, envelope, now, report):
        # A withdrawal is new evidence, not an outage: do not leave a known-bad bulletin available.
        marker = {"available": False, "full_script": "", "formats": {},
                  "generated_at": envelope["researched_at"], "rendered_at": timestamp(now),
                  "expires_at": timestamp(now), "research_id": envelope["research_id"],
                  "reason": "No eligible stories remain", "withdrawn_story_ids": report["withdrawn"]}
        text = json.dumps(marker, indent=2) + "\n"
        write_artifacts(output_dir, {"latest.json": text, **{f"latest-{fmt}.json": text for fmt in FORMATS},
                                     "feed.xml": '<?xml version="1.0"?><rss version="2.0"><channel><title>Trenches News</title></channel></rss>\n'})


@contextlib.contextmanager
def run_lock(state_dir):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / "engine.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another engine run is using this state directory.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "run", "status"))
    parser.add_argument("--config", type=Path, default=BASE / "engine-config.json")
    parser.add_argument("--state-dir", type=Path, default=BASE / ".state")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--research-file", type=Path, help="Import source-attributed operator research; makes no model call")
    parser.add_argument("--market-file", type=Path)
    parser.add_argument("--sources", help="Comma-separated names from build_feed.SOURCES for collection")
    args = parser.parse_args()
    os.umask(0o077)
    engine = None
    try:
        config = load_config(args.config)
        with run_lock(args.state_dir):
            engine = Engine(args.state_dir, config)
            now = dt.datetime.now(UTC)
            if args.command == "status":
                print(json.dumps({"stories": engine.db.execute("SELECT count(*) FROM stories").fetchone()[0], "daily_research_calls": config["daily_research_calls"],
                                  "requests_today": engine.db.execute("SELECT count(*) FROM requests WHERE day=?", (now.date().isoformat(),)).fetchone()[0],
                                  "last_edition": (engine.previous_pool() or {}).get("research_id")}, indent=2))
                return 0
            if args.command == "collect":
                snapshot = collect(args.state_dir / "market" / uuid.uuid4().hex,
                                   args.sources.split(",") if args.sources else None)
                print(json.dumps({"collected_at": snapshot["collected_at"],
                                  "sources": {name: s["status"] for name, s in snapshot["sources"].items()}}, indent=2))
                return 0 if any(s["status"] == "ok" for s in snapshot["sources"].values()) else 1
            snapshot = read_json(args.market_file) if args.market_file else None
            if snapshot:
                validate_snapshot(snapshot, config, now)
            if args.research_file:
                envelope = read_json(args.research_file)
            else:
                if config["backend"] == "xai" and (not os.environ.get("XAI_API_KEY") or not (os.environ.get("XAI_MODEL") or config["model"])):
                    raise ResearchError("Set XAI_API_KEY and XAI_MODEL before live research.")
                if config["backend"] == "grok-cli" and not shutil.which("grok"):
                    raise ResearchError("Install and sign into the grok CLI before live research.")
                research_id = uuid.uuid4().hex
                snapshot = snapshot or collect(args.state_dir / "market" / research_id,
                                               args.sources.split(",") if args.sources else None)
                validate_snapshot(snapshot, config, dt.datetime.now(UTC))
                if not any(s["status"] == "ok" for s in snapshot["sources"].values()):
                    raise ResearchError("All market sources failed; research was not requested.")
                now = dt.datetime.now(UTC)
                engine.reserve_request(research_id, now)
                try:
                    envelope = run_research(config, snapshot, engine.recent(), research_id, now,
                                            capture_path=args.state_dir / "provider" / f"{research_id}.json")
                except Exception:
                    engine.finish_request(research_id, "failed_or_uncertain")
                    raise
                engine.finish_request(research_id, "completed", envelope.get("usage"))
                # Successful local collectors are encountered sources too, independent of model search.
                envelope["citations"] = sorted(set(envelope["citations"]) | {
                    source["source_url"] for source in snapshot["sources"].values() if source["status"] == "ok"})
                (args.state_dir / f"research-{research_id}.json").write_text(json.dumps(envelope, indent=2, ensure_ascii=False) + "\n")
            report = engine.apply(envelope, args.output_dir or args.state_dir / "preview",
                                  snapshot_at=snapshot["collected_at"] if snapshot else None)
            print(json.dumps(report, indent=2))
            return 0
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        print(f"Engine refused: {error}", file=sys.stderr)
        return 1
    finally:
        if engine:
            engine.close()


if __name__ == "__main__":
    sys.exit(main())
