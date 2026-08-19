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

## Optional news API keys

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
- Optional Alpha Vantage and Finnhub news data when their API keys are set.

## A note on the projection

The projection uses the stock's recent daily returns and volatility. Its trend
estimate is deliberately pulled toward zero so a short-lived move is not
extended too confidently. Markets are uncertain: the chart is an educational
estimate, not a price target, investment recommendation, or financial advice.
