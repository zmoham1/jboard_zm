import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src.database import Database


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class SourceRunRetentionTests(unittest.TestCase):
    """source_runs had no retention and grew to 78 MiB of the boards database,
    pushing it past the 95 MiB commit guard so sweep results were discarded."""

    def setUp(self) -> None:
        self.path = os.path.join(tempfile.mkdtemp(), "runs.db")
        self.db = Database(self.path)

    def tearDown(self) -> None:
        self.db.close()

    def _run(self, source_key: str, days_ago: float, entity_type: str = "board") -> None:
        self.db.record_source_run(
            source_key=source_key, entity_type=entity_type, mode="boards",
            platform="greenhouse", company=source_key, url="https://example.com",
            status="success", started_at=_ts(days_ago), finished_at=_ts(days_ago),
            latency_ms=10, fetched_count=1, matched_count=0, new_count=0,
            yes_count=0, maybe_count=0, stale_count=0, jd_coverage=0.0, error_text="",
        )

    def _count(self) -> int:
        conn = sqlite3.connect(self.path)
        n = conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0]
        conn.close()
        return n

    def test_deletes_history_older_than_the_window(self) -> None:
        for d in (10, 8, 5, 4):
            self._run("board-a", d)
        self._run("board-a", 0.1)
        self.assertEqual(self._count(), 5)

        self.db.prune_source_runs(days=3)
        # Only the recent run survives; the four stale ones go.
        self.assertEqual(self._count(), 1)

    def test_keeps_recent_history(self) -> None:
        for d in (2.5, 1.5, 0.5):
            self._run("board-a", d)
        self.db.prune_source_runs(days=3)
        self.assertEqual(self._count(), 3)

    def test_always_keeps_the_newest_run_per_source(self) -> None:
        # get_latest_source_run drives _should_alert_on_run. If a source's only
        # row is deleted, its next failure looks like a first-ever failure and
        # re-alerts, so the newest row must survive regardless of age.
        self._run("stale-board", 30)
        self._run("stale-board", 20)

        self.db.prune_source_runs(days=3)

        latest = self.db.get_latest_source_run("stale-board", "board")
        self.assertIsNotNone(latest, "newest run per source must survive pruning")
        self.assertEqual(self._count(), 1)

    def test_keeps_newest_per_entity_type_independently(self) -> None:
        self._run("shared-key", 30, entity_type="board")
        self._run("shared-key", 30, entity_type="main")
        self.db.prune_source_runs(days=3)
        self.assertIsNotNone(self.db.get_latest_source_run("shared-key", "board"))
        self.assertIsNotNone(self.db.get_latest_source_run("shared-key", "main"))

    def test_returns_zero_when_nothing_to_prune(self) -> None:
        self._run("board-a", 0.5)
        self.assertEqual(self.db.prune_source_runs(days=3), 0)


if __name__ == "__main__":
    unittest.main()
