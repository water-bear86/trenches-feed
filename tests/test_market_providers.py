import datetime as dt
import io
import os
import tempfile
import unittest
from unittest.mock import patch, Mock
import build_feed
import update_prices as prices
NOW = dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc)
def body():
    return {"status": {"error_code": "0"}, "data": [{"id": 5426, "symbol": "SOL", "quote": [{"id": 2781, "symbol": "USD", "price": 100, "percent_change_24h": 2, "last_updated": NOW.isoformat()}]}]}
class MarketTests(unittest.TestCase):
    def test_asset_quote_identity_freshness_and_finite_values(self):
        self.assertEqual(prices.cmc_assets(body(), {5426: "SOL"}, NOW)["SOL"]["usd"], 100)
        for change in [dict(id=1), dict(symbol="FAKE")]:
            bad = body(); bad["data"][0].update(change)
            self.assertEqual(prices.cmc_assets(bad, {5426: "SOL"}, NOW), {})
        for change in [dict(id=1), dict(price=float("nan")), dict(price=True), dict(last_updated=(NOW-dt.timedelta(minutes=4)).isoformat()), dict(last_updated=(NOW+dt.timedelta(minutes=1)).isoformat())]:
            bad = body(); bad["data"][0]["quote"][0].update(change)
            self.assertEqual(prices.cmc_assets(bad, {5426: "SOL"}, NOW), {})
    def test_banned_hosts_and_redirects_never_open(self):
        with patch("urllib.request.build_opener") as opener:
            for get in [prices.get, build_feed.get]:
                for host in ["api.coingecko.com", "pro-api.coingecko.com", "api.geckoterminal.com"]:
                    with self.assertRaises(ValueError): get("https://"+host+"/anything")
            opener.assert_not_called()
        self.assertIsNone(prices.NoRedirect().redirect_request(None, None, None, None, None, None))
    def test_key_only_in_fixed_cmc_header(self):
        opener = Mock(); opener.open.return_value = io.BytesIO(b"{}")
        with patch.dict(os.environ, {"CMC_PRO_API_KEY": "fixture-only-key"}), patch("urllib.request.build_opener", return_value=opener):
            prices.get("https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/quotes/latest?id=5426&convert=USD")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("X-cmc_pro_api_key"), "fixture-only-key")
        self.assertNotIn("fixture-only-key", request.full_url)
        self.assertNotIn("/public-api/", request.full_url)
    def test_collector_rejects_partial_prices(self):
        with tempfile.TemporaryDirectory() as root:
            result = build_feed.collect(root, ["prices"], fetch=lambda _: body(), pause=lambda _:None)
            self.assertEqual(result["sources"]["prices"]["status"], "error")
        self.assertTrue(all("coingecko" not in url and "geckoterminal" not in url for url in build_feed.SOURCES.values()))
if __name__ == "__main__": unittest.main()
