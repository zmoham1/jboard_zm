import os
import tempfile
import unittest
from types import SimpleNamespace

from src.classifier import TRACK_DATA, TRACK_SOFTWARE, set_active_track
from src.database import Database
from src.main import MAX_DIGEST_ROLES, run_digest
from src.notifier import _build_html
from src.sources.base import Job


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[int] = []

    def notify(self, yes_jobs, maybe_jobs, **_):
        self.sent.append(len(yes_jobs) + len(maybe_jobs))
        return []


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(features=SimpleNamespace(notifications=True))


class DigestCapTests(unittest.TestCase):
    """The software track's first run emailed 1,319 roles in one message: a new
    database has no alert history, so its whole opening inventory read as new."""

    def setUp(self) -> None:
        self.db = Database(os.path.join(tempfile.mkdtemp(), "cap.db"))
        self.notifier = _Notifier()

    def tearDown(self) -> None:
        self.db.close()

    def _seed(self, count: int) -> None:
        for i in range(count):
            self.db.mark_job_seen(
                key=f"k{i}", source="gh", company=f"Co{i}", title="Software Engineer",
                location="", url=f"https://example.com/{i}", posted="",
                score=90 - (i % 40), label="yes" if i % 3 else "maybe",
            )

    def test_single_email_is_capped(self) -> None:
        self._seed(150)
        run_digest(cfg=_cfg(), db=self.db, notifier=self.notifier,
                   dry_run=False, no_notify=False, notify_yes_only=False)
        self.assertEqual(self.notifier.sent[0], MAX_DIGEST_ROLES)

    def test_trimmed_roles_stay_pending_rather_than_being_dropped(self) -> None:
        self._seed(150)
        run_digest(cfg=_cfg(), db=self.db, notifier=self.notifier,
                   dry_run=False, no_notify=False, notify_yes_only=False)
        self.assertEqual(len(self.db.get_pending_alert_jobs()), 150 - MAX_DIGEST_ROLES)

    def test_backlog_drains_over_successive_digests_losing_nothing(self) -> None:
        self._seed(150)
        for _ in range(3):
            run_digest(cfg=_cfg(), db=self.db, notifier=self.notifier,
                       dry_run=False, no_notify=False, notify_yes_only=False)
        self.assertEqual(sum(self.notifier.sent), 150)
        self.assertEqual(len(self.db.get_pending_alert_jobs()), 0)

    def test_small_digests_are_unaffected(self) -> None:
        self._seed(5)
        run_digest(cfg=_cfg(), db=self.db, notifier=self.notifier,
                   dry_run=False, no_notify=False, notify_yes_only=False)
        self.assertEqual(self.notifier.sent, [5])


class EmailLabellingTests(unittest.TestCase):
    """The first software digest was headed 'Data Roles Alert' and listed data
    target roles, because the template hardcoded the data profile."""

    def tearDown(self) -> None:
        set_active_track(TRACK_DATA)

    def _html(self, track: str) -> str:
        set_active_track(track)
        job = Job(key="1", source="s", company="Acme", title="Software Engineer I",
                  location="NY", url="https://example.com", score=85, label="yes")
        return _build_html([job], [], "digest", None)

    def test_software_digest_is_labelled_software(self) -> None:
        html = self._html(TRACK_SOFTWARE)
        self.assertIn("Software Roles Alert", html)
        self.assertNotIn("Data Roles Alert", html)

    def test_data_digest_is_unchanged(self) -> None:
        html = self._html(TRACK_DATA)
        self.assertIn("Data Roles Alert", html)
        self.assertNotIn("Software Roles Alert", html)

    def test_footer_roles_follow_the_track(self) -> None:
        self.assertIn("Software Developer", self._html(TRACK_SOFTWARE))
        self.assertIn("Data Analyst", self._html(TRACK_DATA))


if __name__ == "__main__":
    unittest.main()
