"""The radar shares the engine's SQLite ledger; each worker owns its connection."""

import datetime as dt
import json
import sqlite3
from pathlib import Path

from news_engine import UTC, digest, encoded, timestamp
from signals import detect


class RadarStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.state_dir / "newsroom.sqlite", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS radar_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS radar_tokens(address TEXT PRIMARY KEY, last_seen TEXT NOT NULL, origin TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS radar_pools(key TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS radar_observations(id INTEGER PRIMARY KEY, subject TEXT, fetched_at TEXT, body TEXT);
            CREATE INDEX IF NOT EXISTS radar_observations_time ON radar_observations(fetched_at);
            CREATE TABLE IF NOT EXISTS radar_candidates(
                id TEXT PRIMARY KEY, event_key TEXT UNIQUE, subject TEXT, token TEXT,
                kind TEXT, score INTEGER, status TEXT, revision INTEGER, fingerprint TEXT,
                first_seen TEXT, last_seen TEXT, expires_at TEXT, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS radar_candidates_current ON radar_candidates(status, expires_at, score);
            CREATE TABLE IF NOT EXISTS radar_sources(
                id TEXT PRIMARY KEY, status TEXT, last_attempt TEXT, last_success TEXT,
                failures INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS radar_jobs(
                id TEXT PRIMARY KEY, lane TEXT, candidate_id TEXT, revision INTEGER,
                state TEXT, created_at TEXT, started_at TEXT, finished_at TEXT, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS radar_jobs_queue ON radar_jobs(state, lane, created_at);
            CREATE TABLE IF NOT EXISTS radar_stream_events(
                id TEXT PRIMARY KEY, source TEXT, received_at TEXT, body TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO radar_meta VALUES (?,?)", (key, encoded(value)))

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM radar_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def health(self, source, status, now, **details):
        old = self.db.execute("SELECT * FROM radar_sources WHERE id=?", (source,)).fetchone()
        failures = ((old["failures"] if old else 0) + 1) if status == "error" else 0
        success = timestamp(now) if status == "ok" else (old["last_success"] if old else None)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO radar_sources VALUES (?,?,?,?,?,?)",
                            (source, status, timestamp(now), success, failures, encoded(details)))
        return failures

    def add_tokens(self, tokens, now, origin):
        with self.db:
            for token in tokens:
                self.db.execute("INSERT OR REPLACE INTO radar_tokens VALUES (?,?,?)", (token, timestamp(now), origin))

    def watched_tokens(self, config, now):
        cutoff = timestamp(now - dt.timedelta(seconds=config["watch_ttl_seconds"]))
        discovered = [r[0] for r in self.db.execute(
            "SELECT address FROM radar_tokens WHERE last_seen>? ORDER BY CASE origin WHEN 'geckoterminal_trending' THEN 0 ELSE 1 END,last_seen DESC,address", (cutoff,))]
        return list(dict.fromkeys(config["watch_tokens"] + discovered))[:config["max_watched_tokens"]]

    def candidate(self, candidate_id):
        row = self.db.execute("SELECT * FROM radar_candidates WHERE id=?", (candidate_id,)).fetchone()
        return self.decode_candidate(row) if row else None

    @staticmethod
    def decode_candidate(row):
        return {**json.loads(row["body"]), **{key: row[key] for key in (
            "id", "subject", "token", "kind", "score", "status", "revision", "first_seen", "last_seen", "expires_at")}}

    def upsert_signal(self, signal, config, now):
        key = signal.get("event_key") or f"{signal['subject']}:{signal['kind']}"
        if not signal.get("event_key") and signal["basis"].get("direction"):
            key += ":" + signal["basis"]["direction"]
        candidate_id = "candidate-" + digest(key)[:20]
        fingerprint = digest(signal["basis"])
        old = self.db.execute("SELECT * FROM radar_candidates WHERE id=?", (candidate_id,)).fetchone()
        stamp = timestamp(now)
        revision, status, first, expiry = 1, "new", stamp, timestamp(now + dt.timedelta(seconds=config["candidate_ttl_seconds"]))
        if old:
            revision, status, first, expiry = old["revision"], old["status"], old["first_seen"], old["expires_at"]
            cooled = now - dt.datetime.fromisoformat(old["first_seen"]) >= dt.timedelta(seconds=config["subject_cooldown_seconds"])
            if old["expires_at"] <= stamp and cooled:
                revision, status, first = revision + 1, "new", stamp
                expiry = timestamp(now + dt.timedelta(seconds=config["candidate_ttl_seconds"]))
            elif old["fingerprint"] != fingerprint and old["expires_at"] > stamp:
                # A moving metric can update evidence without creating another model job.
                revision += 1
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO radar_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (candidate_id, key, signal["subject"], signal.get("token", ""), signal["kind"],
                             signal["score"], status, revision, fingerprint, first, stamp, expiry, encoded(signal)))
        return candidate_id

    def ingest_pool(self, pool, config, now):
        old = self.db.execute("SELECT body FROM radar_pools WHERE key=?", (pool["key"],)).fetchone()
        previous = json.loads(old[0]) if old else None
        signals = detect(pool, previous, config, now)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO radar_pools VALUES (?,?)", (pool["key"], encoded(pool)))
            self.db.execute("INSERT INTO radar_observations(subject,fetched_at,body) VALUES (?,?,?)",
                            (pool["key"], pool["fetched_at"], encoded(pool)))
        return [self.upsert_signal(signal, config, now) for signal in signals]

    def stream_event(self, event_id, source, body, now):
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO radar_stream_events VALUES (?,?,?,?)",
                                     (event_id, source, timestamp(now), encoded(body)))
        return cursor.rowcount == 1

    def stream_count(self, source, now):
        day = now.astimezone(UTC).date().isoformat()
        return self.db.execute("SELECT count(*) FROM radar_stream_events WHERE source=? AND received_at>=?", (source, day)).fetchone()[0]

    def fresh(self, candidate, config, now):
        cutoff = now - dt.timedelta(seconds=config["source_stale_seconds"])
        if candidate["expires_at"] <= timestamp(now):
            return False
        if candidate["evidence_type"] == "provider_observation":
            row = self.db.execute("SELECT body FROM radar_pools WHERE key=?", (candidate["subject"],)).fetchone()
            return bool(row and dt.datetime.fromisoformat(json.loads(row[0])["fetched_at"]) > cutoff
                        and dt.datetime.fromisoformat(candidate["fetched_at"]) > cutoff)
        return dt.datetime.fromisoformat(candidate["last_seen"]) > now - dt.timedelta(seconds=config["candidate_ttl_seconds"])

    def queue_candidate(self, candidate_id, config, now, *, automatic=False):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            candidate = self.candidate(candidate_id)
            if not candidate or not self.fresh(candidate, config, now):
                raise ValueError("Candidate has expired or its market source is stale.")
            if candidate["status"] not in ("new", "no_story"):
                raise ValueError("Candidate already handled; failed or uncertain jobs are never retried automatically.")
            since = timestamp(now - dt.timedelta(seconds=config["subject_cooldown_seconds"]))
            busy = self.db.execute("""SELECT 1 FROM radar_jobs j JOIN radar_candidates c ON c.id=j.candidate_id
                WHERE (c.subject=? OR (?<>'' AND c.token=?)) AND coalesce(j.started_at,j.created_at)>?
                AND (j.state IN ('queued','researching') OR j.started_at IS NOT NULL) AND j.state<>'cancelled' LIMIT 1""",
                (candidate["subject"], candidate["token"], candidate["token"], since)).fetchone()
            if busy:
                raise ValueError("This token or subject is on its research cooldown.")
            job_id = f"triggered-{candidate_id}-{candidate['revision']}"
            body = {"candidate": candidate, "automatic": automatic}
            self.db.execute("INSERT INTO radar_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                            (job_id, "triggered", candidate_id, candidate["revision"], "queued", timestamp(now), None, None, encoded(body)))
            self.db.execute("UPDATE radar_candidates SET status='queued' WHERE id=?", (candidate_id,))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return job_id

    def dismiss(self, candidate_id):
        with self.db:
            row = self.db.execute("SELECT status FROM radar_candidates WHERE id=?", (candidate_id,)).fetchone()
            if not row or row[0] == "researching":
                raise ValueError("Candidate is missing or its investigation has already started.")
            self.db.execute("UPDATE radar_candidates SET status='dismissed' WHERE id=?", (candidate_id,))
            self.db.execute("UPDATE radar_jobs SET state='cancelled' WHERE candidate_id=? AND state='queued'", (candidate_id,))

    def enqueue_background(self, config, now):
        slot = int((now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()) // config["background_interval_seconds"]
        job_id = f"background-{now.date().isoformat()}-{slot:02d}"
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO radar_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                            (job_id, "background", None, None, "queued", timestamp(now), None, None, "{}"))

    def recover_jobs(self, now):
        # A crash after reservation can have billed the provider. Preserve it as uncertain.
        with self.db:
            rows = self.db.execute("SELECT id,candidate_id,body FROM radar_jobs WHERE state='researching'").fetchall()
            for row in rows:
                self.db.execute("UPDATE radar_jobs SET state='uncertain',finished_at=?,body=? WHERE id=?",
                                (timestamp(now), encoded({**json.loads(row["body"]), "error": "Worker restarted during a request; no automatic retry."}), row["id"]))
                self.db.execute("UPDATE radar_candidates SET status='uncertain' WHERE id=?", (row["candidate_id"],))

    def next_job(self, config, now):
        rows = self.db.execute("""SELECT j.* FROM radar_jobs j LEFT JOIN radar_candidates c ON c.id=j.candidate_id
            WHERE j.state='queued' ORDER BY CASE j.lane WHEN 'triggered' THEN 0 ELSE 1 END,coalesce(c.score,0) DESC,j.created_at""").fetchall()
        last = self.db.execute("SELECT max(started_at) FROM radar_jobs WHERE lane='triggered'").fetchone()[0]
        for row in rows:
            if row["lane"] == "triggered":
                candidate = self.candidate(row["candidate_id"])
                if not candidate or candidate["status"] == "dismissed" or not self.fresh(candidate, config, now):
                    self.finish_job(row["id"], "expired", now, {"error": "Evidence expired before investigation."})
                    continue
                if candidate["score"] < config["urgent_research_score"] and last and dt.datetime.fromisoformat(last) + dt.timedelta(seconds=config["triggered_min_spacing_seconds"]) > now:
                    continue
            else:
                created = dt.datetime.fromisoformat(row["created_at"])
                if created.date() != now.date() or (now - created).total_seconds() >= config["background_interval_seconds"]:
                    self.finish_job(row["id"], "expired", now, {"error": "Missed background slots are not replayed."})
                    continue
            return dict(row)
        return None

    def start_job(self, job, now):
        with self.db:
            claimed = self.db.execute("UPDATE radar_jobs SET state='researching',started_at=? WHERE id=? AND state='queued'", (timestamp(now), job["id"]))
            if not claimed.rowcount:
                return False
            if job["candidate_id"]:
                self.db.execute("UPDATE radar_candidates SET status='researching' WHERE id=?", (job["candidate_id"],))
        return True

    def finish_job(self, job_id, state, now, result):
        row = self.db.execute("SELECT body,candidate_id FROM radar_jobs WHERE id=?", (job_id,)).fetchone()
        body = {**(json.loads(row["body"]) if row else {}), **result}
        with self.db:
            self.db.execute("UPDATE radar_jobs SET state=?,finished_at=?,body=? WHERE id=?", (state, timestamp(now), encoded(body), job_id))
            if row and row["candidate_id"]:
                self.db.execute("UPDATE radar_candidates SET status=? WHERE id=? AND status<>'dismissed'", (state, row["candidate_id"]))

    def snapshot(self, config, now, focus=None):
        cutoff = timestamp(now - dt.timedelta(seconds=config["source_stale_seconds"]))
        pools = [json.loads(r[0]) for r in self.db.execute("SELECT body FROM radar_pools WHERE json_extract(body,'$.fetched_at')>?", (cutoff,))]
        if focus:
            pools = [p for p in pools if p["key"] == focus["subject"]]
        groups = {}
        for pool in sorted(pools, key=lambda p: p["volume_m5"], reverse=True)[:16]:
            key = digest(pool["source_url"])[:12]
            group = groups.setdefault(key, {"source_url": pool["source_url"], "status": "ok", "observed_at": pool["fetched_at"], "data": {"pools": []}})
            group["data"]["pools"].append(pool)
            group["observed_at"] = min(group["observed_at"], pool["fetched_at"])
        return {"collected_at": timestamp(now), "sources": groups}

    def auto_candidate(self, config, now):
        if self.db.execute("SELECT 1 FROM radar_jobs WHERE state='researching'").fetchone():
            return None
        pending = self.db.execute("""SELECT max(c.score) FROM radar_jobs j JOIN radar_candidates c ON c.id=j.candidate_id
            WHERE j.lane='triggered' AND j.state='queued'""").fetchone()[0]
        rows = self.db.execute("SELECT * FROM radar_candidates WHERE status='new' AND score>=? AND expires_at>? ORDER BY score DESC,first_seen", (config["auto_research_score"], timestamp(now)))
        for row in rows:
            candidate = self.decode_candidate(row)
            if pending is not None and (candidate["score"] < config["urgent_research_score"] or candidate["score"] <= pending):
                continue
            if self.fresh(candidate, config, now):
                try:
                    return self.queue_candidate(candidate["id"], config, now, automatic=True)
                except ValueError:
                    continue
        return None

    def prune(self, now):
        day_ago = timestamp(now - dt.timedelta(days=1))
        observations_cutoff = timestamp(now - dt.timedelta(hours=6))
        week_ago = timestamp(now - dt.timedelta(days=7))
        with self.db:
            self.db.execute("DELETE FROM radar_observations WHERE fetched_at<?", (observations_cutoff,))
            self.db.execute("DELETE FROM radar_stream_events WHERE received_at<?", (week_ago,))
            self.db.execute("DELETE FROM radar_tokens WHERE last_seen<?", (day_ago,))
            self.db.execute("DELETE FROM radar_pools WHERE json_extract(body,'$.fetched_at')<?", (day_ago,))
            self.db.execute("DELETE FROM radar_candidates WHERE expires_at<? AND id NOT IN (SELECT candidate_id FROM radar_jobs WHERE candidate_id IS NOT NULL)", (week_ago,))

    def state(self, config, engine_config, now):
        candidates = [self.decode_candidate(r) for r in self.db.execute("SELECT * FROM radar_candidates ORDER BY CASE WHEN expires_at>? AND status<>'dismissed' THEN 0 ELSE 1 END,score DESC,first_seen DESC LIMIT 150", (timestamp(now),))]
        for candidate in candidates:
            candidate["fresh"] = self.fresh(candidate, config, now)
        health = [{**dict(r), "details": json.loads(r["body"])} for r in self.db.execute("SELECT * FROM radar_sources ORDER BY id")]
        for source in health:
            del source["body"]
            interval = config["discovery_seconds"] * 2 if source["id"] in ("dex_discovery", "geckoterminal") else config["source_stale_seconds"]
            source["fresh"] = bool(source["last_success"] and dt.datetime.fromisoformat(source["last_success"]) > now - dt.timedelta(seconds=interval))
        jobs = [{**dict(r), "details": json.loads(r["body"])} for r in self.db.execute("SELECT * FROM radar_jobs ORDER BY created_at DESC LIMIT 50")]
        for job in jobs:
            del job["body"]
        budget = {"total": 0, "background": 0, "triggered": 0, "manual": 0,
                  "limit": engine_config["daily_research_calls"], "background_limit": config["background_calls_per_day"],
                  "triggered_limit": config["triggered_calls_per_day"], "day": now.date().isoformat()}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='requests'").fetchone():
            for row in self.db.execute("SELECT usage FROM requests WHERE day=?", (now.date().isoformat(),)):
                lane = json.loads(row[0]).get("lane", "manual")
                budget["total"] += 1
                if lane in ("background", "triggered", "manual"):
                    budget[lane] += 1
        return {"now": timestamp(now), "off_air": True, "collector": self.meta("collector", {}),
                "research": self.meta("research", {"enabled": False}), "candidates": candidates,
                "sources": health, "jobs": jobs, "budget": budget,
                "subject_cooldown_seconds": config["subject_cooldown_seconds"],
                "watched_tokens": len(self.watched_tokens(config, now)), "poll_seconds": config["poll_seconds"]}
