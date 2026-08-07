import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src.database import Database


class EmptyBoardBackoffTests(unittest.TestCase):
    """~300 boards belong to companies that left the ATS. A flat weekly retry
    re-fetched them forever — 27% of every sweep spent on boards that had
    returned nothing for months."""

    def test_cooldown_doubles_with_each_failure(self) -> None:
        self.assertEqual(Database.empty_board_cooldown_hours(3), 168)
        self.assertEqual(Database.empty_board_cooldown_hours(4), 336)
        self.assertEqual(Database.empty_board_cooldown_hours(5), 672)

    def test_cooldown_is_capped(self) -> None:
        # Never longer than a month, so a company that resumes hiring is found.
        for fail_count in (6, 10, 50, 5000):
            self.assertEqual(
                Database.empty_board_cooldown_hours(fail_count),
                Database.MAX_EMPTY_BOARD_COOLDOWN_HOURS,
                fail_count,
            )

    def test_large_fail_count_does_not_overflow(self) -> None:
        self.assertEqual(
            Database.empty_board_cooldown_hours(10**6),
            Database.MAX_EMPTY_BOARD_COOLDOWN_HOURS,
        )

    def test_boards_below_the_threshold_are_not_backed_off(self) -> None:
        self.assertEqual(Database.empty_board_cooldown_hours(0), 168)
        self.assertEqual(Database.empty_board_cooldown_hours(2), 168)


class SkipBoardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = os.path.join(tempfile.mkdtemp(), "boards.db")
        self.db = Database(self.path)

    def tearDown(self) -> None:
        self.db.close()

    def _board(self, board_id: str, *, fail_count: int, days_since_check: float,
               status: str = "degraded", reason: str = "0 jobs returned") -> None:
        now = datetime.now(timezone.utc).isoformat()
        checked = (datetime.now(timezone.utc) - timedelta(days=days_since_check)).isoformat()
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO boards(board_id,platform,company,url,status,last_checked,job_count,"
            "fail_count,fail_reason,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (board_id, "smartrecruiters", "Co", "https://x", status, checked, 0, fail_count, reason, now, now),
        )
        conn.commit()
        conn.close()

    def test_long_failing_board_is_skipped_for_a_month(self) -> None:
        self._board("stale", fail_count=10, days_since_check=14)
        # A flat weekly cooldown would have re-fetched this after 7 days.
        self.assertTrue(self.db.should_skip_board("stale"))

    def test_long_failing_board_is_retried_after_the_cap(self) -> None:
        self._board("stale", fail_count=10, days_since_check=31)
        self.assertFalse(self.db.should_skip_board("stale"),
                         "a board must still be rechecked monthly, never dropped for good")

    def test_recently_failing_board_keeps_the_weekly_cadence(self) -> None:
        self._board("fresh", fail_count=3, days_since_check=8)
        self.assertFalse(self.db.should_skip_board("fresh"))

    def test_active_board_is_never_skipped(self) -> None:
        self._board("good", fail_count=10, days_since_check=30, status="active")
        self.assertFalse(self.db.should_skip_board("good"))

    def test_other_failure_reasons_are_not_backed_off(self) -> None:
        self._board("http", fail_count=10, days_since_check=14, reason="HTTPError 500")
        self.assertFalse(self.db.should_skip_board("http"))


if __name__ == "__main__":
    unittest.main()
