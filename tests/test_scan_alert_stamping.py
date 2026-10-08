"""A scan that emails a match must not leave it pending for the next digest.

Production symptom (2026-10-08): the 16:55 UTC priority alert and the 17:35 UTC
digest carried the same 16 roles -- every job in the priority email reappeared
in the digest 40 minutes later.

Cause: only run_digest called mark_jobs_alerted. The scan modes emailed and then
stored their matches with alerted_at='' (pending), so the next digest collected
them again. This was latent until priority.yml and boards.yml stopped passing
--no-notify, which is what made the scan modes senders in the first place.
"""
import os
import tempfile
import unittest
from types import SimpleNamespace

from src.database import Database
from src.main import _dispatch_results, run_digest
from src.sources.base import Job


class _Notifier:
    """Records each email as the set of job keys it carried."""

    def __init__(self, *, fail: bool = False) -> None:
        self.emails: list[set[str]] = []
        self.fail = fail

    def notify(self, yes_jobs, maybe_jobs, **_):
        self.emails.append({j.key for j in yes_jobs + maybe_jobs})
        return ["smtp exploded"] if self.fail else []


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        features=SimpleNamespace(notifications=True),
        filter=SimpleNamespace(require_us_location=False),
    )


def _job(key: str, score: int, label: str) -> Job:
    return Job(
        key=key, source="gh", company=f"Co-{key}", title="Data Scientist",
        location="Remote", url=f"https://example.com/{key}", posted="",
        score=score, label=label,
    )


class ScanAlertStampingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(os.path.join(tempfile.mkdtemp(), "stamp.db"))

    def tearDown(self) -> None:
        self.db.close()

    def _scan(self, jobs, notifier, **kw) -> None:
        _dispatch_results(
            all_jobs=jobs, errors=[], db=self.db, notifier=notifier,
            mode=kw.pop("mode", "priority"), dry_run=False, no_notify=False,
            test_notify=False, cfg=_cfg(), notify_yes_only=kw.pop("notify_yes_only", False),
        )

    def test_scanned_and_emailed_job_is_not_resent_by_the_digest(self) -> None:
        scan = _Notifier()
        self._scan([_job("a", 90, "yes")], scan)
        self.assertEqual(scan.emails, [{"a"}], "scan should have emailed the match")

        digest = _Notifier()
        run_digest(cfg=_cfg(), db=self.db, notifier=digest,
                   dry_run=False, no_notify=False, notify_yes_only=False)
        self.assertEqual(digest.emails, [], "digest re-sent a job the scan already emailed")

    def test_the_whole_batch_is_stamped_not_just_the_first(self) -> None:
        jobs = [_job(k, 90, "yes") for k in ("a", "b", "c")] + [_job("d", 70, "maybe")]
        self._scan(jobs, _Notifier())
        self.assertEqual(self.db.get_pending_alert_jobs(), [])

    def test_maybes_held_back_by_notify_yes_only_stay_pending(self) -> None:
        """--notify-yes-only sends nothing when there is no YES. Those MAYBEs
        were never emailed, so the digest must still pick them up."""
        scan = _Notifier()
        self._scan([_job("m", 70, "maybe")], scan, notify_yes_only=True)
        self.assertEqual(scan.emails, [], "nothing should be emailed without a YES")

        digest = _Notifier()
        run_digest(cfg=_cfg(), db=self.db, notifier=digest,
                   dry_run=False, no_notify=False, notify_yes_only=False)
        self.assertEqual(digest.emails, [{"m"}], "digest lost a MAYBE the scan held back")

    def test_failed_delivery_leaves_the_job_pending_for_the_digest(self) -> None:
        """Re-sending beats silently dropping a match, so a notifier error must
        not stamp. Mirrors run_digest's posture on the same failure."""
        self._scan([_job("a", 90, "yes")], _Notifier(fail=True))
        self.assertEqual([j["key"] for j in self.db.get_pending_alert_jobs()], ["a"])

    def test_no_notify_scan_leaves_everything_pending(self) -> None:
        """main.yml still runs --no-notify and relies on the digest to send."""
        quiet = _Notifier()
        _dispatch_results(
            all_jobs=[_job("a", 90, "yes")], errors=[], db=self.db, notifier=quiet,
            mode="main", dry_run=False, no_notify=True, test_notify=False,
            cfg=_cfg(), notify_yes_only=False,
        )
        self.assertEqual(quiet.emails, [])
        self.assertEqual([j["key"] for j in self.db.get_pending_alert_jobs()], ["a"])

    def test_second_scan_of_the_same_job_does_not_re_email(self) -> None:
        """is_new_job already guarded this; the stamp must not regress it."""
        first = _Notifier()
        self._scan([_job("a", 90, "yes")], first)
        second = _Notifier()
        self._scan([_job("a", 90, "yes")], second)
        self.assertEqual(first.emails, [{"a"}])
        self.assertEqual(second.emails, [])


if __name__ == "__main__":
    unittest.main()
