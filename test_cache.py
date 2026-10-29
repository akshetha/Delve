import os
import sqlite3
import tempfile
import unittest

import delve


class CacheTests(unittest.TestCase):
    # Prepare database
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database_path = delve.DB_PATH
        delve.DB_PATH = os.path.join(self.temporary_directory.name, "test.sqlite3")
        delve.initialize_database()

    # Restore database
    def tearDown(self) -> None:
        delve.DB_PATH = self.original_database_path
        self.temporary_directory.cleanup()

    # Store payload
    def test_round_trip_and_demo_symbols(self) -> None:
        payload = {"ticker": "MSFT", "prices": [1.0, 2.0]}
        delve.write_cache("yahoo_history", "MSFT:1y", payload)

        self.assertEqual(
            delve.read_cache("yahoo_history", "MSFT:1y", 60),
            payload,
        )
        overview = delve.cache_overview()
        self.assertEqual(
            [row["symbol"] for row in overview["demo_symbols"]],
            [symbol for symbol, _ in delve.DEMO_SYMBOLS],
        )

    # Expire payload
    def test_expired_entries_are_only_available_as_stale(self) -> None:
        delve.write_cache("yahoo_history", "AAPL:1y", {"value": "cached"})
        with sqlite3.connect(delve.DB_PATH) as connection:
            connection.execute(
                """
                UPDATE cache_entries
                SET updated_at = 1
                WHERE namespace = ? AND cache_key = ?
                """,
                ("yahoo_history", "AAPL:1y"),
            )

        self.assertIsNone(delve.read_cache("yahoo_history", "AAPL:1y", 60))
        self.assertEqual(
            delve.read_cache("yahoo_history", "AAPL:1y", None),
            {"value": "cached"},
        )

    # Reuse payload
    def test_decorator_avoids_repeat_provider_calls(self) -> None:
        calls = []

        @delve.cached_json("yahoo_history", lambda ticker: ticker)
        def provider(ticker: str) -> dict[str, str]:
            calls.append(ticker)
            return {"ticker": ticker}

        self.assertEqual(provider("FIG"), {"ticker": "FIG"})
        self.assertEqual(provider("FIG"), {"ticker": "FIG"})
        self.assertEqual(calls, ["FIG"])

    # Fall back
    def test_decorator_returns_stale_data_on_provider_failure(self) -> None:
        delve.write_cache("yahoo_history", "TSLA", {"ticker": "TSLA"})
        with sqlite3.connect(delve.DB_PATH) as connection:
            connection.execute("UPDATE cache_entries SET updated_at = 1")

        @delve.cached_json("yahoo_history", lambda ticker: ticker)
        def unavailable_provider(ticker: str) -> dict[str, str]:
            raise RuntimeError("provider unavailable")

        self.assertEqual(unavailable_provider("TSLA"), {"ticker": "TSLA"})


if __name__ == "__main__":
    unittest.main()
