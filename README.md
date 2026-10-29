# Delve

Delve is a small, local stock-research dashboard. Enter a ticker such as
`AAPL`, choose how much price history to view, and explore a 30-trading-day
projection alongside recent news and source checks. This project is made by
Akshetha V.D.

It is designed to help you investigate an idea -> not to tell you what to buy or
sell.

## Get started

You need Python 3. Run these commands from the project folder:

```bash
python3 -m pip install -r requirements.txt
python3 delve.py
```

When you see `Delve is running`, open [http://127.0.0.1:8000](http://127.0.0.1:8000)
in your browser. Stop the app at any time with `Ctrl+C` in the terminal.

## Local database cache

Delve uses a local SQLite database named `delve_cache.sqlite3` to reduce repeat
API requests and make demonstrations load quickly. The database stores Yahoo
history, validated Google quotes, Alpha Vantage news, Finnhub data, and public
publisher links. Price quotes refresh after 5 minutes, news after 30–60 minutes,
and historical price series after 6 hours. If a provider is temporarily
unavailable, Delve can fall back to its most recent cached response.

Preload all three history periods for the nine demonstration symbols with:

```bash
python3 delve.py --seed-cache
```

The demonstration list contains `MSFT`, `SOXL`, `AAPL`, `FIG`, `NVDA`, `AMZN`,
`GOOGL`, `META`, and `TSLA`. `SOXL` is a leveraged exchange-traded fund rather
than an individual company stock. Cache counts and refresh timestamps are
available locally at [http://127.0.0.1:8000/api/cache](http://127.0.0.1:8000/api/cache).
The generated database is intentionally excluded from Git.

## Optional: API keys

Delve works without API keys for price history and public news links. To add
provider-backed news and sentiment data, copy `.env.example` to `.env` and add
your own free-tier keys:

```bash
cp .env.example .env
```

Then fill in `ALPHA_VANTAGE_API_KEY` and/or `FINNHUB_API_KEY`. Keep `.env`
private; it is intentionally excluded from version control. Keep your keys safe.

## What you'll see

- Historical daily closing prices from Yahoo Finance.
- A projected 30-trading-day price path and a 90% uncertainty range.
- A Yahoo Finance and Google Finance price comparison, with an alert when the
  difference needs a manual check.
- Recent news, available public-article summaries, and linked publisher
  commentary.
- Optional Alpha Vantage and Finnhub news data when their API keys are set (reccomended).
- Remember: This is a learning tool.

## A note on the projection

The projection uses the stock's recent daily returns and volatility. Its trend
estimate is deliberately pulled toward zero so a short-lived move is not
extended too confidently. Markets are uncertain: the chart is an educational
estimate, not a price target, investment recommendation, or financial advice.
