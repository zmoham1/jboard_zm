import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.database import Database
from src.main import run_digest, run_missed_audit


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], list[str]]] = []

    def notify(self, yes_jobs, maybe_jobs, *, subject_prefix, mode, source_errors=None):
        self.calls.append((subject_prefix, [j.key for j in yes_jobs], [j.key for j in maybe_jobs]))
        return []


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(features=SimpleNamespace(notifications=True))


def _backdate(path: str, key: str, days: int) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE jobs SET first_seen=? WHERE key=?", (stamp, key))
    conn.commit()
    conn.close()


class MissedAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = os.path.join(tempfile.mkdtemp(), "audit.db")
        self.db = Database(self.path)
        self.notifier = _RecordingNotifier()

    def tearDown(self) -> None:
        self.db.close()

    def _seen(self, key: str, label: str, score: int) -> None:
        self.db.mark_job_seen(
            key=key, source="gh", company=f"Co-{key}", title="Data Analyst",
            location="", url=f"https://example.com/{key}", posted="",
            score=score, label=label,
        )

    def test_recovers_maybe_only_matches_held_back_by_yes_only_gate(self) -> None:
        self._seen("stale-maybe", "maybe", 60)
        _backdate(self.path, "stale-maybe", days=3)
        self.db = Database(self.path)

        # The regular digest holds MAYBE-only windows and sends nothing.
        run_digest(cfg=_cfg(), db=self.db, notifier=self.notifier,
                   dry_run=False, no_notify=False, notify_yes_only=True)
        self.assertEqual(self.notifier.calls, [])

        # The audit picks it up under its own subject line.
        run_missed_audit(cfg=_cfg(), db=self.db, notifier=self.notifier,
                         dry_run=False, no_notify=False, min_age_hours=24)
        self.assertEqual(len(self.notifier.calls), 1)
        subject_prefix, yes_keys, maybe_keys = self.notifier.calls[0]
        self.assertIn("Missed", subject_prefix)
        self.assertEqual(maybe_keys, ["stale-maybe"])
        self.assertEqual(yes_keys, [])

    def test_leaves_recent_matches_for_the_next_digest(self) -> None:
        self._seen("fresh", "maybe", 61)
        run_missed_audit(cfg=_cfg(), db=self.db, notifier=self.notifier,
                         dry_run=False, no_notify=False, min_age_hours=24)
        self.assertEqual(self.notifier.calls, [])

    def test_does_not_report_the_same_role_twice(self) -> None:
        self._seen("stale", "maybe", 60)
        _backdate(self.path, "stale", days=3)
        self.db = Database(self.path)

        run_missed_audit(cfg=_cfg(), db=self.db, notifier=self.notifier,
                         dry_run=False, no_notify=False, min_age_hours=24)
        run_missed_audit(cfg=_cfg(), db=self.db, notifier=self.notifier,
                         dry_run=False, no_notify=False, min_age_hours=24)
        self.assertEqual(len(self.notifier.calls), 1)

    def test_sends_nothing_when_no_roles_were_missed(self) -> None:
        run_missed_audit(cfg=_cfg(), db=self.db, notifier=self.notifier,
                         dry_run=False, no_notify=False, min_age_hours=24)
        self.assertEqual(self.notifier.calls, [])


if __name__ == "__main__":
    unittest.main()


class DeliveryStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = os.path.join(tempfile.mkdtemp(), "delivery.db")
        self.db = Database(self.path)

    def tearDown(self) -> None:
        self.db.close()

    def _seen(self, key: str, label: str = "yes", score: int = 80) -> None:
        self.db.mark_job_seen(
            key=key, source="gh", company=f"Co-{key}", title="Data Analyst",
            location="", url=f"https://example.com/{key}", posted="",
            score=score, label=label,
        )

    def test_counts_pending_and_sent(self) -> None:
        self._seen("sent-1")
        self._seen("waiting-1")
        self.db.mark_jobs_alerted(["sent-1"])

        stats = self.db.get_delivery_stats()
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["alerted_24h"], 1)
        self.assertTrue(stats["last_alert"])

    def test_reports_nothing_pending_when_all_sent(self) -> None:
        self._seen("a")
        self.db.mark_jobs_alerted(["a"])
        stats = self.db.get_delivery_stats()
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["oldest_pending"], "")

    def test_tracks_oldest_waiting_match(self) -> None:
        self._seen("old")
        self._seen("new")
        _backdate(self.path, "old", days=5)
        self.db = Database(self.path)
        stats = self.db.get_delivery_stats()
        self.assertEqual(stats["pending"], 2)
        # The oldest straggler drives the "waiting N days" warning.
        self.assertLess(stats["oldest_pending"], datetime.now(timezone.utc).isoformat())
