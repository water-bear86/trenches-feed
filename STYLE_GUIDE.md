# Newscast style guide (applies to every format and every edition)

Voice: a calm, neutral, authoritative public-broadcaster hourly newscast. No slang, hype, jokes or trader jargon
("degen", "ape", "moon", "rug", "cue the…", "let's go", "red candles"). Write for a general listener.

- **Lead first:** the first sentence carries the news (who, what, how much).
- **Attribution:** "according to CoinGecko", "the company says", "figures shared by…", "could not be independently verified".
  Claims from X posts or AI-written X summaries are attributed or framed as reports, never stated as fact.
- **Explain jargon briefly** on first use: memecoin, stablecoin, leverage/liquidation, exchange-traded fund, market maker,
  multi-signature, tokenized shares, over-the-counter.
- **Market numbers, business-report style, as variables only.** Never write a live price, 24h change or market total as a number.
  Use the variables (see README): "As of {{PRICES_AS_OF_SPOKEN}}, Bitcoin was trading near {{BTC_PRICE}} U.S. dollars,
  {{BTC_CHANGE_SPOKEN}} over twenty-four hours." Phrase sentences so they read naturally in any direction: never hard-code
  "down"/"up"/"fell" around a price; use {{..._CHANGE_SPOKEN}}, {{MARKET_DIRECTION}} ("Cryptocurrency prices {{MARKET_DIRECTION}}")
  and {{MARKET_DIRECTION_PAST}}. Don't attribute live prices to a named source; the station supplies them.
  Reported figures tied to a story (a token's reported gain, ETF flows) stay as attributed text.
