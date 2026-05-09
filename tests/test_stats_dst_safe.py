import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from tests._async_compat import run_async


def _unlink_sqlite_files(db_path: Path) -> None:
    db_path.unlink(missing_ok=True)
    db_path.with_name(db_path.name + "-wal").unlink(missing_ok=True)
    db_path.with_name(db_path.name + "-shm").unlink(missing_ok=True)


class StatsDstSafeTests(unittest.TestCase):
    def test_hourly_buckets_remain_unique_across_dst_fold(self):
        db_path = Path("db/test_stats_dst_safe.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            db = TokensUsageDB(db_filename=db_path.name)
            berlin = ZoneInfo("Europe/Berlin")
            repeated_local_hour = [
                datetime(2021, 10, 31, 2, 30, tzinfo=berlin, fold=0),
                datetime(2021, 10, 31, 2, 30, tzinfo=berlin, fold=1),
            ]
            utc_events = [local_time.astimezone(timezone.utc) for local_time in repeated_local_hour]

            with sqlite3.connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO tokens_usage
                    (timestamp, prompt_tokens, completion_tokens, total_tokens,
                     gateway_model, operation, provider, model)
                    VALUES (?, 0, 0, ?, 'gateway', 'chat', 'provider', 'model')
                    """,
                    [(event.isoformat(), index + 1) for index, event in enumerate(utc_events)],
                )
                conn.commit()

            rows = run_async(
                db.get_aggregated_usage(
                    "hour",
                    start_date=datetime(2021, 10, 31, 0, 0, tzinfo=timezone.utc),
                    end_date=datetime(2021, 10, 31, 2, 0, tzinfo=timezone.utc),
                )
            )

            self.assertEqual(
                {
                    (row["time_period"], row["total_tokens"], row["count"])
                    for row in rows
                },
                {
                    ("2021-10-31 00:00:00", 1, 1),
                    ("2021-10-31 01:00:00", 2, 1),
                },
            )
        finally:
            _unlink_sqlite_files(db_path)


if __name__ == "__main__":
    unittest.main()
