import copy
import datetime as dt
import json
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import render_feed as render

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 9, 1, 38, tzinfo=dt.timezone.utc)


class RendererTests(unittest.TestCase):
    def setUp(self):
        self.pool = json.loads((ROOT / "editions/2026-10-08.pool.json").read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_pool_keeps_research_time_across_rerenders(self):
        one = render.render_to(self.pool, self.output, now=NOW)
        two = render.render_to(self.pool, self.output, now=NOW + dt.timedelta(minutes=2))
        self.assertEqual(one["generated_at"], self.pool["generated_at"])
        self.assertEqual(two["generated_at"], one["generated_at"])
        self.assertNotEqual(two["rendered_at"], one["rendered_at"])
        for fmt, ed in two["formats"].items():
            self.assertLessEqual(ed["word_count"], ed["target_words"], fmt)
            self.assertTrue(ed["full_script"])
        ET.fromstring((self.output / "feed.xml").read_text())

    def test_expired_pool_is_rejected_without_changing_last_good(self):
        render.render_to(self.pool, self.output, now=NOW)
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        with self.assertRaisesRegex(ValueError, "expired"):
            render.render_to(self.pool, self.output, now=NOW + dt.timedelta(days=8))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})

    def test_historical_review_is_explicitly_unavailable(self):
        bundle = render.render_to(self.pool, self.output, now=NOW + dt.timedelta(days=8), allow_stale=True)
        self.assertFalse(bundle["available"])
        self.assertEqual(bundle["generated_at"], self.pool["generated_at"])

    def test_full_stories_fit_all_word_budgets(self):
        self.pool["stories"] = self.pool["stories"][:1]
        item = self.pool["stories"][0]
        for field in ("text_medium", "text_extra", "text_short"):
            item[field] = "A synthetic sentence for testing complete story selection. " * 180
        item["text_flash"] = "A complete synthetic flash sentence."
        bundle = render.render_to(self.pool, self.output, now=NOW)
        for edition in bundle["formats"].values():
            self.assertLessEqual(edition["word_count"], edition["target_words"])
            self.assertTrue(edition["full_script"].endswith(self.pool["outro_flash"].split(".")[0] + ".") or edition["full_script"].endswith(edition["outro"]))

    def test_no_empty_filler_when_only_priority_three_story_exists(self):
        self.pool["stories"] = self.pool["stories"][:1]
        self.pool["stories"][0]["priority"] = 3
        bundle = render.render_to(self.pool, self.output, now=NOW)
        for edition in bundle["formats"].values():
            self.assertTrue(edition["segments"])
            self.assertTrue(edition["segments"][0]["stories"])

    def test_unknown_templates_and_naive_dates_are_rejected(self):
        for token in ("{{NEW_FAKE_TOKEN}}", "{{BTC_CHANGE_PCT}}", "{{BTC_PRICE}"):
            pool = copy.deepcopy(self.pool)
            pool["stories"][0]["text_short"] = token
            with self.subTest(token=token), self.assertRaises(ValueError):
                render.render_to(pool, self.output, now=NOW)
        self.pool["generated_at"] = "2026-10-09T01:35:00"
        with self.assertRaises(ValueError):
            render.render_to(self.pool, self.output, now=NOW)

    def test_empty_pool_never_creates_a_bulletin(self):
        self.pool["stories"] = []
        with self.assertRaises(ValueError):
            render.render_to(self.pool, self.output, now=NOW)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_publisher_dry_run_does_not_touch_worktree_or_git(self):
        current = dt.datetime.now(dt.timezone.utc)
        self.pool["generated_at"] = current.isoformat()
        self.pool["data_snapshot_at"] = current.isoformat()
        for story in self.pool["stories"]:
            story["timestamp"] = current.isoformat()
        path = self.output / "synthetic-pool.json"
        path.write_text(json.dumps(self.pool))
        status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True)
        before = status.stdout if status.returncode == 0 else None
        output_before = (ROOT / "latest.json").read_bytes()
        result = subprocess.run(["bash", str(ROOT / "publish.sh"), "--dry-run", "--pool", str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run passed", result.stdout)
        if before is not None:
            self.assertEqual(before, subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
        self.assertEqual(output_before, (ROOT / "latest.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
