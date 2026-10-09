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
VARIABLES = {  # template variables left in scripts for the station to substitute at playback
    "STATION_NAME": {"token": "{{STATION_NAME}}", "description": "Station name, e.g. as it should be spoken on air", "example": "Example FM", "spoken_word_estimate": 2},
    "ANCHOR_NAME": {"token": "{{ANCHOR_NAME}}", "description": "On-air anchor/host name", "example": "Nova", "spoken_word_estimate": 1},
    "TIME_OF_DAY_GREETING": {"token": "{{TIME_OF_DAY_GREETING}}", "description": "Greeting for playback time: Good morning / Good afternoon / Good evening / Hey night owls", "example": "Good evening", "spoken_word_estimate": 2},
    "TIME_SPOKEN": {"token": "{{TIME_SPOKEN}}", "description": "Playback time as spoken, Pacific, e.g. 'six thirty Pacific'", "example": "six thirty Pacific", "spoken_word_estimate": 3},
}
# Optional: fill variables at render time from env vars (STATION_NAME, ANCHOR_NAME, ...). Unfilled by default.
FILLED = {k: os.environ[k] for k in VARIABLES if os.environ.get(k)}
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
    return {k: dict(v, filled=k in FILLED, value=FILLED.get(k)) for k, v in VARIABLES.items()}

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
                est_read_time=f"{words // WPM}:{round(words / WPM * 60) % 60:02d}", wpm_assumed=WPM,
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
    pool = fill(pool)
    eds = {f: build(pool, f) for f in FORMATS}
    bundle = dict(variables=var_block(), generated_at=pool["generated_at"], edition=day, data_snapshot_at=pool["data_snapshot_at"],
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
