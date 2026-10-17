import argparse
import datetime as dt
import http.client
import json
import shutil
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from feed_bridge import make_bridge, private_ipv4
from news_engine import BASE, Engine, UTC, load_config, timestamp
from newsroom import make_server, story_feed
from radar_sources import load_radar_config


class StoryFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.config = load_config()
        self.engine = Engine(self.state, self.config)
        self.now = dt.datetime.now(UTC)
        self.record = {"id": "story-test", "revision": 2, "status": "reported", "headline": "Synthetic story",
                       "timestamp": timestamp(self.now - dt.timedelta(minutes=2)),
                       "researched_at": timestamp(self.now - dt.timedelta(minutes=1)),
                       "expires_at": timestamp(self.now + dt.timedelta(minutes=5)),
                       "verification": "source_attributed", "source_urls": ["https://example.org/report"],
                       "text_short": "An explicitly synthetic story tests the private feed.",
                       "text_medium": "An explicitly synthetic story tests the private feed's source timestamps.",
                       "private_note": "Not part of the station contract", "csrf_token": "never expose"}
        self.put(self.record)

    def put(self, body):
        self.engine.db.execute("INSERT OR REPLACE INTO stories VALUES (?,?,?)",
                               (body["id"], "fixture", json.dumps(body)))
        self.engine.db.commit()

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()


class StoryFeedTests(StoryFixture, unittest.TestCase):
    def test_source_clocks_revisions_and_trusted_flags_are_preserved(self):
        unverified = {**self.record, "id": "unverified", "status": "unverified"}
        withdrawn = {**self.record, "id": "withdrawn", "status": "withdrawn",
                     "expires_at": timestamp(self.now - dt.timedelta(days=1))}
        expired = {**self.record, "id": "expired", "expires_at": timestamp(self.now - dt.timedelta(seconds=1))}
        for item in (unverified, withdrawn, expired):
            self.put(item)
        config = {**self.config, "breaking_story_ids": ["story-test", "unverified", "withdrawn", "missing"]}
        feed = story_feed(self.state, config, self.now)
        self.assertTrue(feed["complete"])
        self.assertEqual(feed["trusted_crazy_story_ids"], ["story-test"])
        self.assertEqual({s["id"] for s in feed["stories"]}, {"story-test", "unverified", "withdrawn"})
        item = next(s for s in feed["stories"] if s["id"] == "story-test")
        for key in ("revision", "timestamp", "researched_at", "expires_at", "source_urls"):
            self.assertEqual(item[key], self.record[key])
        self.assertNotIn("private_note", item)
        self.assertNotIn("csrf_token", json.dumps(feed))
        self.assertNotIn("generated_at", feed)

    def test_missing_state_is_not_created_and_large_feed_fails_closed(self):
        missing = self.state / "missing"
        with self.assertRaises(sqlite3.OperationalError):
            story_feed(missing, self.config, self.now)
        self.assertFalse(missing.exists())
        with patch("newsroom.MAX_STORY_FEED_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "response limit"):
                story_feed(self.state, self.config, self.now)

    def test_loopback_story_route_has_no_operator_token_and_rejects_wrong_host(self):
        radar_config = load_radar_config(BASE / "radar-config.json", self.config)
        server = make_server(self.state, self.config, radar_config, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request("GET", "/api/stories")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            data = json.loads(response.read())
            self.assertEqual(data["stories"][0]["id"], "story-test")
            self.assertNotIn("csrf_token", json.dumps(data))
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request("GET", "/api/stories", headers={"Host": "attacker.invalid"})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


@unittest.skipUnless(shutil.which("openssl"), "TLS transport checks require the documented OpenSSL tool")
class TLSBridgeTests(StoryFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cert_temp = tempfile.TemporaryDirectory()
        root = Path(cls.cert_temp.name)
        cls.cert, cls.key = root / "cert.pem", root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(cls.key), "-out", str(cls.cert), "-subj", "/CN=Newsroom test",
                        "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.cert_temp.cleanup()

    def setUp(self):
        super().setUp()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.cert, self.key)
        self.bed = self.state / "supplied-bed.mp3"
        self.bed.write_bytes(b"ID3 explicitly synthetic transport fixture")
        self.server = make_bridge("127.0.0.1", 0, self.state, BASE / "engine-config.json", context,
                                  ["127.0.0.1"], self.bed)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(self, path, method="GET", headers=None, context=None):
        context = context or ssl.create_default_context(cafile=str(self.cert))
        connection = http.client.HTTPSConnection("127.0.0.1", self.server.server_port, context=context, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_pinned_certificate_is_required_and_story_clock_is_unchanged(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.request("/api/stories", context=ssl.create_default_context())
        status, raw, headers = self.request("/api/stories")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(raw)["stories"][0]["researched_at"], self.record["researched_at"])
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)

    def test_only_fixed_routes_are_exposed_and_operator_actions_are_rejected(self):
        for path in ("/api/state", "/api/candidate", "/.state/newsroom.sqlite", "/assets/../cert.pem",
                     "/api/stories?include=secrets", "/assets/bed.mp3?path=/etc/passwd"):
            self.assertEqual(self.request(path)[0], 404, path)
        self.assertEqual(self.request("/api/stories", method="POST")[0], 405)
        self.assertEqual(self.request("/api/stories", headers={"Host": "attacker.invalid"})[0], 403)
        status, body, headers = self.request("/assets/bed.mp3")
        self.assertEqual((status, body, headers["Content-Type"]), (200, self.bed.read_bytes(), "audio/mpeg"))
        status, body, headers = self.request("/assets/bed.mp3", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(int(headers["Content-Length"]), self.bed.stat().st_size)

    def test_source_ip_is_checked_before_tls_or_reading_the_ledger(self):
        self.server.allowed_clients = frozenset(["127.0.0.2"])
        with patch("feed_bridge.story_feed") as read:
            with self.assertRaises(OSError):
                self.request("/api/stories")
            read.assert_not_called()

    def test_plaintext_and_wrong_certificate_hostname_are_rejected(self):
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        try:
            connection.sendall(b"GET /api/stories HTTP/1.0\r\n\r\n")
            try:
                self.assertEqual(connection.recv(1024), b"")
            except ConnectionResetError:
                pass
        finally:
            connection.close()
        context = ssl.create_default_context(cafile=str(self.cert))
        connection = http.client.HTTPSConnection("localhost", self.server.server_port, context=context, timeout=3)
        try:
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.request("GET", "/api/stories")
        finally:
            connection.close()

    def test_unavailable_feed_and_oversized_asset_fail_closed(self):
        with patch("feed_bridge.story_feed", side_effect=sqlite3.OperationalError("private database detail")):
            status, body, _ = self.request("/api/stories")
        self.assertEqual(status, 503)
        self.assertNotIn(b"private database detail", body)
        with patch("feed_bridge.MAX_ASSET_BYTES", 1):
            self.assertEqual(self.request("/assets/bed.mp3")[0], 503)


class BridgeConfigurationTests(unittest.TestCase):
    def test_only_explicit_private_lan_addresses_can_be_configured(self):
        self.assertEqual(private_ipv4("192.168.88.2"), "192.168.88.2")
        for value in ("0.0.0.0", "127.0.0.1", "169.254.1.1", "8.8.8.8", "localhost", "::1"):
            with self.assertRaises(argparse.ArgumentTypeError):
                private_ipv4(value)


if __name__ == "__main__":
    unittest.main()
