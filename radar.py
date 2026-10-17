#!/usr/bin/env python3
"""Run the off-air Solana radar, durable investigations and local newsroom."""

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import signal
import sqlite3
import sys
import threading
from pathlib import Path

from news_engine import BASE, Engine, UTC, canonical_url, load_config, run_lock, timestamp, validate_snapshot
from radar_sources import DEX, MarketCollector, load_radar_config, request_json, run_solana_stream, run_x_stream
from radar_store import RadarStore
from research import ResearchError, run_research
from signals import normalize_dex


@contextlib.contextmanager
def radar_lock(state_dir):
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "radar.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A radar collector already owns this state directory.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def relevant_stories(stories, focus):
    if not isinstance(stories, list) or any(not isinstance(story, dict) for story in stories):
        raise ValueError("Malformed research stories.")
    if not focus:
        return stories
    target_url = canonical_url(focus["source_url"])
    kept = []
    for story in stories:
        facts = " ".join(str(claim.get("fact", "")) for claim in story.get("claims", []) if isinstance(claim, dict))
        urls = {canonical_url(url) for claim in story.get("claims", []) if isinstance(claim, dict) for url in claim.get("source_urls", [])}
        if (focus.get("token") and focus["token"] in facts) or target_url in urls or canonical_url(story.get("event_url", "")) == target_url:
            kept.append(story)
    return kept


def execute_job(store, engine_config, config, job, output_dir, research_fn=run_research, stop=None, verify_fetch=request_json):
    now = dt.datetime.now(UTC)
    current = store.db.execute("SELECT state FROM radar_jobs WHERE id=?", (job["id"],)).fetchone()
    if not current or current[0] != "queued":
        return True
    focus = store.candidate(job["candidate_id"]) if job["candidate_id"] else None
    snapshot = store.snapshot(config, now, focus=focus)
    if not snapshot["sources"] and (not focus or focus["evidence_type"] == "provider_observation"):
        # Leave this job queued until fresh observations arrive, without reserving a paid call.
        return False
    if focus and focus["evidence_type"] == "provider_observation":
        # Re-fetch an individual pool: batched token URLs must not merge unrelated events in the story ledger.
        backoff = store.meta("verification_backoff", {})
        if backoff.get("job_id") == job["id"] and backoff["retry_at"] > timestamp(now):
            return False
        url = DEX + "/latest/dex/pairs/solana/" + focus["pool"]["pair"]
        try:
            response = verify_fetch(url)
            pool = next((normalize_dex(raw, url, timestamp(dt.datetime.now(UTC))) for raw in response["pairs"]
                         if raw.get("pairAddress") == focus["pool"]["pair"]), None)
            if not pool or pool["token"] != focus["token"]:
                raise ValueError("Pool verification returned a different token.")
            focus = {**focus, "source_url": url, "event_key": pool["event_key"], "pool": pool, "fetched_at": pool["fetched_at"]}
            snapshot = {"collected_at": pool["fetched_at"], "sources": {"verified_pool": {
                "source_url": url, "observed_at": pool["fetched_at"], "status": "ok", "data": pool}}}
            store.health("pool_verification", "ok", dt.datetime.now(UTC), note="Individual pool fetched before research; upstream snapshot age remains unknown.")
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            store.set_meta("verification_backoff", {"job_id": job["id"], "retry_at": timestamp(now + dt.timedelta(seconds=config["poll_seconds"]))})
            store.health("pool_verification", "error", now, note="Waiting for a current individual-pool fetch. No model call reserved.")
            return False
    engine, reserved, usage = None, False, {}
    try:
        with run_lock(store.state_dir):
            # Fast-changing market copy has a shorter lifetime than background announcements.
            effective_config = {**engine_config, "market_observation_ttl_minutes": min(
                5, engine_config.get("market_observation_ttl_minutes", engine_config["edition_ttl_minutes"]))}
            engine = Engine(store.state_dir, effective_config)
            validate_snapshot(snapshot, engine_config, now)
            limit = config[job["lane"] + "_calls_per_day"]
            engine.reserve_request(job["id"], now, lane=job["lane"], lane_limit=limit)
            reserved = True
            if not store.start_job(job, now):
                engine.finish_request(job["id"], "cancelled_before_submission")
                return True
            store.set_meta("research", {"enabled": True, "state": "researching", "job_id": job["id"], "started_at": timestamp(now)})
            captures = store.state_dir / "research"
            captures.mkdir(exist_ok=True, mode=0o700)
            (captures / f"{job['id']}.input.json").write_text(json.dumps({
                "schema_version": 1, "research_id": job["id"], "request_started_at": timestamp(now),
                "market_snapshot": snapshot, "investigation_target": focus,
            }, indent=2, ensure_ascii=False) + "\n")
            envelope = research_fn(effective_config, snapshot, engine.recent(), job["id"], now,
                                   capture_path=captures / f"{job['id']}.response.json", focus=focus, cancel_event=stop)
            usage = envelope.get("usage", {})
            finished = dt.datetime.now(UTC)
            engine.finish_request(job["id"], "completed", usage)
            if focus and not store.fresh(store.candidate(focus["id"]), config, finished):
                store.finish_job(job["id"], "expired", finished, {"error": "Investigation finished after its evidence became stale; copy withheld."})
                return True
            envelope["stories"] = relevant_stories(envelope["stories"], focus)
            envelope["citations"] = sorted(set(envelope["citations"]) | {s["source_url"] for s in snapshot["sources"].values() if s["status"] == "ok"})
            snapshots_at = [s["observed_at"] for s in snapshot["sources"].values()]
            snapshot_at = min(snapshots_at) if snapshots_at else envelope["researched_at"]
            envelope["data_snapshot_at"] = snapshot_at
            (captures / f"{job['id']}.json").write_text(json.dumps(envelope, indent=2, ensure_ascii=False) + "\n")
            report = engine.apply(envelope, output_dir, now=finished, snapshot_at=snapshot_at)
            story_ids = list(dict.fromkeys(report["new"] + report["updated"] + report["unchanged"]))
            state = "drafted" if story_ids else "no_story"
            store.finish_job(job["id"], state, finished,
                             {"research_id": envelope["research_id"], "story_ids": story_ids, "report": report,
                              "attribution": "source_attributed; requires editorial review before airing"})
            if focus:
                store.set_meta("last_latency", {"candidate_id": focus["id"], "research_id": envelope["research_id"],
                    "request_seconds": round((finished - now).total_seconds(), 3),
                    "detection_to_result_seconds": round((finished - dt.datetime.fromisoformat(focus["first_seen"])).total_seconds(), 3),
                    "source_event_to_detection_seconds": None, "note": "Upstream snapshot update time is not supplied."})
            return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, sqlite3.Error) as error:
        if reserved and engine:
            engine.finish_request(job["id"], "failed", usage)
        # Do not copy provider exception text, configured endpoint URLs or subprocess diagnostics into the UI.
        state = "blocked" if isinstance(error, ResearchError) and not reserved else "failed"
        store.finish_job(job["id"], state, dt.datetime.now(UTC),
                         {"error": "Research quota unavailable." if state == "blocked" else "Investigation failed or output validation rejected it. No automatic retry.",
                          "error_type": type(error).__name__})
        return True
    finally:
        if engine:
            engine.close()
        store.set_meta("research", {"enabled": True, "state": "idle", "last_job_id": job["id"], "updated_at": timestamp(dt.datetime.now(UTC))})


def research_worker(state_dir, engine_config, config, output_dir, stop):
    store = RadarStore(state_dir)
    try:
        while not stop.is_set():
            try:
                now = dt.datetime.now(UTC)
                store.enqueue_background(config, now)
                store.auto_candidate(config, now)
                job = store.next_job(config, now)
                if job:
                    execute_job(store, engine_config, config, job, output_dir, stop=stop)
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                store.set_meta("research", {"enabled": True, "state": "error", "updated_at": timestamp(dt.datetime.now(UTC)),
                                            "error": "Research worker encountered a local error; collection continues."})
            stop.wait(2)
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "once", "status"), nargs="?", default="run")
    parser.add_argument("--state-dir", type=Path, default=BASE / ".state")
    parser.add_argument("--output-dir", type=Path, help="Off-air bulletin files; defaults to STATE/radar-output")
    parser.add_argument("--engine-config", type=Path, default=BASE / "engine-config.json")
    parser.add_argument("--config", type=Path, default=BASE / "radar-config.json")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--no-research", action="store_true", help="Collect and review without any model requests")
    parser.add_argument("--no-ui", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    store, server, worker = None, None, None
    stop = threading.Event()
    try:
        engine_config = load_config(args.engine_config)
        config = load_radar_config(args.config, engine_config)
        if not 1024 <= args.port <= 65535:
            raise ValueError("Choose a local port between 1024 and 65535.")
        store = RadarStore(args.state_dir)
        if args.command == "status":
            print(json.dumps(store.state(config, engine_config, dt.datetime.now(UTC)), indent=2))
            return 0
        with radar_lock(args.state_dir):
            collector = MarketCollector(store, config)
            if args.command == "once":
                collector.tick(dt.datetime.now(UTC))
                print(json.dumps(store.state(config, engine_config, dt.datetime.now(UTC)), indent=2))
                return 0
            output_dir = args.output_dir or args.state_dir / "radar-output"
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: stop.set())
            store.recover_jobs(dt.datetime.now(UTC))
            store.set_meta("research", {"enabled": not args.no_research, "state": "idle" if not args.no_research else "paused"})
            if not args.no_ui:
                from newsroom import make_server
                server = make_server(args.state_dir, engine_config, config, args.port)
                threading.Thread(target=server.serve_forever, daemon=True, name="newsroom-http").start()
                print(f"Newsroom: http://127.0.0.1:{args.port}", flush=True)
            for target in (run_x_stream, run_solana_stream):
                threading.Thread(target=target, args=(args.state_dir, config, stop), daemon=True).start()
            if not args.no_research:
                worker = threading.Thread(target=research_worker, args=(args.state_dir, engine_config, config, output_dir, stop), daemon=True, name="research-worker")
                worker.start()
            last_prune = 0
            while not stop.is_set():
                now = dt.datetime.now(UTC)
                store.set_meta("collector", {"status": "collecting", "heartbeat": timestamp(now), "last_scan_at": collector.last_poll_at, "pid": os.getpid()})
                try:
                    collector.tick(now)
                    store.set_meta("collector", {"status": "collecting", "heartbeat": timestamp(dt.datetime.now(UTC)), "last_scan_at": collector.last_poll_at, "pid": os.getpid()})
                except (OSError, ValueError, TypeError, sqlite3.Error):
                    store.set_meta("collector", {"status": "error", "heartbeat": timestamp(dt.datetime.now(UTC)), "pid": os.getpid()})
                if last_prune % 120 == 0:
                    store.prune(dt.datetime.now(UTC))
                last_prune += 1
                stop.wait(1)
            store.set_meta("collector", {"status": "stopped", "heartbeat": timestamp(dt.datetime.now(UTC))})
            return 0
    except (OSError, ValueError) as error:
        print(f"Radar stopped: {error}", file=sys.stderr)
        return 1
    finally:
        stop.set()
        if worker:
            worker.join(timeout=5)
        if server:
            server.shutdown()
            server.server_close()
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
