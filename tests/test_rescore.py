import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from src.database import Database
from src.main import run_digest, run_rescore
from src.scoring_policy import SCORING_VERSION


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], list[str]]] = []

    def notify(self, yes_jobs, maybe_jobs, *, subject_prefix, mode, source_errors=None):
        self.calls.append((subject_prefix, [j.key for j in yes_jobs], [j.key for j in maybe_jobs]))
        return []


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        features=SimpleNamespace(notifications=True),
        filter=SimpleNamespace(require_us_location=True),
    )


class RescoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = os.path.join(tempfile.mkdtemp(), "rescore.db")
        self.db = Database(self.path)
        self.cfg = _cfg()

    def tearDown(self) -> None:
        self.db.close()

    def _seen(self, key: str, *, title: str = "Data Analyst", label: str = "yes", score: int = 80) -> None:
        self.db.mark_job_seen(
            key=key, source="gh", company=f"Co-{key}", title=title,
            location="Remote - US", url=f"https://example.com/{key}", posted="",
            score=score, label=label, grade="A", evaluation_json="", description="",
        )

    def _raw(self, key: str) -> dict:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone())
        finally:
            conn.close()

    def _set_version(self, key: str, version: int) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE jobs SET scoring_version=? WHERE key=?", (version, key))
        conn.commit()
        conn.close()

    # -- version stamping ---------------------------------------------------

    def test_newly_stored_jobs_are_stamped_with_the_current_version(self):
        """Otherwise every scan would immediately create a fresh rescore backlog."""
        self._seen("a")
        self.assertEqual(self._raw("a")["scoring_version"], SCORING_VERSION)
        self.assertEqual(self.db.count_jobs_needing_rescore(SCORING_VERSION), 0)

    def test_only_rows_below_the_current_version_are_selected(self):
        self._seen("current")
        self._seen("stale")
        self._set_version("stale", SCORING_VERSION - 1)

        self.assertEqual(self.db.count_jobs_needing_rescore(SCORING_VERSION), 1)
        keys = [r["key"] for r in self.db.get_jobs_for_rescore(SCORING_VERSION)]
        self.assertEqual(keys, ["stale"])

    def test_rescore_all_ignores_the_version_stamp(self):
        self._seen("a")
        self._seen("b")
        keys = {r["key"] for r in self.db.get_jobs_for_rescore(SCORING_VERSION + 1)}
        self.assertEqual(keys, {"a", "b"})

    def test_limit_leaves_the_remainder_for_the_next_pass(self):
        for i in range(5):
            self._seen(f"j{i}")
            self._set_version(f"j{i}", 0)

        run_rescore(cfg=self.cfg, db=self.db, limit=2)
        self.assertEqual(self.db.count_jobs_needing_rescore(SCORING_VERSION), 3)

        run_rescore(cfg=self.cfg, db=self.db, limit=0)
        self.assertEqual(self.db.count_jobs_needing_rescore(SCORING_VERSION), 0)

    def test_unscoreable_rows_are_still_stamped(self):
        """A row that cannot be scored must not block the backlog forever."""
        self._seen("j")
        self._set_version("j", 0)

        def _boom(*args, **kwargs):
            raise RuntimeError("scoring blew up")

        with mock.patch("src.main.evaluate_job", _boom):
            summary = run_rescore(cfg=self.cfg, db=self.db)

        self.assertEqual(summary["errors"], 1)
        self.assertEqual(self.db.count_jobs_needing_rescore(SCORING_VERSION), 0)

    # -- score correction ---------------------------------------------------

    def test_stale_score_is_replaced_by_the_current_one(self):
        # Stored with a score no current evaluation would produce.
        self._seen("j", title="Data Analyst", label="no", score=0)
        self._set_version("j", 0)

        summary = run_rescore(cfg=self.cfg, db=self.db)

        row = self._raw("j")
        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(row["scoring_version"], SCORING_VERSION)
        self.assertNotEqual((row["score"], row["label"]), (0, "no"))
        self.assertEqual(summary["updated"], 1)

    def test_dry_run_reports_without_writing(self):
        self._seen("j", label="no", score=0)
        self._set_version("j", 0)

        summary = run_rescore(cfg=self.cfg, db=self.db, dry_run=True)

        row = self._raw("j")
        self.assertGreater(summary["updated"], 0)
        self.assertEqual((row["score"], row["label"]), (0, "no"))
        self.assertEqual(row["scoring_version"], 0)

    def test_rescore_never_touches_delivery_columns(self):
        """alerted_at/first_seen/last_seen are sighting facts, not scoring facts."""
        self._seen("j", label="no", score=0)
        self.db.mark_jobs_alerted(["j"])
        before = self._raw("j")
        self._set_version("j", 0)

        run_rescore(cfg=self.cfg, db=self.db)

        after = self._raw("j")
        for column in ("alerted_at", "first_seen", "last_seen"):
            self.assertEqual(before[column], after[column], column)

    # -- the reason this exists ---------------------------------------------

    def test_a_rescued_job_reaches_the_next_digest(self):
        """A row stored as "no" under old logic must be emailable once it qualifies.

        This is the whole point: the digest selects on the stored label, so
        without a rescore the role is invisible to it forever.
        """
        self._seen("rescued", title="Data Analyst", label="no", score=0)
        self._set_version("rescued", 0)

        notifier = _RecordingNotifier()
        run_digest(cfg=self.cfg, db=self.db, notifier=notifier, dry_run=False, no_notify=False)
        self.assertEqual(notifier.calls, [], "a 'no' row must not be emailed before the rescore")

        summary = run_rescore(cfg=self.cfg, db=self.db)
        self.assertEqual(summary["newly_pending"], 1)

        notifier = _RecordingNotifier()
        run_digest(cfg=self.cfg, db=self.db, notifier=notifier, dry_run=False, no_notify=False)
        sent = [k for call in notifier.calls for k in call[1] + call[2]]
        self.assertIn("rescued", sent)

    def test_an_already_alerted_job_is_not_re_sent_after_a_rescore(self):
        self._seen("sent", title="Data Analyst", label="yes", score=80)
        self.db.mark_jobs_alerted(["sent"])
        self._set_version("sent", 0)

        run_rescore(cfg=self.cfg, db=self.db)

        notifier = _RecordingNotifier()
        run_digest(cfg=self.cfg, db=self.db, notifier=notifier, dry_run=False, no_notify=False)
        self.assertEqual(notifier.calls, [])

    def test_second_pass_is_a_no_op(self):
        """Rescoring must converge, or every run would churn the digest."""
        for i in range(4):
            self._seen(f"j{i}", title="Data Analyst")
            self._set_version(f"j{i}", 0)

        run_rescore(cfg=self.cfg, db=self.db)
        second = run_rescore(cfg=self.cfg, db=self.db, rescore_all=True)

        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["unchanged"], 4)


if __name__ == "__main__":
    unittest.main()
