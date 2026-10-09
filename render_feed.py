#!/usr/bin/env python3
"""Render a story pool (editions/<date>.pool.json) into clock-slot formats for the radio station.

Outputs (in this folder):
  latest.json                    {"generated_at", "formats": {"12min","7min","5min","2.5min"}}
  latest-12min.json ... latest-2.5min.json
  feed-<date>.json               same as latest.json for that edition (archive)
  feed-<date>-<fmt>.md           human-readable scripts
  feed-<date>.md                 index pointing to all four scripts (12-min script inline)
  schedule.json                  clock slot (PT) -> format
  feed.xml                       RSS 2.0: one item per story (12-min wording) + one full-script item per format
Usage: python3 render_feed.py [YYYY-MM-DD]
Env: FEED_BASE_URL (public base URL once hosted); optional STATION_NAME, ANCHOR_NAME,
     TIME_OF_DAY_GREETING, TIME_SPOKEN to pre-fill {{TEMPLATE}} variables (left unfilled by default;
     TIME_* should normally be filled by the station at playback).
"""
import json, os, sys, glob, datetime, email.utils, hashlib
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape

BASE = os.path.dirname(os.path.abspath(__file__))
URL = os.environ.get("FEED_BASE_URL", "https://example.com/trenches-feed").rstrip("/")
WPM = 150
FORMATS = {  # name: (target words, label, max priority, text style, intro style)
    "12min": (1800, "12-minute program", 3, "long", "long"),
    "7min": (1050, "7-minute program", 2, "medium", "long"),
    "5min": (750, "5-minute program", 2, "short", "short"),
    "2.5min": (375, "2-minute-30 flash (headlines + top briefs)", 2, "flash", "flash"),
}
wc = lambda s: len(s.split())
VARIABLES = {  # template variables left UNFILLED in every published output; the station substitutes them at playback
    # --- station / clock ---
    "STATION_NAME": {"description": "Station name as spoken on air", "format": "spoken words", "example": "Example FM", "spoken_word_estimate": 2},
    "ANCHOR_NAME": {"description": "On-air anchor/host name", "format": "spoken words", "example": "Alex Morgan", "spoken_word_estimate": 2},
    "TIME_OF_DAY_GREETING": {"description": "Greeting for playback time", "format": "spoken words, sentence case", "example": "Good evening", "spoken_word_estimate": 2},
    "TIME_SPOKEN": {"description": "Playback time, Pacific", "format": "spoken words", "example": "six thirty Pacific time", "spoken_word_estimate": 4},
    # --- market (all prices in U.S. dollars, 24-hour change) ---
    "PRICES_AS_OF_SPOKEN": {"description": "Time the station's price snapshot was taken", "format": "spoken words", "example": "six twenty-nine Pacific time", "spoken_word_estimate": 4, "source_hint": "timestamp of the price read"},
    "MARKET_DIRECTION": {"description": "Present-tense verb phrase for BTC/ETH/SOL overall 24h move (used in headline tease: 'Cryptocurrency prices {{MARKET_DIRECTION}}')", "format": "one of: 'fall' | 'rise' | 'are mixed' | 'hold steady'", "example": "fall", "spoken_word_estimate": 1, "source_hint": "derive from BTC/ETH/SOL 24h changes (all <= -0.5% fall; all >= +0.5% rise; all within +/-0.5% hold steady; else are mixed)"},
    "MARKET_DIRECTION_PAST": {"description": "Past-tense form of MARKET_DIRECTION ('Cryptocurrency prices {{MARKET_DIRECTION_PAST}} over the past day')", "format": "one of: 'fell' | 'rose' | 'were mixed' | 'held steady'", "example": "fell", "spoken_word_estimate": 1, "source_hint": "same rule as MARKET_DIRECTION"},
    "BTC_PRICE": {"description": "Bitcoin price in U.S. dollars, rounded for speech (BTC to nearest 100, ETH to nearest 5, others to whole dollars); script supplies the word 'dollars'", "format": "spoken words", "example": "eighty-one thousand seven hundred", "spoken_word_estimate": 4, "source_hint": "Price feeds via Helius RPC, e.g. Pyth BTC / USD"},
    "BTC_CHANGE_SPOKEN": {"description": "Bitcoin 24h change as a spoken phrase including direction (Canadian 'per cent')", "format": "spoken words: 'up about X per cent' | 'down about X per cent' | 'little changed'", "example": "down about two per cent", "spoken_word_estimate": 5, "source_hint": "24h change from Price feeds via Helius RPC, e.g. Pyth BTC / USD (current vs. 24h-ago price)"},
    "BTC_CHANGE_PCT": {"description": "Bitcoin 24h change, numeric (machine field; not read in scripts)", "format": "digits, signed, one decimal", "example": "-2.0", "spoken_word_estimate": 0, "used_in_scripts": False, "source_hint": "Price feeds via Helius RPC, e.g. Pyth BTC / USD"},
    "BTC_PRICE_DIGITS": {"description": "Bitcoin price for written headlines (not spoken)", "format": "digits with $ and commas, rounded to nearest 100", "example": "$81,700", "spoken_word_estimate": 2, "source_hint": "Price feeds via Helius RPC, e.g. Pyth BTC / USD"},
    "ETH_PRICE": {"description": "Ether price in U.S. dollars, rounded for speech (BTC to nearest 100, ETH to nearest 5, others to whole dollars); script supplies the word 'dollars'", "format": "spoken words", "example": "two thousand four hundred and seventy-five", "spoken_word_estimate": 6, "source_hint": "Price feeds via Helius RPC, e.g. Pyth ETH / USD"},
    "ETH_CHANGE_SPOKEN": {"description": "Ether 24h change as a spoken phrase including direction (Canadian 'per cent')", "format": "spoken words: 'up about X per cent' | 'down about X per cent' | 'little changed'", "example": "down about four per cent", "spoken_word_estimate": 5, "source_hint": "24h change from Price feeds via Helius RPC, e.g. Pyth ETH / USD (current vs. 24h-ago price)"},
    "ETH_CHANGE_PCT": {"description": "Ether 24h change, numeric (machine field; not read in scripts)", "format": "digits, signed, one decimal", "example": "-4.2", "spoken_word_estimate": 0, "used_in_scripts": False, "source_hint": "Price feeds via Helius RPC, e.g. Pyth ETH / USD"},
    "SOL_PRICE": {"description": "Solana price in U.S. dollars, rounded for speech (BTC to nearest 100, ETH to nearest 5, others to whole dollars); script supplies the word 'dollars'", "format": "spoken words", "example": "one hundred and nine", "spoken_word_estimate": 4, "source_hint": "Price feeds via Helius RPC, e.g. Pyth SOL / USD"},
    "SOL_CHANGE_SPOKEN": {"description": "Solana 24h change as a spoken phrase including direction (Canadian 'per cent')", "format": "spoken words: 'up about X per cent' | 'down about X per cent' | 'little changed'", "example": "down about six and a half per cent", "spoken_word_estimate": 8, "source_hint": "24h change from Price feeds via Helius RPC, e.g. Pyth SOL / USD (current vs. 24h-ago price)"},
    "SOL_CHANGE_PCT": {"description": "Solana 24h change, numeric (machine field; not read in scripts)", "format": "digits, signed, one decimal", "example": "-6.5", "spoken_word_estimate": 0, "used_in_scripts": False, "source_hint": "Price feeds via Helius RPC, e.g. Pyth SOL / USD"},
    "HYPE_PRICE": {"description": "Hyperliquid (HYPE) price in U.S. dollars, rounded for speech (BTC to nearest 100, ETH to nearest 5, others to whole dollars); script supplies the word 'dollars'", "format": "spoken words", "example": "eighty-four", "spoken_word_estimate": 1, "source_hint": "Price feeds via Helius RPC, e.g. Pyth HYPE / USD"},
    "HYPE_CHANGE_SPOKEN": {"description": "Hyperliquid (HYPE) 24h change as a spoken phrase including direction (Canadian 'per cent')", "format": "spoken words: 'up about X per cent' | 'down about X per cent' | 'little changed'", "example": "down about five per cent", "spoken_word_estimate": 5, "source_hint": "24h change from Price feeds via Helius RPC, e.g. Pyth HYPE / USD (current vs. 24h-ago price)"},
    "HYPE_CHANGE_PCT": {"description": "Hyperliquid (HYPE) 24h change, numeric (machine field; not read in scripts)", "format": "digits, signed, one decimal", "example": "-4.9", "spoken_word_estimate": 0, "used_in_scripts": False, "source_hint": "Price feeds via Helius RPC, e.g. Pyth HYPE / USD"},
    "TOTAL_MARKET_CAP": {"description": "Total value of all cryptocurrencies in U.S. dollars; script supplies the word 'dollars'", "format": "spoken words", "example": "two point seven eight trillion", "spoken_word_estimate": 5, "source_hint": "not available from Pyth/Helius; use an aggregator (e.g. CoinGecko /global) or drop the 12-min sentence"},
    "TOTAL_MARKET_CAP_CHANGE_SPOKEN": {"description": "24h change of total crypto market value", "format": "spoken words: 'up about X per cent' | 'down about X per cent' | 'little changed'", "example": "down about five per cent", "spoken_word_estimate": 4, "source_hint": "same as TOTAL_MARKET_CAP"},
}
# Optional: fill NON-market variables at render time from env vars (STATION_NAME, ANCHOR_NAME, ...). Unfilled by default.
# Market/price variables are never filled by this script.
FILLED = {k: os.environ[k] for k in ("STATION_NAME", "ANCHOR_NAME", "TIME_OF_DAY_GREETING", "TIME_SPOKEN") if os.environ.get(k)}
def fill(o):
    if isinstance(o, str):
        for k, v in FILLED.items(): o = o.replace("{{" + k + "}}", v)
        return o
    if isinstance(o, list): return [fill(x) for x in o]
    if isinstance(o, dict): return {k: fill(v) for k, v in o.items()}
    return o
def spoken_wc(script):
    return wc(script) + sum(script.count("{{" + k + "}}") * (v["spoken_word_estimate"] - 1) for k, v in VARIABLES.items())
def var_block():
    return {k: dict({"token": "{{" + k + "}}"}, **v, filled=k in FILLED, value=FILLED.get(k)) for k, v in VARIABLES.items()}

def text_for(s, style):
    if style == "long": return (s["text_medium"] + " " + s["text_extra"]).strip()
    if style == "medium": return s["text_medium"]
    if style == "short": return s["text_short"] or s["text_medium"]
    return s["text_flash"] or s["headline"]

def build(pool, fmt):
    target, label, maxpri, style, ist = FORMATS[fmt]
    chosen = {s["id"]: style for s in pool["stories"] if s["priority"] <= maxpri}
    def assemble():
        segs, parts = [], [pool[f"intro_{ist}"]]
        for seg in pool["segments"]:
            items = [s for s in pool["stories"] if s["segment"] == seg["id"] and s["id"] in chosen]
            if not items: continue
            sintro = seg["name"] + "." if style == "flash" else seg["intro"]
            parts.append(sintro)
            st = [dict(id=f"{pool['edition']}-{fmt}-{s['id']}", segment=seg["name"], segment_id=seg["id"],
                       headline=s["headline"], script_text=text_for(s, chosen[s["id"]]),
                       source_urls=s["source_urls"], timestamp=s["timestamp"]) for s in items]
            for x in st: x["word_count"] = wc(x["script_text"]); parts.append(x["script_text"])
            segs.append(dict(id=seg["id"], name=seg["name"], intro=sintro, stories=st))
        parts.append(pool[f"outro_{ist}"])
        return segs, "\n\n".join(parts)
    segs, script = assemble()
    # trim toward target: shed 12-min extras, then lowest-priority stories, then shorten (latest stories first)
    idx = {x["id"]: i for i, x in enumerate(pool["stories"])}
    order = sorted(pool["stories"], key=lambda s: (-s["priority"], -idx[s["id"]]))  # least important, latest first
    w0 = spoken_wc(script)
    shrink = {"long": "medium", "medium": "short", "short": "flash"}
    def over(): return spoken_wc(script) > target
    for step in ("extras", "drop3", "shorten"):
        for s in [x for x in order if x["id"] in chosen]:
            if not over(): break
            st = chosen.get(s["id"])
            if step == "extras" and st == "long" and s["text_extra"]:
                chosen[s["id"]] = "medium"
            elif step == "drop3" and s["priority"] == 3:
                del chosen[s["id"]]
            elif step == "shorten" and st in ("medium", "short"):
                chosen[s["id"]] = shrink[st]
            else:
                continue
            segs, script = assemble()
    # fill toward target: add lower-priority stories (medium/long styles) or upgrade short -> medium
    extras = [s for s in pool["stories"] if s["id"] not in chosen and style == "medium"]
    up_to = {"short": "medium", "flash": "short"}.get(style)
    upgrades = [s for s in pool["stories"] if s["id"] in chosen and up_to and s["priority"] == 1]
    for s in ([] if spoken_wc(script) < w0 else extras + upgrades):  # no refill after trimming
        new = "medium" if s in extras else up_to
        add = wc(text_for(s, new)) - (wc(text_for(s, style)) if s["id"] in chosen else 0)
        if spoken_wc(script) + add <= target:
            chosen[s["id"]] = new; segs, script = assemble()
    words = spoken_wc(script)
    return dict(variables=var_block(), format=fmt, label=label, edition=pool["edition"], title=f"{pool['title_base']} ({label})",
                generated_at=pool["generated_at"], data_snapshot_at=pool["data_snapshot_at"],
                target_words=target, word_count=words, est_read_seconds=round(words / WPM * 60),
                est_read_time=f"{round(words / WPM * 60) // 60}:{round(words / WPM * 60) % 60:02d}", wpm_assumed=WPM,
                disclaimer=pool["disclaimer"], intro=pool[f"intro_{ist}"], outro=pool[f"outro_{ist}"],
                segments=segs, full_script=script)

def md(ed):
    L = [f"# {ed['title']}", "", f"*{ed['word_count']} words, ~{ed['est_read_time']} at {WPM} wpm · data snapshot {ed['data_snapshot_at']} · {ed['disclaimer']}*", "",
         "**Host intro:** " + ed["intro"], ""]
    for seg in ed["segments"]:
        L += [f"## {seg['name']}", "", f"_{seg['intro']}_", ""]
        for i, s in enumerate(seg["stories"], 1):
            L += [f"**{i}. {s['headline']}**", "", s["script_text"], "", "<sub>Sources: " + " · ".join(s["source_urls"]) + "</sub>", ""]
    L += ["**Host outro:** " + ed["outro"], ""]
    return "\n".join(L)

def schedule():
    slots = {}
    for h in range(24):
        hh = f"{h:02d}"
        slots[f"{hh}:00"] = "12min" if h % 3 == 0 else "7min"
        slots[f"{hh}:15"] = "2.5min"; slots[f"{hh}:30"] = "5min"; slots[f"{hh}:45"] = "2.5min"
    return dict(timezone="America/Los_Angeles", wpm_assumed=WPM,
                rules={"03/06/09/12 o'clock AM+PM (:00)": "12min", "other tops of hour (:00)": "7min", ":30": "5min", ":15 and :45": "2.5min"},
                endpoints={f: f"{URL}/latest-{f}.json" for f in FORMATS} | {"all": f"{URL}/latest.json", "rss": f"{URL}/feed.xml"},
                slots=slots)

def rfc822(ts): return email.utils.format_datetime(datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")))

def rss(eds_by_date):
    now = email.utils.format_datetime(datetime.datetime.now().astimezone())
    X = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">', "<channel>",
         "<title>Trenches Report: crypto &amp; memecoin news feed</title>", f"<link>{URL}/</link>",
         f'<atom:link href="{URL}/feed.xml" rel="self" type="application/rss+xml"/>',
         "<description>Radio-ready crypto news: the trenches, broader crypto, trending metas. Not financial advice.</description>",
         "<language>en-us</language>", f"<lastBuildDate>{now}</lastBuildDate>", "<ttl>30</ttl>"]
    def item(title, text, link, cat, gid, ts):
        return ["<item>", f"<title>{escape(title)}</title>", f"<link>{escape(link)}</link>", f"<description>{escape(text)}</description>",
                f"<category>{escape(cat)}</category>", f'<guid isPermaLink="false">trenches-{gid}-{hashlib.sha1(text.encode()).hexdigest()[:8]}</guid>',
                f"<pubDate>{rfc822(ts)}</pubDate>", "</item>"]
    for date, eds in eds_by_date:
        for f, ed in eds.items():
            X += item(f"Full script ({ed['label']}, {ed['word_count']} words): {ed['title']}", ed["full_script"],
                      f"{URL}/latest-{f}.json", f"Full Script {f}", f"{date}-full-{f}", ed["generated_at"])
        for seg in eds["12min"]["segments"]:
            for s in seg["stories"]:
                X += item(f"[{s['segment']}] {s['headline']}", s["script_text"] + "\n\nSources:\n" + "\n".join(s["source_urls"]),
                          s["source_urls"][0], s["segment"], s["id"], s["timestamp"])
    return "\n".join(X + ["</channel>", "</rss>", ""])

def main():
    files = sorted(glob.glob(os.path.join(BASE, "editions", "*.pool.json")))
    if not files: sys.exit("no editions/*.pool.json")
    target = os.path.join(BASE, "editions", f"{sys.argv[1]}.pool.json") if len(sys.argv) > 1 else files[-1]
    pool = json.load(open(target, encoding="utf-8")); day = pool["edition"]
    pool = fill(pool)  # only fills variables explicitly set via env (none by default); prices are never filled here
    now = datetime.datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds")
    pool["edition_generated_at"] = pool["generated_at"]; pool["generated_at"] = now
    eds = {f: build(pool, f) for f in FORMATS}
    for ed in eds.values(): ed["prices_as_of"] = "{{PRICES_AS_OF_SPOKEN}}"; ed["prices_filled_by"] = "station"
    bundle = dict(variables=var_block(), generated_at=now, edition_generated_at=pool["edition_generated_at"],
                  prices_as_of="{{PRICES_AS_OF_SPOKEN}}", prices_filled_by="station", edition=day, data_snapshot_at=pool["data_snapshot_at"],
                  timezone="America/Los_Angeles", wpm_assumed=WPM, disclaimer=pool["disclaimer"], formats=eds)
    dump = lambda o, p: json.dump(o, open(os.path.join(BASE, p), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    dump(bundle, f"feed-{day}.json")
    if target == files[-1]:
        dump(bundle, "latest.json")
        for f, ed in eds.items(): dump(ed, f"latest-{f}.json")
    for f, ed in eds.items(): open(os.path.join(BASE, f"feed-{day}-{f}.md"), "w", encoding="utf-8").write(md(ed))
    idx = [f"# Trenches Report {day}: all formats", ""] + [f"- [{ed['label']}](feed-{day}-{f}.md): {ed['word_count']} words, ~{ed['est_read_time']}" for f, ed in eds.items()] + ["", "---", "", md(eds["12min"])]
    open(os.path.join(BASE, f"feed-{day}.md"), "w", encoding="utf-8").write("\n".join(idx))
    dump(schedule(), "schedule.json")
    hist = []
    for p in files[-3:][::-1]:
        pl = fill(json.load(open(p, encoding="utf-8"))); hist.append((pl["edition"], {f: build(pl, f) for f in FORMATS}))
    open(os.path.join(BASE, "feed.xml"), "w", encoding="utf-8").write(rss(hist))
    for f, ed in eds.items():
        print(f"{f:7s} target {ed['target_words']:5d}  words {ed['word_count']:5d}  ~{ed['est_read_time']}  stories {sum(len(s['stories']) for s in ed['segments'])}")

if __name__ == "__main__":
    main()
