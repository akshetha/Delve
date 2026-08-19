"""Delve stock research dashboard.

Run with ``python3 delve.py`` and visit http://localhost:8000.

The app fetches public Yahoo Finance market data for the selected ticker. Its
30-trading-day projection uses geometric Brownian motion (GBM), a deliberately
shrunk historical drift estimate, and the stock's observed return volatility.
Makes educational estimate, not for financial advice.
"""

from __future__ import annotations

import json
import math
import os
import re
from mimetypes import guess_type
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from xml.etree import ElementTree

import numpy as np
import requests


HOST = "127.0.0.1"
PORT = 8000
USER_AGENT = "Mozilla/5.0 (compatible; StockProjectionDashboard/1.0)"


def load_local_env() -> None:
    """Load local API configuration without adding a third-party dependency."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        with open(env_path, encoding="utf-8") as env_file:
            for line in env_file:
                key, separator, value = line.strip().partition("=")
                if separator and key and not key.startswith("#"):
                    os.environ.setdefault(key, value)
    except FileNotFoundError:
        pass


load_local_env()
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")


def usable_headline(value: object) -> str | None:
    """Return a displayable headline, excluding provider template markers."""
    title = str(value or "").strip()
    if not title or "META_TITLE_QUOTE" in title.upper() or title.upper() in {"UNTITLED ARTICLE", "UNTITLED HEADLINE"}:
        return None
    return title


def yahoo_chart(ticker: str, period: str) -> tuple[str, list[dict[str, float | str]], dict[str, object]]:
    """Fetch historical daily closes from Yahoo Finance's public chart feed."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker)}"
    response = requests.get(
        url,
        params={"range": period, "interval": "1d", "events": "history"},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    meta = result.get("meta", {})
    timestamps = result.get("timestamp", [])
    closes = result["indicators"]["quote"][0].get("close", [])
    prices = [
        {"date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"), "close": round(close, 2)}
        for ts, close in zip(timestamps, closes)
        if close is not None and close > 0
    ]
    if len(prices) < 25:
        raise ValueError("Not enough price history is available for a projection.")
    return meta.get("longName") or meta.get("shortName") or ticker.upper(), prices, meta


def yahoo_structured_quote(meta: dict[str, object]) -> dict[str, float | str | int | None]:
    """Return the ticker-scoped regular-market quote from Yahoo chart metadata.

    The old HTML parser found the first `regularMarketPrice` string anywhere in
    Yahoo's client payload. That payload can include related instruments, which
    explains implausible cross-source gaps. Chart metadata is returned for the
    requested symbol itself, so no broad HTML regex is needed.
    """
    price = meta.get("regularMarketPrice")
    return {
        "market_price": float(price) if isinstance(price, (float, int)) and price > 0 else None,
        "source": "Yahoo Finance structured regular-market quote",
        "market_time": meta.get("regularMarketTime"),
        "market_state": meta.get("marketState") or "regular market",
        "currency": meta.get("currency") or "USD",
    }


def google_finance_quote(ticker: str, yahoo_exchange: str) -> dict[str, float | str | bool | None]:
    """Scrape the visible last-price attribute from Google Finance.

    The request is exchange-qualified using Yahoo's exchange metadata. The
    page title must also contain the requested ticker, otherwise it is treated
    as a symbol mismatch rather than a valid comparison.
    """
    exchange_name = yahoo_exchange.lower()
    if "nasdaq" in exchange_name or yahoo_exchange in {"NMS", "NGM", "NCM"}:
        exchange = "NASDAQ"
    elif "nyse" in exchange_name or yahoo_exchange in {"NYQ", "NYS"}:
        exchange = "NYSE"
    else:
        return {"market_price": None, "source": "Google Finance exchange not mapped", "validated": False}
    try:
        response = requests.get(
            f"https://www.google.com/finance/quote/{quote(ticker)}:{exchange}",
            params={"hl": "en"},
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            timeout=12,
        )
        response.raise_for_status()
        if not re.search(rf"\({re.escape(ticker)}\)", response.text) or f"{ticker}:{exchange}" not in response.url.upper():
            return {"market_price": None, "source": "Google Finance symbol mismatch", "validated": False}
        # Scope the match to the security's name-and-price area. A generic
        # first-price selector can accidentally capture a related instrument.
        match = re.search(
            r'class="gO24Ff">.*?</div>.*?class="N6SYTe".*?<span[^>]*>[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*</span>',
            response.text,
            re.DOTALL,
        )
        if match:
            return {
                "market_price": float(match.group(1).replace(",", "")),
                "source": f"Google Finance ({exchange}; ticker validated)",
                "validated": True,
                "session": "Google Finance displayed quote (session timestamp unavailable)",
            }
    except (requests.RequestException, ValueError):
        pass
    return {"market_price": None, "source": "Google Finance price unavailable", "validated": False}


def alpha_vantage_news(ticker: str, company_name: str = "", limit: int = 8) -> dict[str, object]:
    """Read Alpha Vantage's ticker-scored market-news feed."""
    ticker = ticker.upper()
    if not ALPHA_VANTAGE_API_KEY:
        return {"items": [], "status": "API key not configured", "source": "Alpha Vantage"}
    try:
        response = requests.get(
            "https://www.alphavantage.co/query",
            params={"function": "NEWS_SENTIMENT", "tickers": ticker, "sort": "RELEVANCE", "limit": limit, "apikey": ALPHA_VANTAGE_API_KEY},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("Note") or data.get("Information") or data.get("Error Message"):
            return {"items": [], "status": str(data.get("Note") or data.get("Information") or data.get("Error Message")), "source": "Alpha Vantage"}
        items = []
        company_terms = [ticker]
        company_terms.extend(
            word for word in re.findall(r"[A-Za-z]{4,}", company_name.upper())
            if word not in {"INC", "INCORPORATED", "CORPORATION", "COMPANY", "LIMITED", "GROUP", "HOLDINGS"}
        )
        for article in data.get("feed", []):
            per_ticker = next(
                (row for row in article.get("ticker_sentiment", []) if str(row.get("ticker", "")).upper() == ticker),
                None,
            )
            title = usable_headline(article.get("title"))
            # Alpha Vantage can include broad-market articles in its feed.
            # Only render articles explicitly scored for the requested symbol.
            # Provider summaries can carry broad market metadata. Use the
            # visible headline as the relevance gate so the dashboard does
            # not show an unrelated company just because its summary tags it.
            headline_text = (title or "").upper()
            if per_ticker is None or title is None or not any(term in headline_text for term in company_terms):
                continue
            items.append({
                "title": title, "link": article.get("url", "#"),
                "source": article.get("source", "Alpha Vantage"), "published": article.get("time_published", ""),
                "summary": article.get("summary", ""), "score": float(per_ticker.get("ticker_sentiment_score", 0)),
                "label": per_ticker.get("ticker_sentiment_label", "Neutral"),
                "relevance": float(per_ticker.get("relevance_score", 0)),
            })
            if len(items) >= limit:
                break
        return {"items": items, "status": "ok", "source": "Alpha Vantage News & Sentiment"}
    except (requests.RequestException, ValueError):
        return {"items": [], "status": "API request unavailable", "source": "Alpha Vantage"}


def finnhub_data(ticker: str, limit: int = 8) -> dict[str, object]:
    """Read Finnhub company news and report aggregate-sentiment availability.

    ``/news-sentiment`` is the correct Finnhub endpoint, but access depends on
    the account's subscription. Keep company news usable when that optional
    endpoint returns an authorization error.
    """
    if not FINNHUB_API_KEY:
        return {"items": [], "aggregate": {}, "news_status": "API key not configured", "sentiment_status": "API key not configured", "source": "Finnhub"}
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=14)
    try:
        news_response = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": ticker, "from": start.isoformat(), "to": end.isoformat(), "token": FINNHUB_API_KEY},
            headers={"User-Agent": USER_AGENT}, timeout=15,
        )
        if not news_response.ok:
            return {"items": [], "aggregate": {}, "news_status": f"Finnhub company news HTTP {news_response.status_code}", "sentiment_status": "Not requested because company news was unavailable", "source": "Finnhub"}
        articles = news_response.json()
        items = []
        for row in articles if isinstance(articles, list) else []:
            title = usable_headline(row.get("headline"))
            if title is None:
                continue
            items.append({"title": title, "link": row.get("url", "#"), "source": row.get("source", "Finnhub"), "published": row.get("datetime", ""), "summary": row.get("summary", "")})
            if len(items) >= limit:
                break
        sentiment_response = requests.get(
            "https://finnhub.io/api/v1/news-sentiment", params={"symbol": ticker, "token": FINNHUB_API_KEY}, headers={"User-Agent": USER_AGENT}, timeout=15,
        )
        sentiment_payload = sentiment_response.json() if sentiment_response.content else {}
        if sentiment_response.ok and isinstance(sentiment_payload, dict):
            sentiment_status = "ok"
            aggregate = sentiment_payload
        else:
            provider_error = sentiment_payload.get("error") if isinstance(sentiment_payload, dict) else None
            if sentiment_response.status_code in {401, 403}:
                sentiment_status = "Finnhub sentiment requires a paid plan — not included"
            else:
                sentiment_status = f"Finnhub aggregate sentiment is temporarily unavailable" + (f" ({provider_error})" if provider_error else "")
            aggregate = {}
        return {"items": items, "aggregate": aggregate, "news_status": "ok", "sentiment_status": sentiment_status, "source": "Finnhub company news"}
    except (requests.RequestException, ValueError):
        return {"items": [], "aggregate": {}, "news_status": "API request unavailable", "sentiment_status": "API request unavailable", "source": "Finnhub"}


def provider_summary(alpha: dict[str, object], finnhub: dict[str, object]) -> str:
    """Summarize provider-supplied scores, never classify raw user text."""
    alpha_items = alpha.get("items", [])
    finnhub_aggregate = finnhub.get("aggregate", {})
    if not alpha_items and not finnhub.get("items"):
        return "No provider-backed news items are currently available. Check API key configuration or free-tier rate limits; no sentiment conclusion is shown."
    alpha_score = sum(float(item.get("score", 0)) * max(float(item.get("relevance", 0)), 0.1) for item in alpha_items) / sum(max(float(item.get("relevance", 0)), 0.1) for item in alpha_items) if alpha_items else 0.0
    alpha_view = "positive" if alpha_score > 0.15 else "negative" if alpha_score < -0.15 else "mixed/neutral"
    sentiment = finnhub_aggregate.get("sentiment", {}) if isinstance(finnhub_aggregate, dict) else {}
    bullish = sentiment.get("bullishPercent")
    bearish = sentiment.get("bearishPercent")
    finnhub_view = f"Finnhub reports {bullish}% bullish versus {bearish}% bearish coverage" if bullish is not None and bearish is not None else f"Finnhub aggregate sentiment is unavailable ({finnhub.get('sentiment_status', 'no provider status')})"
    return f"Alpha Vantage's relevance-weighted article scores are {alpha_view} across {len(alpha_items)} items. {finnhub_view}. This combines provider-supplied news signals, not raw keyword matching, and is not financial advice."


def news_feed(ticker: str, limit: int = 6) -> dict[str, object]:
    """Fetch recent, linked Google News RSS headlines for the ticker."""
    try:
        response = requests.get(
            "https://news.google.com/rss/search",
            params={"q": f"{ticker} stock", "hl": "en-US", "gl": "US", "ceid": "US:en"},
            headers={"User-Agent": USER_AGENT},
            timeout=12,
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        items = []
        for item in root.findall("./channel/item")[:limit]:
            title = usable_headline(item.findtext("title"))
            if title is None:
                continue
            items.append(
                {
                    "title": title,
                    "link": item.findtext("link", "#"),
                    "source": item.findtext("source", "Google News"),
                    "published": item.findtext("pubDate", ""),
                }
            )
        return {"items": items, "source": "Google News RSS"}
    except (requests.RequestException, ElementTree.ParseError):
        return {"items": [], "source": "Google News RSS"}


def publisher_commentary_feed(ticker: str, limit_per_source: int = 3) -> dict[str, object]:
    """Link publicly indexed publisher coverage without scraping social posts."""
    publishers = {
        "MarketWatch": "marketwatch.com",
        "Yahoo Finance": "finance.yahoo.com",
        "Benzinga": "benzinga.com",
        "Seeking Alpha": "seekingalpha.com",
    }
    def fetch_publisher(publisher: str, domain: str) -> list[dict[str, str]]:
        try:
            response = requests.get("https://news.google.com/rss/search", params={"q": f"{ticker} stock site:{domain}", "hl": "en-US", "gl": "US", "ceid": "US:en"}, headers={"User-Agent": USER_AGENT}, timeout=12)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            items = []
            for item in root.findall("./channel/item"):
                title = usable_headline(item.findtext("title"))
                if title is None:
                    continue
                items.append({"title": title, "link": item.findtext("link", "#"), "source": item.findtext("source", publisher), "publisher": publisher})
                if len(items) >= limit_per_source:
                    break
            return items
        except (requests.RequestException, ElementTree.ParseError):
            return []
    with ThreadPoolExecutor(max_workers=len(publishers)) as executor:
        futures = [executor.submit(fetch_publisher, publisher, domain) for publisher, domain in publishers.items()]
        items = [item for future in futures for item in future.result()]
    return {"items": items, "source": "Publisher links via Google News RSS", "publishers": list(publishers)}


def contextual_analysis(news: dict[str, object], finnhub: dict[str, object], commentary: dict[str, object]) -> list[str]:
    """Summarize source availability without labeling text as positive/negative."""
    news_items = news.get("items", [])
    finnhub_items = finnhub.get("items", [])
    commentary_items = commentary.get("items", [])
    return [
        f"Alpha Vantage: {len(news_items)} ticker-scored news items returned.",
        f"Finnhub: {len(finnhub_items)} company-news items returned; aggregate sentiment status: {finnhub.get('sentiment_status', 'unknown')}.",
        f"Public publisher commentary: {len(commentary_items)} linked articles across MarketWatch, Yahoo Finance, Benzinga, and Seeking Alpha when indexed.",
        "Check out these articles for yourself!",
    ]


def make_projection(prices: list[dict[str, float | str]], days: int = 30) -> dict[str, object]:
    """Forecast 30 trading days with shrunk-drift GBM and empirical volatility.

    Daily log-return drift is highly noisy. We therefore use a zero-centered
    ridge/empirical-Bayes shrinkage prior equivalent to 252 daily observations:
    the historical mean is pulled halfway toward zero when a full trading year
    is available. This allows a modest, data-driven 30-day slope without
    treating a recent run as a certain continuing trend. The 90% interval uses
    the sample standard deviation of the ticker's actual daily log returns.
    """
    window = min(252, len(prices))
    closes = np.array([float(row["close"]) for row in prices[-window:]])
    log_returns = np.diff(np.log(closes))
    daily_volatility = float(np.std(log_returns, ddof=1))
    if not math.isfinite(daily_volatility) or daily_volatility <= 0:
        raise ValueError("The price history does not contain enough variation for a volatility forecast.")

    latest_close = float(closes[-1])
    raw_log_drift = float(np.mean(log_returns))
    prior_observations = 252
    shrunken_log_drift = raw_log_drift * len(log_returns) / (len(log_returns) + prior_observations)
    # GBM's arithmetic drift converts the estimated log-return drift into the
    # expected-price path while retaining a log-normal forecast distribution.
    arithmetic_drift = shrunken_log_drift + 0.5 * daily_volatility**2
    z_score = 1.645  # central 90% interval under the log-normal GBM model
    start_date = datetime.strptime(str(prices[-1]["date"]), "%Y-%m-%d").date()
    forecast = [{"date": start_date.isoformat(), "price": round(latest_close, 2), "low": round(latest_close, 2), "high": round(latest_close, 2)}]
    date = start_date
    for trading_day in range(1, days + 1):
        date += timedelta(days=1)
        while date.weekday() >= 5:
            date += timedelta(days=1)
        # GBM quantiles widen at the empirical sigma * sqrt(time).
        spread = daily_volatility * math.sqrt(trading_day)
        expected_price = latest_close * math.exp(arithmetic_drift * trading_day)
        forecast.append(
            {
                "date": date.isoformat(),
                "price": round(expected_price, 2),
                "low": round(latest_close * math.exp(shrunken_log_drift * trading_day - z_score * spread), 2),
                "high": round(latest_close * math.exp(shrunken_log_drift * trading_day + z_score * spread), 2),
            }
        )
    return {
        "forecast": forecast,
        "annualized_volatility": round(daily_volatility * math.sqrt(252) * 100, 1),
        "volatility_observations": len(log_returns),
        "projected_return": round((forecast[-1]["price"] / latest_close - 1) * 100, 2),
        "raw_annualized_drift": round(raw_log_drift * 252 * 100, 1),
        "shrunken_annualized_drift": round(shrunken_log_drift * 252 * 100, 1),
    }


def dashboard_data(ticker: str, period: str, days: int) -> dict[str, object]:
    ticker = ticker.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.^-]{1,15}", ticker):
        raise ValueError("Enter a valid ticker symbol, such as AAPL, MSFT, TSLA, or ^GSPC.")
    name, prices, yahoo_meta = yahoo_chart(ticker, period)
    projection = make_projection(prices, days)
    # Secondary data sources are intentionally non-blocking: a temporary
    # source outage should never prevent the historical chart from rendering.
    yahoo_quote = yahoo_structured_quote(yahoo_meta)
    with ThreadPoolExecutor(max_workers=5) as executor:
        google_future = executor.submit(google_finance_quote, ticker, str(yahoo_meta.get("fullExchangeName") or yahoo_meta.get("exchangeName") or ""))
        alpha_future = executor.submit(alpha_vantage_news, ticker, name)
        finnhub_future = executor.submit(finnhub_data, ticker)
        commentary_future = executor.submit(publisher_commentary_feed, ticker)
        google_quote = google_future.result()
        alpha = alpha_future.result()
        finnhub = finnhub_future.result()
        commentary = commentary_future.result()
    provider_text = provider_summary(alpha, finnhub)
    # The existing summary panel consumes these fields; the text now comes
    # from API-provided scores and aggregate metrics, not keyword matching.
    alpha["label"] = provider_text
    alpha["score"] = 0
    yahoo_price = yahoo_quote["market_price"]
    google_price = google_quote["market_price"]
    difference = None
    validation = {"status": "unavailable", "message": "A validated real-time comparison is not available."}
    if google_quote.get("validated") and isinstance(google_price, float) and isinstance(yahoo_price, float):
        difference = round((google_price / float(yahoo_price) - 1) * 100, 2)
        if abs(difference) > 2:
            validation = {
                "status": "alert",
                "message": f"Quote validation alert: sources differ by {abs(difference):.2f}%, above the 2% threshold. Verify both quotes before relying on either.",
            }
        else:
            validation = {"status": "ok", "message": "Validated: sources are within the 2% comparison threshold."}
    elif not yahoo_price:
        validation = {"status": "unavailable", "message": "Yahoo regular-market quote is unavailable; comparison withheld."}
    elif not google_quote.get("validated"):
        validation = {"status": "unavailable", "message": "Google quote was not ticker/exchange validated; comparison withheld."}
    return {
        "ticker": ticker,
        "name": name,
        "history": prices,
        "last_close": prices[-1]["close"],
        "as_of": prices[-1]["date"],
        "price_sources": {
            "yahoo": yahoo_quote,
            "google": google_quote,
            "difference_percent": difference,
            "validation": validation,
        },
        "news": alpha,
        "article_summary": {"text": provider_text, "sources_read": ["Alpha Vantage", "Finnhub", "public publisher commentary"]},
        "finnhub": finnhub,
        "commentary": commentary,
        # Temporary view-model aliases keep the compact client renderer
        # backwards compatible while it presents Finnhub and publisher links.
        "marketwatch": finnhub,
        "stocktwits": {
            "label": "Linked articles",
            "items": [
                {
                    # The compact client renderer consumes these fields. The
                    # delimiter lets its publisher-link enhancement recover
                    # the original article URL without trusting article HTML.
                    "body": f"{item.get('title', 'Untitled article')}\u001f{item.get('link', '#')}",
                    "user": item.get("source", "Publisher"),
                    "sentiment": "Linked article",
                }
                for item in commentary.get("items", [])
            ],
        },
        "analysis": contextual_analysis(alpha, finnhub, commentary),
        **projection,
    }


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Delve — Stock Research Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box}body{margin:0;background:#07111f url('/assets/background.png') center/cover fixed no-repeat;color:#e7edf7;font-family:Inter,ui-sans-serif,system-ui,sans-serif}.shell{max-width:1150px;margin:auto;padding:42px 24px 60px}.eyebrow{color:#76d8b4;text-transform:uppercase;letter-spacing:.13em;font-size:.75rem;font-weight:700}h1{font-size:clamp(2rem,4vw,3.5rem);margin:.35rem 0 .5rem;letter-spacing:-.05em}.sub{color:#9fb0c9;max-width:680px;line-height:1.6}.controls{display:flex;flex-wrap:wrap;gap:12px;margin:28px 0}input,select,button,.horizon{font:inherit;border-radius:9px;padding:12px 14px;border:1px solid #2b3d56;background:#0c1a2c;color:#e7edf7}input{width:155px;font-weight:700;letter-spacing:.04em}.horizon{display:flex;align-items:center;color:#9fb0c9;font-weight:600}button{background:#76d8b4;color:#062018;border:0;font-weight:800;cursor:pointer;padding-inline:22px}button:hover{background:#9ee8cd}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:13px;margin:0 0 15px}.card,.chart-box,.insight{background:rgba(12,26,44,.94);border:1px solid #21334c;border-radius:14px}.card{padding:17px}.label{font-size:.76rem;color:#91a4c1;text-transform:uppercase;letter-spacing:.08em}.value{font-size:1.52rem;font-weight:750;margin-top:6px}.muted{font-size:.85rem;color:#9fb0c9;margin-top:5px}.chart-box{padding:20px;height:490px;background:rgba(12,26,44,.94);border:1px solid #21334c;border-radius:14px}.insights,.research-grid{display:grid;grid-template-columns:1fr 1.6fr;gap:13px;margin-top:15px}.insight{padding:20px}.insight h2{font-size:1.02rem;margin:0 0 13px}.insight h3{font-size:.92rem;margin:0;color:#cbd9ee}.research{margin-top:13px}.summary{margin:0;color:#dbe6f8;line-height:1.55;max-width:940px}.source-row{display:flex;justify-content:space-between;padding:10px 0;border-top:1px solid #21334c;font-size:.92rem}.source-row:first-of-type{border-top:0}.source-price{font-weight:750}.quote-alert{margin-top:12px;padding:10px;border-radius:8px;background:#3b1721;color:#ffb9c7;font-size:.84rem;line-height:1.4}.quote-ok{color:#8de6c4}.sentiment{display:inline-block;padding:4px 9px;border-radius:999px;background:#183d36;color:#8de6c4;font-size:.8rem;font-weight:700}.headlines{display:grid;gap:9px;margin-top:12px}.headline,.post{display:block;border-top:1px solid #21334c;padding-top:9px;color:#dbe6f8;text-decoration:none;font-size:.9rem;line-height:1.4}.headline:hover{color:#76d8b4}.headline small,.post small{display:block;color:#91a4c1;margin-top:3px}.analysis-list{margin:0 0 16px;padding-left:19px;color:#b8c8df;font-size:.9rem;line-height:1.55}.post{color:#dbe6f8}.tag{font-size:.72rem;padding:2px 6px;border-radius:5px;background:#22334c;color:#b9c9e4}.note{color:#91a4c1;font-size:.83rem;line-height:1.55;margin:16px 2px}.error{display:none;background:#3b1721;color:#ffb9c7;border:1px solid #713043;padding:14px;border-radius:10px;margin-bottom:16px}.loading{color:#9fb0c9;padding:35px;text-align:center}@media(max-width:680px){.shell{padding:30px 16px}.grid,.insights,.research-grid{grid-template-columns:1fr}.chart-box{height:390px}.controls>*{flex:1}}
</style><style>
body{color:#fff;text-shadow:0 1px 10px rgba(56,28,53,.2)}.shell{position:relative}.eyebrow{color:#fff;text-shadow:0 1px 8px rgba(56,28,53,.35)}h1{color:#3d2737;text-shadow:none}.sub{color:#fff;font-weight:500}.card,.chart-box,.insight{background:rgba(255,255,255,.22);border:1px solid rgba(255,255,255,.62);box-shadow:0 12px 32px rgba(88,39,73,.16);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px)}input,select,button,.horizon{background:rgba(255,255,255,.34);border-color:rgba(255,255,255,.7);color:#3d2737;box-shadow:0 5px 16px rgba(88,39,73,.1);text-shadow:none}input::placeholder{color:#604354}button{background:rgba(255,255,255,.82);color:#3d2737}button:hover{background:#fff}.label,.muted,.headline small,.post small,.note{color:#fff}.value,.insight h2,.insight h3,.summary,.headline,.post,.analysis-list{color:#fff}.source-row{border-color:rgba(255,255,255,.34)}.headline:hover{color:#fff;text-decoration:underline}.tag,.sentiment{background:rgba(255,255,255,.24)!important;color:#fff!important}.quote-alert{background:rgba(99,29,55,.54);border:1px solid rgba(255,255,255,.4);color:#fff}.quote-ok{color:#fff}.error{background:rgba(99,29,55,.72);border-color:rgba(255,255,255,.45);color:#fff}.loading{color:#fff}#return{color:#fff!important}.chart-box canvas{filter:grayscale(1) brightness(1.65)}
</style></head><body><main class="shell"><div class="eyebrow">Your market research tool</div><h1>Delve</h1><p class="sub">Gathers recent market data and turns its current trend into a clear price projection. Use it to explore ideas. Learn to your heart's content!</p>
<form class="controls" id="form"><input id="ticker" value="AAPL" aria-label="Ticker symbol" maxlength="15"><select id="period" aria-label="History period"><option value="6mo">6 months of history</option><option value="1y" selected>1 year of history</option><option value="2y">2 years of history</option></select><div class="horizon" aria-label="Projection horizon">30 trading-day projection</div><button>Analyze stock</button></form>
<div id="error" class="error"></div><section id="results" hidden><div class="grid"><div class="card"><div class="label">Latest close</div><div class="value" id="close">—</div><div class="muted" id="asof">—</div></div><div class="card"><div class="label">30-day expected return</div><div class="value" id="return">—</div><div class="muted" id="drift">Volatility-shrunk historical drift</div></div><div class="card"><div class="label">Annualized volatility</div><div class="value" id="volatility">—</div><div class="muted" id="source">—</div></div></div><div class="chart-box"><canvas id="chart"></canvas></div><section class="insight research"><h2>Reported-sentiment summary</h2><p id="article-summary" class="summary">Reading public article text…</p><div id="articles-read" class="muted"></div></section><div class="insights"><section class="insight"><h2>Price-source check</h2><div class="source-row"><span>Yahoo Finance</span><span id="yahoo-price" class="source-price">—</span></div><div class="source-row"><span>Google Finance</span><span id="google-price" class="source-price">—</span></div><div id="price-difference" class="muted">Comparing sources…</div><div id="quote-validation" class="muted" role="status"></div></section><section class="insight"><h2>Recent news sources</h2><div id="headlines" class="headlines"></div><div id="news-empty" class="muted" hidden>No current headlines available from Google News RSS.</div></section></div><section class="insight research"><h2>Cross-source research</h2><ul id="analysis" class="analysis-list"></ul><div class="research-grid"><div><h3>MarketWatch reporting</h3><div id="marketwatch" class="headlines"></div><div id="marketwatch-empty" class="muted" hidden>No recent MarketWatch headlines found.</div></div><div><h3>StockTwits community <span id="stocktwits-sentiment" class="sentiment">—</span></h3><div id="stocktwits" class="headlines"></div><div id="stocktwits-empty" class="muted" hidden>No public StockTwits posts available.</div></div></div></section><p class="note">Projection model: 30-trading-day geometric Brownian motion. The midpoint uses the stock's average daily log return from up to 252 sessions, shrunk toward zero with a zero-return prior equal to 252 observations; this keeps the forecast responsive but avoids extending a recent move unchecked. The central 90% interval is calculated from the stock's actual daily return volatility and widens with √time. The reported-sentiment summary is generated only from public full text that can be read from the linked articles; it is not financial advice. StockTwits labels are supplied by post authors. Historical data: Yahoo Finance structured regular-market quote; comparison quote: Google Finance only after ticker/exchange validation. A gap above 2% triggers a validation alert.</p></section><div id="loading" class="loading" aria-live="polite">Loading market data…</div></main>
<script>
const themeOverrides=document.createElement('style');themeOverrides.textContent="#sentiment{display:block!important;background:rgba(255,255,255,.14)!important;color:#fff!important;border:1px solid rgba(255,255,255,.34);border-radius:12px;padding:16px!important;line-height:1.55}.headline,.post{border-color:rgba(255,255,255,.42)!important}.show-more{margin-top:13px;padding:8px 13px;border:1px solid rgba(255,255,255,.58);border-radius:999px;background:rgba(255,255,255,.2);color:#fff;font:inherit;font-size:.84rem;font-weight:700;cursor:pointer}.show-more:hover{background:rgba(255,255,255,.34)}";document.head.append(themeOverrides);
themeOverrides.textContent+='@keyframes floatBubble{0%,100%{transform:translate3d(0,0,0) rotate(0deg)}50%{transform:translate3d(4vw,-5vh,0) rotate(8deg)}}.bubbles{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none}.bubble{position:absolute;width:clamp(150px,22vw,340px);aspect-ratio:1;background:center/contain no-repeat;opacity:.35;filter:drop-shadow(0 12px 20px rgba(116,50,87,.18));animation:floatBubble 12s ease-in-out infinite}.bubble-one{background-image:url("/assets/bubbles1.png");left:4%;top:20%;animation-delay:-2s}.bubble-two{background-image:url("/assets/bubbles2.png");right:6%;top:40%;width:clamp(120px,18vw,270px);animation-duration:15s;animation-delay:-7s}.bubble-three{background-image:url("/assets/bubbles3.png");left:28%;bottom:4%;width:clamp(100px,15vw,220px);animation-duration:18s;animation-delay:-11s}.shell{z-index:1}.headline,.post{max-height:18rem;overflow:hidden;opacity:1;transition:max-height .36s ease,opacity .22s ease,padding .3s ease,border-width .3s ease}[hidden]{display:block!important;max-height:0!important;opacity:0!important;padding-top:0!important;border-top-width:0!important;pointer-events:none!important}';
const bubbles=document.createElement('div');bubbles.className='bubbles';bubbles.setAttribute('aria-hidden','true');bubbles.innerHTML='<i class="bubble bubble-one"></i><i class="bubble bubble-two"></i><i class="bubble bubble-three"></i>';document.body.prepend(bubbles);
document.querySelector('.insight.research h2').textContent='Check This Out';
document.querySelector('#article-summary').id='sentiment';
document.querySelector('.insights .insight:nth-child(2) h2').textContent='Alpha Vantage scored news';
document.querySelector('.research-grid h3').textContent='Finnhub company news';
document.querySelectorAll('.research-grid h3')[1].firstChild.nodeValue='Publisher commentary ';
document.querySelector('.note').textContent=document.querySelector('.note').textContent.replace(' StockTwits labels are supplied by post authors.', ' Publisher commentary is sourced from publicly indexed articles; no Reddit or StockTwits posts are fetched.');
document.querySelector('.note').textContent="How Delve estimates: the chart uses a stock's recent price history to estimate what its next 30 trading days could look like. The shaded range shows how wide the outcome could be based on the stock's past ups and downs. News notes use only readable public articles, and publisher links come from public search results. Prices are checked against Yahoo Finance and Google Finance when possible. If they differ by more than 2%, Delve flags it for you to review. Keep in mind -> his is for research only, not financial advice.";
const headlineGroups=['#headlines','#marketwatch','#stocktwits'].map(selector=>document.querySelector(selector));
function limitHeadlines(group){const items=[...group.querySelectorAll(':scope > .headline,:scope > .post')],existingButton=group.querySelector(':scope > .show-more');if(items.length<=5){existingButton?.remove();return}const expanded=existingButton?.dataset.expanded==='true';items.forEach((item,index)=>{item.hidden=!expanded&&index>=5});let button=existingButton;if(!button){button=document.createElement('button');button.type='button';button.className='show-more';button.addEventListener('click',()=>{button.dataset.expanded=button.dataset.expanded==='true'?'false':'true';limitHeadlines(group)});group.append(button)}button.textContent=expanded?'Show less':`Show more (${items.length-5})`}
const headlineObserver=new MutationObserver(()=>headlineGroups.forEach(limitHeadlines));headlineGroups.forEach(group=>headlineObserver.observe(group,{childList:true}));
let chart;const money=v=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(v);const esc=v=>String(v).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
async function load(){const t=document.querySelector('#ticker').value.trim().toUpperCase(),p=document.querySelector('#period').value;document.querySelector('#error').style.display='none';document.querySelector('#results').hidden=true;document.querySelector('#loading').textContent=`Loading ${t||'selected ticker'} market data…`;document.querySelector('#loading').style.display='block';try{const r=await fetch(`/api/stock?ticker=${encodeURIComponent(t)}&period=${p}&days=30`),data=await r.json();if(!r.ok)throw new Error(data.error||'Unable to load data.');render(data)}catch(e){document.querySelector('#error').textContent=e.message;document.querySelector('#error').style.display='block'}finally{document.querySelector('#loading').style.display='none'}}
function render(d){document.querySelector('#results').hidden=false;document.querySelector('#close').textContent=money(d.last_close);document.querySelector('#asof').textContent=`Close on ${d.as_of}`;document.querySelector('#return').textContent=`${d.projected_return>=0?'+':''}${d.projected_return}%`;document.querySelector('#return').style.color=d.projected_return>=0?'#76d8b4':'#ff9eac';document.querySelector('#drift').textContent=`Shrunk annual drift: ${d.shrunken_annualized_drift>=0?'+':''}${d.shrunken_annualized_drift}%`;document.querySelector('#volatility').textContent=`${d.annualized_volatility}%`;document.querySelector('#source').textContent=`${d.volatility_observations} daily returns · ${d.ticker}`;const sources=d.price_sources,fmt=v=>typeof v==='number'?money(v):'Unavailable';document.querySelector('#yahoo-price').textContent=fmt(sources.yahoo.market_price);document.querySelector('#google-price').textContent=fmt(sources.google.market_price);document.querySelector('#price-difference').textContent=sources.difference_percent===null?`${sources.google.source}`:`Google is ${sources.difference_percent>=0?'+':''}${sources.difference_percent}% vs. Yahoo`;const validation=document.querySelector('#quote-validation');validation.textContent=sources.validation.message;validation.className=sources.validation.status==='alert'?'quote-alert':sources.validation.status==='ok'?'quote-ok muted':'muted';const sentiment=document.querySelector('#sentiment');sentiment.textContent=d.news.label;sentiment.style.background=d.news.score<0?'#491f29':d.news.score>0?'#183d36':'#22334c';sentiment.style.color=d.news.score<0?'#ffb9c7':d.news.score>0?'#8de6c4':'#b9c9e4';const headlines=document.querySelector('#headlines'),empty=document.querySelector('#news-empty');headlines.innerHTML=d.news.items.map(i=>{const href=/^https?:\/\//.test(i.link)?i.link:'#';return `<a class="headline" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(i.title)}<small>${esc(i.source)}</small></a>`}).join('');empty.hidden=d.news.items.length>0;document.querySelector('#analysis').innerHTML=d.analysis.map(item=>`<li>${esc(item)}</li>`).join('');const mw=document.querySelector('#marketwatch'),mwEmpty=document.querySelector('#marketwatch-empty');mw.innerHTML=d.marketwatch.items.map(i=>{const href=/^https?:\/\//.test(i.link)?i.link:'#';return `<a class="headline" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(i.title)}<small>${esc(i.source)}</small></a>`}).join('');mwEmpty.hidden=d.marketwatch.items.length>0;const st=document.querySelector('#stocktwits'),stEmpty=document.querySelector('#stocktwits-empty'),stLabel=document.querySelector('#stocktwits-sentiment');stLabel.textContent=d.stocktwits.label;stLabel.style.background=d.stocktwits.label==='Bearish'?'#491f29':d.stocktwits.label==='Bullish'?'#183d36':'#22334c';st.innerHTML=d.stocktwits.items.map(i=>`<div class="post">${esc(i.body)}<small>@${esc(i.user)} · <span class="tag">${esc(i.sentiment)}</span></small></div>`).join('');stEmpty.hidden=d.stocktwits.items.length>0;const labels=[...d.history.map(x=>x.date),...d.forecast.slice(1).map(x=>x.date)],hist=d.history.map(x=>x.close),pad=Array(d.history.length-1).fill(null),fc=[...pad,d.last_close,...d.forecast.slice(1).map(x=>x.price)],low=[...pad,d.last_close,...d.forecast.slice(1).map(x=>x.low)],high=[...pad,d.last_close,...d.forecast.slice(1).map(x=>x.high)];if(chart)chart.destroy();chart=new Chart(document.querySelector('#chart'),{type:'line',data:{labels,datasets:[{label:'Historical close',data:[...hist,...Array(labels.length-hist.length).fill(null)],borderColor:'#6da9ff',borderWidth:2,pointRadius:0,tension:.18},{label:'Expected price',data:fc,borderColor:'#76d8b4',borderWidth:2,borderDash:[7,5],pointRadius:0,tension:.18},{label:'Upper 90% bound',data:high,borderColor:'rgba(118,216,180,.35)',borderWidth:1,pointRadius:0,tension:.18,fill:false},{label:'Lower 90% bound',data:low,borderColor:'rgba(118,216,180,.35)',borderWidth:1,pointRadius:0,tension:.18,fill:'-1',backgroundColor:'rgba(118,216,180,.10)'}]},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{legend:{labels:{color:'#c7d3e6',usePointStyle:true}},title:{display:true,text:`${d.name} (${d.ticker}) · 30 trading days`,color:'#e7edf7',font:{size:17,weight:'600'}},tooltip:{callbacks:{label:c=>`${c.dataset.label}: ${money(c.parsed.y)}`}}},scales:{x:{ticks:{color:'#91a4c1',maxTicksLimit:9},grid:{color:'rgba(145,164,193,.08)'}},y:{ticks:{color:'#91a4c1',callback:v=>money(v)},grid:{color:'rgba(145,164,193,.10)'}}}}})}
document.querySelector('#form').addEventListener('submit',e=>{e.preventDefault();load()});load();
</script><script>
// Convert publisher-commentary rows into safe, direct outbound article links.
const publisherArticles=document.querySelector('#stocktwits');
new MutationObserver(()=>publisherArticles.querySelectorAll('.post').forEach(row=>{
  const [title,url]=row.firstChild?.textContent.split('\u001f')||[];
  if(!title||!/^https?:\/\//.test(url||''))return;
  const source=row.querySelector('small')?.textContent.replace(/^@/,'').split(' · ')[0]||'Publisher';
  const link=document.createElement('a');link.className='headline';link.href=url;link.target='_blank';link.rel='noopener noreferrer';link.textContent=title;
  const label=document.createElement('small');label.textContent=source;link.append(label);row.replaceChildren(link);
})).observe(publisherArticles,{childList:true});
</script></body></html>'''


class AppHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {fmt % args}")

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        request = urlparse(self.path)
        if request.path == "/":
            encoded = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        asset_names = {"/assets/background.png", "/assets/bubbles1.png", "/assets/bubbles2.png", "/assets/bubbles3.png"}
        if request.path in asset_names:
            asset_path = os.path.join(os.path.dirname(__file__), "assets", os.path.basename(request.path))
            try:
                with open(asset_path, "rb") as asset_file:
                    encoded = asset_file.read()
            except FileNotFoundError:
                self.send_error(404, "Background image not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", guess_type(asset_path)[0] or "image/png")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        if request.path == "/api/stock":
            params = parse_qs(request.query)
            ticker = params.get("ticker", [""])[0].strip().upper()
            try:
                period = params.get("period", ["1y"])[0]
                days = int(params.get("days", ["60"])[0])
                if period not in {"6mo", "1y", "2y"} or days != 30:
                    raise ValueError("This dashboard provides a fixed 30-trading-day projection.")
                self.send_json(200, dashboard_data(ticker, period, days))
            except (ValueError, KeyError, IndexError, requests.RequestException):
                stock_name = ticker or "that stock"
                self.send_json(400, {"error": f"Are you sure you've entered this in right? - {stock_name}"})
            return
        self.send_error(404, "Not found")


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"Delve is running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
