#!/usr/bin/env python3
"""Serve only newsroom stories and an optional supplied bed over a private TLS link."""

import argparse
import datetime as dt
import ipaddress
import json
import signal
import sqlite3
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from news_engine import BASE, UTC, load_config
from newsroom import story_feed

MAX_ASSET_BYTES = 10_000_000


def private_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise argparse.ArgumentTypeError("Use an explicit private IPv4 address.") from None
    if not address.is_private or address.is_loopback or address.is_unspecified or address.is_link_local:
        raise argparse.ArgumentTypeError("Use an explicit private LAN address, not a wildcard or public IP.")
    return str(address)


class TLSFeedServer(HTTPServer):
    """Reject other client IPs before performing a bounded TLS handshake."""

    request_queue_size = 8

    def __init__(self, address, handler, context, allowed_clients):
        self.context = context
        self.allowed_clients = frozenset(allowed_clients)
        super().__init__(address, handler)

    def get_request(self):
        raw, address = super().get_request()
        raw.settimeout(5)
        try:
            if address[0] not in self.allowed_clients:
                raise ConnectionRefusedError("Client is not allowed.")
            return self.context.wrap_socket(raw, server_side=True), address
        except (OSError, ValueError):
            raw.close()
            raise


def make_bridge(bind, port, state_dir, config_path, context, allowed_clients, bed_file=None):
    # Configuration is local-only. Requests cannot select paths, upstreams or actions.
    state_dir, config_path = Path(state_dir), Path(config_path)
    bed_file = Path(bed_file) if bed_file else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "NewsroomFeed/1"
        sys_version = ""

        def log_message(self, *_args):
            pass

        def send(self, status, body, content_type="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Host") != f"{bind}:{self.server.server_port}":
                self.send(403, {"error": "The configured feed host is required."})
                return
            if self.path == "/api/stories":
                try:
                    self.send(200, story_feed(state_dir, load_config(config_path), dt.datetime.now(UTC)))
                except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                    self.send(503, {"error": "The current story ledger is unavailable."})
                return
            if self.path == "/assets/bed.mp3" and bed_file:
                try:
                    with bed_file.open("rb") as handle:
                        body = handle.read(MAX_ASSET_BYTES + 1)
                    if not body or len(body) > MAX_ASSET_BYTES:
                        raise ValueError("Invalid supplied asset.")
                    self.send(200, body, "audio/mpeg")
                except (OSError, ValueError):
                    self.send(503, {"error": "The supplied asset is unavailable."})
                return
            self.send(404, {"error": "Not found."})

        do_HEAD = do_GET

        def do_POST(self):
            self.send(405, {"error": "This feed is read only."})

    return TLSFeedServer((bind, port), Handler, context, allowed_clients)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, type=private_ipv4)
    parser.add_argument("--allow-client", required=True, action="append", type=private_ipv4)
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--state-dir", type=Path, default=BASE / ".state")
    parser.add_argument("--config", type=Path, default=BASE / "engine-config.json")
    parser.add_argument("--cert", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--bed-file", type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Use a TCP port between 1 and 65535.")
    if args.key.stat().st_mode & 0o077:
        parser.error("The TLS private key must be readable by its owner only.")
    load_config(args.config)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert, args.key)
    server = make_bridge(args.bind, args.port, args.state_dir, args.config, context,
                         args.allow_client, args.bed_file)
    def stop(*_args):
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    print(json.dumps({"story_url": f"https://{args.bind}:{server.server_port}/api/stories",
                      "allowed_clients": args.allow_client, "read_only": True}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
