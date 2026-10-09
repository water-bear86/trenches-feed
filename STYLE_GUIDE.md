# Newscast style guide (applies to every format and every edition)

Voice: a calm, neutral, authoritative public-broadcaster hourly newscast. No slang, hype, jokes or trader jargon
("degen", "ape", "moon", "rug", "cue the…", "let's go", "red candles"). Write for a general listener.

- **Lead first:** the first sentence carries the news (who, what, how much).
- **Attribution:** "according to CoinGecko", "the company says", "figures shared by…", "could not be independently verified".
  Claims from X posts or AI-written X summaries are attributed or framed as reports, never stated as fact.
- **Explain jargon briefly** on first use: memecoin, stablecoin, leverage/liquidation, exchange-traded fund, market maker,
  multi-signature, tokenized shares, over-the-counter.
- **Market numbers, business-report style (use the [[TOKENS]] from README, never hard-coded prices):** "Bitcoin was trading near eighty-one thousand seven hundred U.S. dollars this evening,
  down about two per cent." Round sensibly; say the source and the time of the price snapshot.
- **Canadian spelling and usage:** per cent, favour, centre, criticized, co-ordinated, cheque, "U.S." Numbers are written as spoken words.
- **Structure:**
  - Open: `{{TIME_OF_DAY_GREETING}}. It's {{TIME_SPOKEN}}. This is {{STATION_NAME}} News. I'm {{ANCHOR_NAME}}.`
  - Headline tease (12- and 7-minute formats): three one-line headlines, then a one-line "not financial advice" note.
  - Segment transitions: "We begin with…", "Turning now to…", "Finally, a look at…".
  - Close: price-time note, then `That's the news for this hour. I'm {{ANCHOR_NAME}}.`
- **No real broadcaster names, slogans or anchors.** Station and anchor come only from template variables.
- **Never** present a token as a recommendation; include the disclaimer in every format.
- **Lengths (150 wpm):** 12 min ≈ 1800 words, 7 min ≈ 1050, 5 min ≈ 750, 2:30 ≈ 375 (headlines + brief items).
  Each story supplies `text_medium` (7-min), `text_extra` (12-min add-on), `text_short` (5-min), `text_flash` (2:30); render_feed.py fits them to each target.
