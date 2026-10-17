import datetime as dt
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from news_engine import BASE, Engine, UTC, load_config, timestamp
from newsroom import make_server
from radar import execute_job, radar_lock, relevant_stories
from radar_sources import MarketCollector, load_radar_config, solana_signal, x_signal
from radar_store import RadarStore
from research import ResearchError, research_request, run_research
from signals import detect, normalize_dex, number

TOKEN = "A" * 32
PAIR = "B" * 32
NOW = dt.datetime(2026, 10, 17, 14, 30, tzinfo=UTC)
URL = "https://api.dexscreener.com/tokens/v1/solana/" + TOKEN


def raw_pair(**changes):
    raw = {"chainId": "solana", "pairAddress": PAIR, "baseToken": {"address": TOKEN, "symbol": "TEST", "name": "Test fixture"},
           "dexId": "pumpswap", "liquidity": {"usd": 100000}, "volume": {"m5": 40000, "h1": 150000},
           "priceChange": {"m5": 20}, "priceUsd": "0.01", "pairCreatedAt": int((NOW - dt.timedelta(minutes=25)).timestamp() * 1000)}
    raw.update(changes)
    return raw


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.engine_config = load_config()
        self.config = load_radar_config(BASE / "radar-config.json", self.engine_config)
        self.store = RadarStore(self.state)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def pool(self, raw=None, now=NOW):
        return normalize_dex(raw or raw_pair(), URL, timestamp(now))

    def candidate(self, now=NOW):
        ids = self.store.ingest_pool(self.pool(now=now), self.config, now)
        return next(self.store.candidate(cid) for cid in ids if self.store.candidate(cid)["kind"] == "volume_acceleration")

    def test_only_solana_valid_finite_observations_enter_detection(self):
        for bad in ({"chainId": "base"}, {"pairAddress": "../bad"}, {"liquidity": {"usd": "NaN"}}, {"volume": {"m5": True}}):
            self.assertIsNone(normalize_dex(raw_pair(**bad), URL, timestamp(NOW)))
        for value in (True, "Infinity", "NaN", -1, None):
            self.assertIsNone(number(value))

    def test_acceleration_excludes_latest_five_minutes_from_baseline(self):
        signals = detect(self.pool(), None, self.config, NOW)
        volume = next(s for s in signals if s["kind"] == "volume_acceleration")
        self.assertEqual(volume["metrics"]["Previous 5m pace"], 10000)
        self.assertEqual(volume["metrics"]["Acceleration"], 4)

    def test_no_acceleration_claim_without_comparable_hourly_window(self):
        pool = self.pool(raw_pair(volume={"m5": 40000, "h1": 35000}))
        self.assertNotIn("volume_acceleration", [s["kind"] for s in detect(pool, None, self.config, NOW)])

    def test_paid_boost_is_not_a_signal_and_thin_pools_are_excluded(self):
        pool = self.pool(raw_pair(boosts={"active": 500}, liquidity={"usd": 1000}))
        self.assertEqual(detect(pool, None, self.config, NOW), [])

    def test_liquidity_alert_survives_falling_below_discovery_floor(self):
        previous = self.pool()
        pool = self.pool(raw_pair(liquidity={"usd": 10000}), NOW + dt.timedelta(seconds=30))
        signal = next(s for s in detect(pool, previous, self.config, NOW + dt.timedelta(seconds=30)) if s["kind"] == "liquidity_change")
        self.assertEqual(signal["metrics"]["Change (%)"], -90)
        self.assertIn("has not been established", signal["reason"])

    def test_stale_or_different_provider_liquidity_values_are_not_compared(self):
        previous = self.pool()
        for stale, different in ((True, False), (False, True)):
            pool = self.pool(raw_pair(liquidity={"usd": 10000}), NOW + dt.timedelta(seconds=300 if stale else 30))
            if different:
                previous["source"] = "other"
            self.assertNotIn("liquidity_change", [s["kind"] for s in detect(pool, previous, self.config, dt.datetime.fromisoformat(pool["fetched_at"]))])

    def test_repeat_signal_keeps_identity_and_original_expiry_across_restart(self):
        first = self.candidate()
        later = NOW + dt.timedelta(seconds=30)
        self.candidate(later)
        self.store.close()
        self.store = RadarStore(self.state)
        current = self.store.candidate(first["id"])
        self.assertEqual(current["expires_at"], first["expires_at"])
        self.assertEqual(current["first_seen"], first["first_seen"])
        self.assertEqual(current["last_seen"], timestamp(later))

    def test_candidate_rearms_only_after_expiry_and_cooldown(self):
        first = self.candidate()
        current = self.candidate(NOW + dt.timedelta(minutes=11))
        self.assertEqual(current["expires_at"], first["expires_at"])
        current = self.candidate(NOW + dt.timedelta(minutes=31))
        self.assertGreater(current["revision"], first["revision"])
        self.assertGreater(current["expires_at"], first["expires_at"])

    def test_queue_is_deduplicated_per_token_even_across_different_signals(self):
        first = self.candidate()
        self.store.queue_candidate(first["id"], self.config, NOW)
        other = self.store.db.execute("SELECT id FROM radar_candidates WHERE kind='price_move'").fetchone()[0]
        with self.assertRaises(ValueError):
            self.store.queue_candidate(other, self.config, NOW)
        with self.assertRaises(ValueError):
            self.store.queue_candidate(first["id"], self.config, NOW)

    def test_price_reversal_is_a_new_candidate_but_shares_the_token_research_cooldown(self):
        ids = self.store.ingest_pool(self.pool(raw_pair(priceChange={"m5":-20})), self.config, NOW)
        first = next(self.store.candidate(cid) for cid in ids if self.store.candidate(cid)['kind']=='price_move')
        self.store.queue_candidate(first['id'],self.config,NOW)
        later = NOW + dt.timedelta(seconds=30)
        ids = self.store.ingest_pool(self.pool(raw_pair(priceChange={"m5":20}),later),self.config,later)
        second = next(self.store.candidate(cid) for cid in ids if self.store.candidate(cid)['kind']=='price_move')
        self.assertNotEqual(first['id'],second['id'])
        self.assertTrue(self.store.fresh(second,self.config,later))
        with self.assertRaises(ValueError):
            self.store.queue_candidate(second['id'],self.config,later)

    def test_job_expiring_before_submission_does_not_consume_token_research_cooldown(self):
        first = self.candidate()
        job_id = self.store.queue_candidate(first['id'],self.config,NOW)
        self.store.finish_job(job_id,'expired',NOW,{'error':'Expired before submission'})
        other = self.store.db.execute("SELECT id FROM radar_candidates WHERE kind='price_move'").fetchone()[0]
        self.assertTrue(self.store.queue_candidate(other,self.config,NOW))

    def test_stale_pool_cannot_be_queued_even_if_candidate_has_not_expired(self):
        candidate = self.candidate()
        with self.assertRaises(ValueError):
            self.store.queue_candidate(candidate["id"], self.config, NOW + dt.timedelta(seconds=121))

    def test_new_pool_fetch_cannot_refresh_an_old_signal_that_no_longer_fires(self):
        candidate = self.candidate()
        now = NOW + dt.timedelta(seconds=121)
        quiet = self.pool(raw_pair(volume={"m5": 1, "h1": 100000}, priceChange={"m5": 0}), now)
        self.store.ingest_pool(quiet, self.config, now)
        self.assertFalse(self.store.fresh(self.store.candidate(candidate["id"]), self.config, now))

    def test_dismiss_cancels_queued_work(self):
        candidate = self.candidate()
        job = self.store.queue_candidate(candidate["id"], self.config, NOW)
        self.store.dismiss(candidate["id"])
        self.assertIsNone(self.store.next_job(self.config, NOW))
        self.assertEqual(self.store.db.execute("SELECT state FROM radar_jobs WHERE id=?", (job,)).fetchone()[0], "cancelled")

    def test_restart_never_retries_a_potentially_billed_job(self):
        candidate = self.candidate()
        self.store.queue_candidate(candidate["id"], self.config, NOW)
        job = self.store.next_job(self.config, NOW)
        self.store.start_job(job, NOW)
        self.store.recover_jobs(NOW + dt.timedelta(seconds=20))
        self.assertIsNone(self.store.next_job(self.config, NOW + dt.timedelta(seconds=20)))
        row = self.store.db.execute("SELECT state,body FROM radar_jobs").fetchone()
        self.assertEqual(row["state"], "uncertain")
        self.assertIn("candidate", json.loads(row["body"]))

    def test_background_slot_is_persistent_and_missed_slots_are_not_replayed(self):
        self.store.enqueue_background(self.config, NOW)
        self.store.enqueue_background(self.config, NOW + dt.timedelta(minutes=10))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM radar_jobs").fetchone()[0], 1)
        self.assertIsNone(self.store.next_job(self.config, NOW + dt.timedelta(hours=3)))

    def test_triggered_jobs_take_precedence_and_have_spacing(self):
        self.store.enqueue_background(self.config, NOW)
        candidate = self.candidate()
        self.store.queue_candidate(candidate["id"], self.config, NOW)
        job = self.store.next_job(self.config, NOW)
        self.assertEqual(job["lane"], "triggered")
        self.store.start_job(job, NOW)
        self.store.finish_job(job["id"], "no_story", NOW, {})
        self.assertEqual(self.store.next_job(self.config, NOW)["lane"], "background")

    def test_automatic_selection_waits_for_existing_triggered_work(self):
        self.candidate()
        self.assertIsNotNone(self.store.auto_candidate(self.config, NOW))
        self.assertIsNone(self.store.auto_candidate(self.config, NOW))

    def test_urgent_signal_bypasses_normal_spacing_without_bypassing_token_cooldown(self):
        first=self.candidate()
        self.store.queue_candidate(first['id'],self.config,NOW)
        job=self.store.next_job(self.config,NOW)
        self.store.start_job(job,NOW)
        self.store.finish_job(job['id'],'drafted',NOW,{})
        later=NOW+dt.timedelta(seconds=30)
        raw=raw_pair(pairAddress='D'*32,baseToken={'address':'E'*32,'symbol':'OTHER'},volume={'m5':100000,'h1':110000})
        ids=self.store.ingest_pool(normalize_dex(raw,URL,timestamp(later)),self.config,later)
        urgent=next(self.store.candidate(cid) for cid in ids if self.store.candidate(cid)['kind']=='volume_acceleration')
        self.assertGreaterEqual(urgent['score'],self.config['urgent_research_score'])
        self.store.queue_candidate(urgent['id'],self.config,later)
        self.assertEqual(self.store.next_job(self.config,later)['candidate_id'],urgent['id'])

    def test_urgent_candidate_can_pass_a_waiting_lower_priority_candidate(self):
        self.candidate()
        normal_job=self.store.auto_candidate(self.config,NOW)
        later=NOW+dt.timedelta(seconds=30)
        raw=raw_pair(pairAddress='D'*32,baseToken={'address':'E'*32,'symbol':'OTHER'},volume={'m5':100000,'h1':110000})
        self.store.ingest_pool(normalize_dex(raw,URL,timestamp(later)),self.config,later)
        urgent_job=self.store.auto_candidate(self.config,later)
        self.assertIsNotNone(urgent_job)
        self.assertNotEqual(normal_job,urgent_job)
        self.assertEqual(self.store.next_job(self.config,later)['id'],urgent_job)

    def test_lane_limits_are_atomic_and_do_not_overwrite_usage_metadata(self):
        engine = Engine(self.state, self.engine_config)
        other = Engine(self.state, self.engine_config)
        try:
            engine.reserve_request("first", NOW, lane="triggered", lane_limit=1)
            engine.finish_request("first", "failed", {"total_tokens": 23, "lane": "background"})
            with self.assertRaises(ResearchError):
                other.reserve_request("second", NOW, lane="triggered", lane_limit=1)
            other.reserve_request("third", NOW, lane="background", lane_limit=1)
            state = self.store.state(self.config, self.engine_config, NOW)
            self.assertEqual(state["budget"]["triggered"], 1)
            self.assertEqual(state["budget"]["total"], 2)
            self.assertEqual(json.loads(engine.db.execute("SELECT usage FROM requests WHERE id='first'").fetchone()[0])["total_tokens"], 23)
        finally:
            other.close()
            engine.close()

    def test_radar_and_manual_requests_share_global_cap(self):
        engine = Engine(self.state, {**self.engine_config, "daily_research_calls": 1})
        try:
            engine.reserve_request("manual", NOW)
            with self.assertRaises(ResearchError):
                engine.reserve_request("radar", NOW, lane="triggered", lane_limit=12)
        finally:
            engine.close()

    def test_invalid_or_overallocated_configuration_is_rejected(self):
        for changes in ({"poll_seconds": True}, {"triggered_calls_per_day": 13}, {"watch_tokens": ["bad"]}, {"x_handles": ["../bad"]}, {"volume_acceleration": float("nan")}):
            file = self.state / "config.json"
            file.write_text(json.dumps({**self.config, **changes}))
            with self.assertRaises(ValueError):
                load_radar_config(file, self.engine_config)

    def test_stale_source_health_is_not_presented_as_fresh(self):
        self.store.health("dexscreener", "ok", NOW)
        state = self.store.state(self.config, self.engine_config, NOW + dt.timedelta(seconds=121))
        self.assertFalse(state["sources"][0]["fresh"])

    def test_collectors_use_real_api_shapes_and_keep_only_configured_chain(self):
        def fetch(url):
            if "token-profiles" in url:
                return [{"chainId": "solana", "tokenAddress": TOKEN}, {"chainId": "base", "tokenAddress": "bad"}]
            if "geckoterminal" in url:
                return {"data": []}
            return [raw_pair()]
        collector = MarketCollector(self.store, self.config, fetch, clock=lambda: NOW)
        collector.tick(NOW, monotonic=0)
        self.assertEqual(self.store.watched_tokens(self.config, NOW), [TOKEN])
        self.assertTrue(self.store.state(self.config, self.engine_config, NOW)["candidates"])
        self.assertEqual(self.store.snapshot(self.config, NOW)["sources"][next(iter(self.store.snapshot(self.config, NOW)["sources"]))]["observed_at"], timestamp(NOW))

    def test_failed_collection_never_renews_previous_observation(self):
        self.store.add_tokens([TOKEN], NOW, "fixture")
        collector = MarketCollector(self.store, self.config, lambda _url: [raw_pair()], clock=lambda: NOW)
        collector.poll(NOW)
        collector.fetch = Mock(side_effect=OSError("source failed"))
        collector.poll(NOW + dt.timedelta(minutes=3))
        self.assertEqual(self.store.snapshot(self.config, NOW + dt.timedelta(minutes=3))["sources"], {})

    def test_x_identity_allowlist_edit_history_and_freshness(self):
        message = {"data": {"id": "1234", "author_id": "99", "created_at": timestamp(NOW), "text": "Fee change announced", "edit_history_tweet_ids": ["1233", "1234"]},
                   "includes": {"users": [{"id": "99", "username": "pumpdotfun"}]}}
        signal = x_signal(message, ["pumpdotfun"], NOW)
        self.assertEqual(signal["event_key"], "x:1233")
        self.assertEqual(signal["evidence_type"], "firsthand_post")
        self.assertIsNone(x_signal(message, ["other"], NOW))
        self.assertIsNone(x_signal(message, ["pumpdotfun"], NOW + dt.timedelta(minutes=11)))

    def test_solana_failed_transactions_are_not_candidates_and_mentions_are_not_sales(self):
        message = {"params": {"result": {"context": {"slot": 123}, "value": {"signature": "C" * 88, "err": None, "logs": ["Program log: Instruction: Sell"]}}}}
        signal = solana_signal(message, TOKEN, NOW)
        self.assertEqual(signal["kind"], "watched_transaction")
        self.assertLess(signal["score"], self.config["auto_research_score"])
        self.assertIn("neither wallet ownership nor a sale", signal["reason"])
        message["params"]["result"]["value"]["err"] = {"InstructionError": [0, "Custom"]}
        self.assertIsNone(solana_signal(message, TOKEN, NOW))

    def test_stream_delivery_is_idempotent(self):
        self.assertTrue(self.store.stream_event("same", "x_stream", {"public_post": True}, NOW))
        self.assertFalse(self.store.stream_event("same", "x_stream", {"public_post": True}, NOW))

    def test_exclusive_collector_lock_prevents_duplicate_schedulers(self):
        with radar_lock(self.state):
            with self.assertRaises(ValueError):
                with radar_lock(self.state):
                    pass

    def test_focused_research_has_exact_target_and_rejects_unrelated_results(self):
        focus = self.candidate()
        payload = research_request({**self.engine_config, "model": "test"}, self.store.snapshot(self.config, NOW), [], NOW, focus=focus)
        context = json.loads(payload["input"][1]["content"])
        self.assertEqual(context["investigation_target"]["token"], TOKEN)
        self.assertEqual(context["max_stories"], 2)
        unrelated = {"claims": [{"fact": "General crypto headline", "source_urls": ["https://example.com/news"]}], "event_url": "https://example.com/news"}
        self.assertEqual(relevant_stories([unrelated], focus), [])

    def test_focused_x_research_matches_the_same_post_despite_handle_and_tracking_urls(self):
        focus = {'source_url':'https://x.com/i/status/1234','token':''}
        source = 'https://x.com/pumpdotfun/status/1234?s=20'
        story = {'event_url':source,'claims':[{'fact':'A Solana launchpad posted a fee update.','source_urls':[source]}]}
        self.assertEqual(relevant_stories([story],focus),[story])

    def test_fresh_signal_to_researched_four_format_draft(self):
        now = dt.datetime.now(UTC)
        focus = self.candidate(now)
        self.store.queue_candidate(focus["id"], self.config, now)
        job = self.store.next_job(self.config, now)
        def research(config, snapshot, recent, research_id, started, **kwargs):
            text = "A DEX Screener snapshot shows elevated five-minute volume in a Solana pool. The cause has not been established."
            return {"schema_version": 1, "research_id": research_id, "researched_at": timestamp(dt.datetime.now(UTC)), "provenance": "operator",
                    "citations": [URL], "stories": [{"event_key": "solana:test:volume", "event_url": URL, "segment": "trenches", "priority": 2,
                    "headline": "Solana pool volume increases", "occurred_at": timestamp(now), "status": "reported", "correction_of": "",
                    "claims": [{"fact": f"Solana pool for {TOKEN} has elevated five-minute volume.", "source_urls": [URL]}],
                    "text_medium": text, "text_extra": "", "text_short": text, "text_flash": "A Solana pool shows elevated trading volume, according to DEX Screener."}], "usage": {"total_tokens": 123}}
        self.assertTrue(execute_job(self.store, self.engine_config, self.config, job, self.state / "output", research_fn=research, verify_fetch=lambda _: {"pairs": [raw_pair()]}))
        self.assertEqual(self.store.candidate(focus["id"])["status"], "drafted")
        for fmt in ("12min", "7min", "5min", "2.5min"):
            self.assertTrue((self.state / "output" / f"latest-{fmt}.json").exists())
        edition = json.loads((self.state / "output" / "latest-5min.json").read_text())
        self.assertLessEqual(dt.datetime.fromisoformat(edition["expires_at"]), now + dt.timedelta(minutes=5))
        self.assertEqual(self.store.state(self.config, self.engine_config, now)["budget"]["triggered"], 1)
        audit_input = json.loads((self.state / "research" / f"{job['id']}.input.json").read_text())
        self.assertEqual(audit_input["investigation_target"]["source_url"], 'https://api.dexscreener.com/latest/dex/pairs/solana/' + PAIR)

    def test_failed_provider_request_stays_counted_and_is_never_requeued(self):
        now = dt.datetime.now(UTC)
        focus = self.candidate(now)
        self.store.queue_candidate(focus["id"], self.config, now)
        job = self.store.next_job(self.config, now)
        execute_job(self.store, self.engine_config, self.config, job, self.state / "output", research_fn=Mock(side_effect=ResearchError("timeout")), verify_fetch=lambda _: {"pairs": [raw_pair()]})
        self.assertEqual(self.store.state(self.config, self.engine_config, now)["budget"]["triggered"], 1)
        self.assertIsNone(self.store.next_job(self.config, now))
        self.assertFalse((self.state / "output" / "latest.json").exists())

    def test_stale_background_waits_without_spending(self):
        self.store.enqueue_background(self.config, dt.datetime.now(UTC))
        job = self.store.next_job(self.config, dt.datetime.now(UTC))
        research = Mock()
        self.assertFalse(execute_job(self.store, self.engine_config, self.config, job, self.state / "output", research_fn=research))
        research.assert_not_called()

    def test_failed_pool_crosscheck_is_backed_off_and_does_not_spend(self):
        now = dt.datetime.now(UTC)
        candidate = self.candidate(now)
        self.store.queue_candidate(candidate["id"], self.config, now)
        job = self.store.next_job(self.config, now)
        fetch, research = Mock(side_effect=OSError()), Mock()
        for _ in range(2):
            self.assertFalse(execute_job(self.store, self.engine_config, self.config, job, self.state / "output", research_fn=research, verify_fetch=fetch))
        self.assertEqual(fetch.call_count, 1)
        research.assert_not_called()
        self.assertEqual(self.store.state(self.config, self.engine_config, now)["budget"]["total"], 0)

    def test_dismissal_during_market_crosscheck_prevents_model_submission(self):
        now = dt.datetime.now(UTC)
        candidate = self.candidate(now)
        self.store.queue_candidate(candidate['id'],self.config,now)
        job = self.store.next_job(self.config,now)
        research = Mock()
        def crosscheck(_url):
            self.store.dismiss(candidate['id'])
            return {'pairs':[raw_pair()]}
        execute_job(self.store,self.engine_config,self.config,job,self.state/'output',research_fn=research,verify_fetch=crosscheck)
        research.assert_not_called()
        self.assertEqual(self.store.candidate(candidate['id'])['status'],'dismissed')

    def test_market_event_identity_recognises_only_supported_snapshot_urls(self):
        from news_engine import market_source_url
        self.assertTrue(market_source_url('https://api.dexscreener.com/latest/dex/pairs/solana/' + PAIR))
        self.assertTrue(market_source_url(URL))
        self.assertFalse(market_source_url('https://api.dexscreener.com.attacker.example/latest/dex/pairs/solana/' + PAIR))
        self.assertFalse(market_source_url('https://api.dexscreener.com/news/launch'))
        self.assertFalse(market_source_url(URL.replace('https:', 'http:')))

    def test_two_pools_in_one_batch_have_distinct_story_identities(self):
        from test_news_engine import story
        one, two = story('one'), story('two')
        for item in (one, two):
            item['event_url'] = URL
            item['occurred_at'] = timestamp(NOW)
            for claim in item['claims']:
                claim['source_urls'] = [URL]
        engine = Engine(self.state, self.engine_config)
        try:
            report = engine.apply({'schema_version':1,'research_id':'two-pools','researched_at':timestamp(NOW),'provenance':'operator',
                                   'citations':[URL],'stories':[one,two]}, self.state/'output', now=NOW)
            self.assertEqual(len(set(report['new'])),2)
        finally:
            engine.close()

    def test_invalid_usage_cannot_lose_the_call_reservation_or_block_terminal_state(self):
        engine = Engine(self.state, self.engine_config)
        try:
            engine.reserve_request('invalid-usage', NOW, lane='triggered', lane_limit=1)
            engine.finish_request('invalid-usage', 'failed', {'total_tokens':float('nan')})
            row = engine.db.execute("SELECT status,usage FROM requests WHERE id='invalid-usage'").fetchone()
            self.assertEqual(row['status'],'failed')
            self.assertEqual(json.loads(row['usage'])['lane'],'triggered')
        finally:
            engine.close()

    def test_stopping_research_kills_cli_process_without_submitting_another_job(self):
        cancel = Mock()
        cancel.is_set.return_value = False
        cancel.wait.return_value = True
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch('research.shutil.which', return_value='/installed/grok'), patch('research.subprocess.Popen', return_value=process) as start, patch('research.os.killpg') as kill:
            with self.assertRaises(ResearchError):
                run_research(self.engine_config, {'sources':{}}, [], 'cancelled', NOW, cancel_event=cancel)
            self.assertEqual(start.call_count,1)
            kill.assert_called_once()

    def test_loopback_api_enforces_host_origin_token_and_durable_actions(self):
        now = dt.datetime.now(UTC)
        focus = self.candidate(now)
        server = make_server(self.state, self.engine_config, self.config, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            state = json.load(urllib.request.urlopen(base + "/api/state"))
            self.assertTrue(state["off_air"])
            with self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(urllib.request.Request(base + "/api/state", headers={"Host": "attacker.example"}))
            self.assertEqual(result.exception.code, 403)
            result.exception.close()
            payload = json.dumps({"id": focus["id"], "action": "queue"}).encode()
            with self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(urllib.request.Request(base + "/api/candidate", data=payload, headers={"Content-Type": "application/json"}))
            self.assertEqual(result.exception.code, 403)
            result.exception.close()
            request = urllib.request.Request(base + "/api/candidate", data=payload, headers={"Content-Type": "application/json", "Origin": base, "X-Radar-Token": state["csrf_token"]})
            self.assertEqual(urllib.request.urlopen(request).status, 202)
            self.assertEqual(self.store.candidate(focus["id"])["status"], "queued")
            with self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(base + "/../engine-config.json")
            self.assertEqual(result.exception.code, 404)
            result.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
