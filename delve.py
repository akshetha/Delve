from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import time
from functools import wraps
from mimetypes import guess_type
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import parse_qs, quote, urlparse
from xml.etree import ElementTree

import numpy as np
import requests


HOST = "127.0.0.1"
PORT = 8000
USER_AGENT = "Mozilla/5.0 (compatible; StockProjectionDashboard/1.0)"
DB_PATH = os.path.join(os.path.dirname(__file__), "delve_cache.sqlite3")
DEMO_SYMBOLS = (
    ("MSFT", "Microsoft"),
    ("SOXL", "Direxion Semiconductor Bull 3X ETF"),
    ("AAPL", "Apple"),
    ("FIG", "Figma"),
    ("NVDA", "NVIDIA"),
    ("AMZN", "Amazon"),
    ("GOOGL", "Alphabet"),
    ("META", "Meta Platforms"),
    ("TSLA", "Tesla"),
)
CACHE_TTLS = {
    "yahoo_history": 6 * 60 * 60,
    "google_quote": 5 * 60,
    "alpha_news": 30 * 60,
    "finnhub": 30 * 60,
    "publisher_commentary": 60 * 60,
}
_DATABASE_INIT_LOCK = Lock()
_INITIALIZED_DATABASES: set[str] = set()


def database_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    database_path = os.path.abspath(DB_PATH)
    if database_path in _INITIALIZED_DATABASES:
        return
    with _DATABASE_INIT_LOCK:
        if database_path in _INITIALIZED_DATABASES:
            return
        with database_connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    namespace TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (namespace, cache_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS demo_symbols (
                    symbol TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO demo_symbols (symbol, display_name, sort_order)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    display_name = excluded.display_name,
                    sort_order = excluded.sort_order
                """,
                [(symbol, name, index) for index, (symbol, name) in enumerate(DEMO_SYMBOLS)],
            )
        _INITIALIZED_DATABASES.add(database_path)


def read_cache(namespace: str, cache_key: str, max_age_seconds: int | None) -> object | None:
    initialize_database()
    with database_connection() as connection:
        row = connection.execute(
            """
            SELECT payload_json, updated_at
            FROM cache_entries
            WHERE namespace = ? AND cache_key = ?
            """,
            (namespace, cache_key),
        ).fetchone()
    if row is None:
        return None
    if max_age_seconds is not None and int(time.time()) - int(row["updated_at"]) > max_age_seconds:
        return None
    try:
        return json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        return None


def write_cache(namespace: str, cache_key: str, payload: object) -> None:
    initialize_database()
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO cache_entries (namespace, cache_key, payload_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(namespace, cache_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (namespace, cache_key, json.dumps(payload), int(time.time())),
        )


def cached_json(namespace: str, key_builder, should_cache=lambda result: True):
    def decorator(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            cache_key = str(key_builder(*args, **kwargs))
            fresh = read_cache(namespace, cache_key, CACHE_TTLS[namespace])
            if fresh is not None:
                return fresh
            stale = read_cache(namespace, cache_key, None)
            try:
                result = function(*args, **kwargs)
            except Exception:
                if stale is not None:
                    return stale
                raise
            if should_cache(result):
                write_cache(namespace, cache_key, result)
            elif stale is not None:
                return stale
            return result
        return wrapper
    return decorator


def load_local_env() -> None:
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
DB_PATH = os.getenv("DELVE_DB_PATH", DB_PATH)
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")


def usable_headline(value: object) -> str | None:
    title = str(value or "").strip()
    if not title or "META_TITLE_QUOTE" in title.upper() or title.upper() in {"UNTITLED ARTICLE", "UNTITLED HEADLINE"}:
        return None
    return title


@cached_json("yahoo_history", lambda ticker, period: f"{ticker.upper()}:{period}")
def yahoo_chart(ticker: str, period: str) -> tuple[str, list[dict[str, float | str]], dict[str, object]]:
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
    price = meta.get("regularMarketPrice")
    return {
        "market_price": float(price) if isinstance(price, (float, int)) and price > 0 else None,
        "source": "Yahoo Finance structured regular-market quote",
        "market_time": meta.get("regularMarketTime"),
        "market_state": meta.get("marketState") or "regular market",
        "currency": meta.get("currency") or "USD",
    }


@cached_json(
    "google_quote",
    lambda ticker, yahoo_exchange: f"{ticker.upper()}:{yahoo_exchange}",
    lambda result: isinstance(result, dict),
)
def google_finance_quote(ticker: str, yahoo_exchange: str) -> dict[str, float | str | bool | None]:
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


@cached_json(
    "alpha_news",
    lambda ticker, company_name="", limit=8: f"{ticker.upper()}:{limit}",
    lambda result: isinstance(result, dict),
)
def alpha_vantage_news(ticker: str, company_name: str = "", limit: int = 8) -> dict[str, object]:
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


@cached_json(
    "finnhub",
    lambda ticker, limit=8: f"{ticker.upper()}:{limit}",
    lambda result: isinstance(result, dict) and result.get("news_status") == "ok",
)
def finnhub_data(ticker: str, limit: int = 8) -> dict[str, object]:
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


@cached_json(
    "publisher_commentary",
    lambda ticker, limit_per_source=3: f"{ticker.upper()}:{limit_per_source}",
    lambda result: isinstance(result, dict),
)
def publisher_commentary_feed(ticker: str, limit_per_source: int = 3) -> dict[str, object]:
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

    arithmetic_drift = shrunken_log_drift + 0.5 * daily_volatility**2
    z_score = 1.645
    start_date = datetime.strptime(str(prices[-1]["date"]), "%Y-%m-%d").date()
    forecast = [{"date": start_date.isoformat(), "price": round(latest_close, 2), "low": round(latest_close, 2), "high": round(latest_close, 2)}]
    date = start_date
    for trading_day in range(1, days + 1):
        date += timedelta(days=1)
        while date.weekday() >= 5:
            date += timedelta(days=1)

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
        "marketwatch": finnhub,
        "stocktwits": {
            "label": "Linked articles",
            "items": [
                {
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


def cache_overview() -> dict[str, object]:
    initialize_database()
    with database_connection() as connection:
        cache_rows = connection.execute(
            """
            SELECT namespace, COUNT(*) AS entry_count, MAX(updated_at) AS newest_at
            FROM cache_entries
            GROUP BY namespace
            ORDER BY namespace
            """
        ).fetchall()
        symbol_rows = connection.execute(
            """
            SELECT symbol, display_name
            FROM demo_symbols
            ORDER BY sort_order
            """
        ).fetchall()
    return {
        "database": os.path.basename(DB_PATH),
        "demo_symbols": [dict(row) for row in symbol_rows],
        "cache": [
            {
                "namespace": row["namespace"],
                "entries": row["entry_count"],
                "newest_at": datetime.fromtimestamp(int(row["newest_at"]), tz=timezone.utc).isoformat(),
            }
            for row in cache_rows
        ],
    }


def seed_demo_cache(periods: tuple[str, ...] = ("6mo", "1y", "2y")) -> dict[str, object]:
    initialize_database()
    started_at = time.perf_counter()
    refreshed = []
    failures = []
    for symbol, _ in DEMO_SYMBOLS:
        for period in periods:
            try:
                dashboard_data(symbol, period, 30)
                refreshed.append({"symbol": symbol, "period": period})
            except (ValueError, KeyError, IndexError, requests.RequestException) as error:
                failures.append({"symbol": symbol, "period": period, "error": str(error)})
    return {
        "refreshed": refreshed,
        "failures": failures,
        "elapsed_seconds": round(time.perf_counter() - started_at, 2),
        "overview": cache_overview(),
    }




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

    def send_file(self, path: str, content_type: str | None = None) -> None:
        try:
            with open(path, "rb") as file:
                content = file.read()
        except FileNotFoundError:
            self.send_error(404, "Not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type or guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        request = urlparse(self.path)
        if request.path == "/":
            self.send_file(os.path.join(os.path.dirname(__file__), "templates", "index.html"), "text/html; charset=utf-8")
            return
        asset_names = {"/assets/background.png", "/assets/bubbles1.png", "/assets/bubbles2.png", "/assets/bubbles3.png"}
        if request.path in asset_names:
            asset_path = os.path.join(os.path.dirname(__file__), "assets", os.path.basename(request.path))
            self.send_file(asset_path)
            return
        if request.path in {"/static/styles.css", "/static/app.js"}:
            static_path = os.path.join(os.path.dirname(__file__), "static", os.path.basename(request.path))
            self.send_file(static_path)
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
        if request.path == "/api/cache":
            self.send_json(200, cache_overview())
            return
        self.send_error(404, "Not found")


if __name__ == "__main__":
    initialize_database()
    if "--seed-cache" in sys.argv:
        print(json.dumps(seed_demo_cache(), indent=2))
        raise SystemExit(0)
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"Delve is running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
