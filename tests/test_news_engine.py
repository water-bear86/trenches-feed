import copy
import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_feed
import news_engine as news
import research

NOW = dt.datetime(2026, 10, 17, 14, tzinfo=dt.timezone.utc)


def story(name="aster", segment="trenches"):
    url = f"https://example.org/announcements/{name}"
    return {"event_key": f"test:{name}:routing", "event_url": url, "segment": segment,
            "priority": 1, "headline": f"Synthetic {name} routing announcement",
            "occurred_at": NOW.isoformat(), "status": "reported", "correction_of": "",
            "claims": [{"fact": f"Synthetic crypto protocol {name} announced a routing update.", "source_urls": [url]}],
            "text_medium": f"In this synthetic test, protocol {name} says its routing update is available. The announcement describes a change to how swap requests are routed.",
            "text_extra": "This text is a test fixture and does not report a real product release.",
            "text_short": f"Synthetic protocol {name} says a routing update is available, according to its test announcement.",
            "text_flash": f"Synthetic protocol {name} announces a routing update."}


def envelope(stories=None, research_id="test-one", now=NOW):
    stories = [story()] if stories is None else stories
    return {"schema_version": 1, "research_id": research_id, "researched_at": now.isoformat(),
            "provenance": "operator", "stories": stories,
            "citations": sorted({url for s in stories for c in s["claims"] for url in c["source_urls"]})}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = news.load_config()
        self.engine = news.Engine(self.root / "state", self.config)
        self.output = self.root / "preview"

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def apply(self, data, now=NOW):
        return self.engine.apply(data, self.output, now=now)

    def latest(self):
        return json.loads((self.output / "latest.json").read_text())

    def test_complete_offair_contract(self):
        report = self.apply(envelope())
        self.assertEqual(report["status"], "rendered")
        bundle = self.latest()
        self.assertEqual(set(bundle["formats"]), {"12min", "7min", "5min", "2.5min"})
        for fmt, edition in bundle["formats"].items():
            self.assertEqual(edition["generated_at"], NOW.isoformat())
            self.assertEqual(edition["research_id"], "test-one")
            self.assertLessEqual(edition["word_count"], edition["target_words"])
            self.assertIn("{{TIME_SPOKEN}}", edition["full_script"])
            self.assertEqual(edition["variables"]["BTC_PRICE"]["token"], "{{BTC_PRICE}}")
            self.assertTrue(edition["underfilled"])
            self.assertTrue((self.output / f"latest-{fmt}.json").is_file())

    def test_replay_is_idempotent_and_does_not_touch_output(self):
        data = envelope()
        self.apply(data)
        before = (self.output / "latest.json").read_bytes()
        result = self.apply(data, NOW + dt.timedelta(minutes=2))
        self.assertEqual(result["status"], "already_processed")
        self.assertEqual(before, (self.output / "latest.json").read_bytes())
        self.assertEqual(len(self.engine.stories()), 1)
        data["stories"][0]["headline"] = "Different input under the same request ID"
        with self.assertRaisesRegex(ValueError, "different input"):
            self.apply(data)

    def test_retitled_same_facts_preserve_id_revision_copy_and_expiry(self):
        self.apply(envelope())
        original = self.engine.stories()[0]
        repeated = story()
        repeated.update(headline="Another synthetic headline", text_medium="Rewritten wording for the same synthetic facts.",
                        event_key="a-new-title-for-an-old-development")
        result = self.apply(envelope([repeated], "test-two", NOW + dt.timedelta(minutes=15)), NOW + dt.timedelta(minutes=15))
        current = self.engine.stories()[0]
        self.assertEqual(result["status"], "unchanged")
        for key in ("id", "revision", "text_medium", "expires_at", "event_key"):
            self.assertEqual(current[key], original[key])
        self.assertEqual(self.latest()["research_id"], "test-one")

    def test_same_facts_under_different_urls_do_not_create_a_second_story(self):
        self.apply(envelope())
        repeated = story()
        repeated["event_url"] = "https://second.example.org/report/aster"
        repeated["event_key"] = "retitled:aster:report"
        repeated["claims"][0]["source_urls"] = [repeated["event_url"]]
        self.apply(envelope([repeated], "test-two"))
        self.assertEqual(len(self.engine.stories()), 1)
        self.assertEqual(self.engine.stories()[0]["revision"], 2)  # New evidence, same event.

    def test_new_facts_create_a_revision_and_keep_history(self):
        self.apply(envelope())
        updated = story()
        updated["claims"][0]["fact"] = "Synthetic crypto protocol aster corrected its routing announcement."
        updated["text_short"] = "Synthetic protocol aster has corrected its test announcement."
        updated["correction_of"] = self.engine.stories()[0]["id"]
        report = self.apply(envelope([updated], "correction"))
        self.assertEqual(len(report["updated"]), 1)
        self.assertEqual(self.engine.stories()[0]["revision"], 2)
        count = self.engine.db.execute("SELECT count(*) FROM revisions").fetchone()[0]
        self.assertEqual(count, 2)
        self.assertEqual(self.latest()["formats"]["5min"]["segments"][0]["stories"][0]["revision"], 2)

    def test_invalid_claim_rejects_whole_batch_and_keeps_last_good(self):
        self.apply(envelope())
        before = (self.output / "latest.json").read_bytes()
        data = envelope([story("new"), story("bad")], "bad-batch")
        data["stories"][1]["claims"][0]["source_urls"] = ["https://example.org/not-encountered"]
        with self.assertRaisesRegex(ValueError, "absent"):
            self.apply(data)
        self.assertEqual(len(self.engine.stories()), 1)
        self.assertEqual(before, (self.output / "latest.json").read_bytes())

    def test_unused_provider_profile_citations_do_not_poison_valid_claims(self):
        data = envelope()
        data["citations"].extend(["https://x.com/i/user/123", "http://example.org/unused"])
        self.assertEqual(self.apply(data)["status"], "rendered")

    def test_contradictory_versions_in_one_batch_roll_back(self):
        one, two = story(), story()
        two["claims"][0]["fact"] = "A contradictory synthetic crypto fact."
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.apply(envelope([one, two]))
        self.assertEqual(self.engine.stories(), [])
        self.assertFalse((self.output / "latest.json").exists())

    def test_withdrawal_invalidates_current_output(self):
        self.apply(envelope())
        withdrawn = story()
        withdrawn["status"] = "withdrawn"
        withdrawn["claims"][0]["fact"] = "Synthetic crypto protocol aster withdrew its announcement."
        result = self.apply(envelope([withdrawn], "withdrawal"))
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(self.latest()["available"])
        self.assertEqual(self.latest()["full_script"], "")
        self.assertEqual(self.engine.stories()[0]["status"], "withdrawn")

    def test_replaying_old_withdrawal_cannot_invalidate_a_newer_edition(self):
        self.apply(envelope())
        withdrawn = story()
        withdrawn["status"] = "withdrawn"
        data = envelope([withdrawn], "withdrawal")
        self.apply(data)
        self.apply(envelope([story("new-event")], "new-edition"))
        before = self.latest()
        self.apply(data)
        self.assertEqual(self.latest(), before)
        self.assertTrue(self.latest()["available"])

    def test_known_story_withdrawal_does_not_need_to_repeat_crypto_keywords(self):
        self.apply(envelope())
        item = story()
        item["status"] = "withdrawn"
        item["claims"][0]["fact"] = "The issuer withdrew its earlier announcement."
        result = self.apply(envelope([item], "withdrawal-without-keywords"))
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(self.latest()["available"])

    def test_unverified_leads_do_not_generate_spoken_copy(self):
        lead = story()
        lead["status"] = "unverified"
        result = self.apply(envelope([lead]))
        self.assertTrue(result["withheld"])
        self.assertFalse((self.output / "latest.json").exists())

    def test_unchanged_coverage_cannot_renew_an_expired_story(self):
        self.apply(envelope())
        before = self.latest()
        later = NOW + dt.timedelta(minutes=65)
        report = self.apply(envelope([story()], "test-two", later), later)
        self.assertEqual(report["status"], "unchanged")
        self.assertEqual(self.latest(), before)
        self.assertLess(news.parse_time(self.latest()["expires_at"]), later)

    def test_current_announcements_carry_into_a_later_hour_with_new_developments(self):
        self.apply(envelope())
        original = self.engine.stories()[0]
        later = NOW + dt.timedelta(minutes=65)
        added = story("new-hour")
        added["occurred_at"] = later.isoformat()
        self.apply(envelope([story(), added], "new-hour", later), later)
        self.assertEqual(self.engine.previous_pool()["stories"][0]["id"], original["id"])
        self.assertEqual(self.engine.previous_pool()["stories"][0]["revision"], 1)
        self.assertEqual(self.engine.previous_pool()["stories"][0]["expires_at"], original["expires_at"])
        self.assertTrue(self.engine.previous_pool()["reuse"]["actual"] > 0)

    def test_failed_output_promotion_recovers_without_research_or_duplicate_revisions(self):
        data = envelope()
        with patch("news_engine.write_artifacts", side_effect=OSError("synthetic interrupted promotion")):
            with self.assertRaises(OSError):
                self.apply(data)
        self.assertEqual(len(self.engine.stories()), 1)
        self.assertFalse((self.output / "latest.json").exists())
        report = self.apply(data)
        self.assertEqual(report["status"], "already_processed")
        self.assertEqual(self.latest()["research_id"], data["research_id"])
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM revisions").fetchone()[0], 1)

    def test_empty_research_keeps_last_good_without_new_timestamp(self):
        self.apply(envelope())
        before = (self.output / "latest.json").read_bytes()
        report = self.apply(envelope([], "empty"))
        self.assertEqual(report["status"], "empty_research")
        self.assertEqual(before, (self.output / "latest.json").read_bytes())

    def test_expired_and_unrelated_leads_are_rejected_without_poisoning_current_news(self):
        stale, unrelated, valid = story("stale"), story("stock-rules"), story("current")
        stale["occurred_at"] = (NOW - dt.timedelta(days=3)).isoformat()
        unrelated["claims"][0]["fact"] = "Synthetic stock-market settlement procedures changed."
        result = self.apply(envelope([stale, unrelated, valid]))
        self.assertEqual(result["status"], "rendered")
        self.assertEqual({r["reason"] for r in result["rejected"]}, {"outside_lookback", "outside_crypto_scope"})
        self.assertEqual(len(self.engine.stories()), 1)
        self.assertEqual(self.engine.stories()[0]["event_key"], valid["event_key"])

    def test_market_observations_expire_sooner_than_announcements(self):
        item = story()
        item["event_url"] = build_feed.SOURCES["gt_solana"]
        item["claims"][0]["source_urls"] = [item["event_url"]]
        self.apply(envelope([item]))
        stored = self.engine.stories()[0]
        self.assertEqual(stored["evidence_type"], "market_observation")
        self.assertEqual(news.parse_time(stored["expires_at"]), NOW + dt.timedelta(minutes=60))

    def test_a_crypto_headline_cannot_disguise_unrelated_claims(self):
        item = story()
        item["headline"] = "Crypto angle on stock settlement"
        item["claims"][0]["fact"] = "A synthetic traditional stock-settlement announcement."
        result = self.apply(envelope([item]))
        self.assertEqual(result["status"], "no_eligible_stories")
        self.assertEqual(result["rejected"][0]["reason"], "outside_crypto_scope")
        self.assertFalse((self.output / "latest.json").exists())

    def test_ordinary_updates_append_and_operator_breaking_precedes_other_segments(self):
        self.apply(envelope([story("old", "metas")]))
        old_id = self.engine.stories()[0]["id"]
        self.apply(envelope([story("new", "trenches")], "second"))
        output_stories = [s for seg in self.latest()["formats"]["5min"]["segments"] for s in seg["stories"]]
        self.assertEqual(output_stories[0]["story_id"], old_id)
        new_id = next(s["id"] for s in self.engine.stories() if s["event_key"] == "test:new:routing")
        self.config["breaking_story_ids"] = [new_id]
        self.apply(envelope([story("third", "broader")], "third"))
        output_stories = [s for seg in self.latest()["formats"]["5min"]["segments"] for s in seg["stories"]]
        self.assertEqual(output_stories[0]["story_id"], new_id)
        self.assertTrue(output_stories[0]["breaking"])

    def test_feed_cannot_set_breaking_flag(self):
        incoming = story()
        incoming["breaking"] = True
        self.apply(envelope([incoming]))
        self.assertFalse(self.engine.previous_pool()["stories"][0]["breaking"])

    def test_invalid_freshness_and_urls_fail_without_output(self):
        for changed in ("2026-10-17T14:00:00", (NOW + dt.timedelta(minutes=5)).isoformat(),
                        (NOW - dt.timedelta(hours=2)).isoformat()):
            with self.subTest(changed=changed):
                data = envelope()
                data["researched_at"] = changed
                with self.assertRaises(ValueError):
                    self.apply(data)
        for url in ("http://example.org/post", "https://localhost/post", "https://127.0.0.1/post",
                    "https://user:password@example.org/post", "https://x.com/i/trending/123"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                news.canonical_url(url, event=True)
        self.assertFalse(self.output.exists())

    def test_a_new_collection_timestamp_cannot_hide_stale_market_observations(self):
        snapshot = {"collected_at": NOW.isoformat(), "sources": {"test": {
            "source_url": "https://example.org/data", "status": "ok", "data": {"value": 1},
            "observed_at": (NOW - dt.timedelta(hours=2)).isoformat()}}}
        with self.assertRaisesRegex(ValueError, "observation"):
            news.validate_snapshot(snapshot, self.config, NOW)
        data = envelope()
        data["data_snapshot_at"] = (NOW - dt.timedelta(hours=2)).isoformat()
        with self.assertRaisesRegex(ValueError, "snapshot"):
            self.apply(data)

    def test_url_identity_ignores_tracking_and_x_handle_changes(self):
        self.assertEqual(news.canonical_url("https://twitter.com/example/status/123?s=20"),
                         news.canonical_url("https://x.com/i/status/123"))
        self.assertEqual(news.canonical_url("https://example.org/post?utm_source=x#part"), "https://example.org/post")

    def test_quota_reserves_before_failure_and_is_shared_between_instances(self):
        self.config["daily_research_calls"] = 1
        self.engine.reserve_request("request-one", NOW)
        self.engine.finish_request("request-one", "failed_or_uncertain")
        another = news.Engine(self.root / "state", self.config)
        try:
            with self.assertRaisesRegex(research.ResearchError, "limit"):
                another.reserve_request("request-two", NOW)
            another.reserve_request("tomorrow", NOW + dt.timedelta(days=1))
        finally:
            another.close()

    def test_process_lock_is_exclusive_and_released(self):
        with news.run_lock(self.root / "locked"):
            with self.assertRaisesRegex(ValueError, "Another engine"):
                with news.run_lock(self.root / "locked"):
                    self.fail("Concurrent run acquired the lock")
        with news.run_lock(self.root / "locked"):
            pass

    def test_config_rejects_unbounded_calls_and_boolean_numeric_fields(self):
        for field, value in (("daily_research_calls", 100000), ("max_turns", True), ("reuse_target", 2)):
            config = copy.deepcopy(self.config)
            config[field] = value
            path = self.root / "config.json"
            path.write_text(json.dumps(config))
            with self.subTest(field=field), self.assertRaises(ValueError):
                news.load_config(path)


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.config = {**news.load_config(), "backend": "xai", "model": "grok-4.7"}

    def test_api_request_has_documented_tools_limits_and_no_data_driven_tools(self):
        request = research.research_request(self.config, {"sources": {}}, [], NOW)
        self.assertEqual([tool["type"] for tool in request["tools"]], ["x_search", "web_search"])
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        self.assertEqual(request["max_turns"], self.config["max_turns"])
        self.assertFalse(request["store"])

    def test_response_extracts_real_annotations_and_usage(self):
        response = {"status": "completed", "id": "provider-one", "usage": {"output_tokens": 20},
                    "output": [{"type": "message", "content": [{"type": "output_text",
                     "text": json.dumps({"stories": [story()]}),
                     "annotations": [{"type": "url_citation", "url": story()["event_url"]}]}]}]}
        parsed = research.parse_response(response, "request-one", NOW, NOW)
        self.assertEqual(parsed["citations"], [story()["event_url"]])
        self.assertEqual(parsed["usage"]["output_tokens"], 20)
        response["status"] = "incomplete"
        with self.assertRaises(research.ResearchError):
            research.parse_response(response, "incomplete", NOW, NOW)

    def test_cli_is_readonly_and_uses_no_shell_or_auth_file_extraction(self):
        payload = {"stopReason": "EndTurn", "requestId": "cli-request", "text": json.dumps({
            "stories": [story()], "citations": [story()["event_url"]]})}
        def start(args, **kwargs):
            self.assertIn("--no-subagents", args)
            self.assertEqual(args[args.index("--tools") + 1], "web_search,web_fetch")
            self.assertEqual(args[args.index("--allow") + 1], "WebFetch")
            self.assertNotIn("shell", kwargs)
            self.assertNotIn("--always-approve", args)
            kwargs["stdout"].write(json.dumps(payload))
            kwargs["stdout"].flush()
            class Process:
                returncode = 0
                def wait(self, timeout=None):
                    return 0
            return Process()
        with patch("research.shutil.which", return_value="/fake/grok"), patch("research.subprocess.Popen", side_effect=start):
            result = research.run_grok_cli({**self.config, "model": ""}, {"sources": {}}, [], "cli-one", NOW)
        self.assertEqual(result["provenance"], "grok_cli")
        self.assertEqual(result["citation_validation"], "model_reported")

    def test_cli_timeout_kills_process_group_without_retry(self):
        class Process:
            pid = 123456
            calls = 0
            def wait(self, timeout=None):
                self.calls += 1
                if timeout:
                    raise subprocess.TimeoutExpired("grok", timeout)
        process = Process()
        with patch("research.shutil.which", return_value="/fake/grok"), patch("research.subprocess.Popen", return_value=process) as start, patch("research.os.killpg") as kill:
            with self.assertRaisesRegex(research.ResearchError, "timed out"):
                research.run_grok_cli(self.config, {"sources": {}}, [], "cli-timeout", NOW)
            self.assertEqual(start.call_count, 1)
            kill.assert_called_once()

    def test_failed_collect_does_not_reuse_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_feed.collect(root, ["cg_trending"], fetch=lambda _: {"coins": [1]}, pause=lambda _: None)
            def fail(_):
                raise OSError("offline")
            snapshot = build_feed.collect(root, ["cg_trending"], fetch=fail, pause=lambda _: None)
            self.assertEqual(snapshot["sources"]["cg_trending"]["status"], "error")
            self.assertNotIn("data", json.loads((root / "cg_trending.json").read_text()))


if __name__ == "__main__":
    unittest.main()
