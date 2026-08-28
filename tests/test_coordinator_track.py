import unittest
from unittest.mock import patch

from src.classifier import (
    DATA_STRONG,
    DATA_WEAK,
    TRACK_COORDINATOR,
    TRACK_DATA,
    TRACK_SOFTWARE,
    classify,
    get_active_track,
    set_active_track,
)
from src.coordinator_keywords import (
    COORDINATOR_STRONG,
    COORDINATOR_TARGET_ROLES,
    COORDINATOR_WEAK,
)
from src.evaluation import evaluate_job
from src.software_keywords import SOFTWARE_STRONG, SOFTWARE_WEAK

RESUME = "Coordinated project schedules, tracked deliverables, ran status reporting in Excel and Jira."

JD = (
    "Project Coordinator. Responsibilities: maintain project schedules, track "
    "deliverables and risks, coordinate across teams, prepare status reports. "
    "Requirements: {req}strong organisational skills."
)


def _score(req: str, title: str = "Project Coordinator") -> int:
    with patch("src.evaluation._resume_evidence_text", return_value=RESUME):
        return evaluate_job(
            title, JD.format(req=req), company="X",
            location="Atlanta, GA", source="greenhouse", require_us_location=False,
        ).score


class TrackSwitchTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_active_track(TRACK_DATA)

    def test_defaults_to_data_track(self) -> None:
        self.assertEqual(get_active_track(), TRACK_DATA)

    def test_coordinator_is_a_selectable_track(self) -> None:
        set_active_track(TRACK_COORDINATOR)
        self.assertEqual(get_active_track(), TRACK_COORDINATOR)

    def test_unknown_track_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            set_active_track("coordinater")


class ClassifierTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_active_track(TRACK_DATA)

    def test_matches_the_coordinator_family(self) -> None:
        set_active_track(TRACK_COORDINATOR)
        for title in ("Project Coordinator", "Program Coordinator", "Project Administrator",
                      "PMO Analyst", "Project Scheduler", "Project Operations Coordinator"):
            self.assertEqual(classify(title).label, "yes", title)

    def test_plural_and_separator_variants_still_match(self) -> None:
        """Word-boundary matching must not lose "Coordinators" or hyphenation."""
        set_active_track(TRACK_COORDINATOR)
        for title in ("Project Coordinators", "Project-Coordinator", "Project Co-ordinator"):
            self.assertEqual(classify(title).label, "yes", title)

    def test_entry_level_markers_rank_above_a_bare_title(self) -> None:
        set_active_track(TRACK_COORDINATOR)
        bare = classify("Project Coordinator").score
        for title in ("Project Coordinator I", "Entry Level Project Coordinator",
                      "Junior Project Coordinator"):
            self.assertGreater(classify(title).score, bare, title)

    def test_seniority_is_held_at_review_not_rejected(self) -> None:
        """Coordinator postings are junior by nature; a senior one is a stretch,
        not noise, so it surfaces as MAYBE rather than being dropped."""
        set_active_track(TRACK_COORDINATOR)
        self.assertEqual(classify("Senior Project Coordinator").label, "maybe")

    def test_director_level_is_rejected(self) -> None:
        set_active_track(TRACK_COORDINATOR)
        for title in ("Director of Project Management", "VP, Program Management"):
            self.assertEqual(classify(title).label, "no", title)

    def test_program_manager_titles_are_rejected(self) -> None:
        """The first live sweep put 22 Technical Program Manager postings from
        one company into a 60-role digest, crowding out real coordinators.
        A TPM is a senior engineering role, not a coordination job."""
        set_active_track(TRACK_COORDINATOR)
        for title in ("Technical Program Manager, Compute", "Senior Technical Program Manager",
                      "Program Manager, Compliance", "Product Program Manager",
                      "Project Manager", "Senior Professional Services Project Manager",
                      "Legal Program Manager"):
            self.assertEqual(classify(title).label, "no", title)

    def test_explicitly_junior_manager_titles_still_match(self) -> None:
        """Cutting the bare noun must not take the real entry-level titles."""
        set_active_track(TRACK_COORDINATOR)
        for title in ("Associate Project Manager", "Assistant Project Manager",
                      "Junior Project Manager", "Associate Program Manager"):
            self.assertEqual(classify(title).label, "maybe", title)

    def test_generic_operations_coordinators_are_rejected(self) -> None:
        """Generic office nouns caught paralegal, HR, ad-ops and hospital
        scheduling roles — one scored 71 and was shown as a STRONG match."""
        set_active_track(TRACK_COORDINATOR)
        for title in ("Legal Operations Coordinator", "Talent Operations Coordinator",
                      "People Operations Coordinator", "Advertising Operations Coordinator",
                      "Referral and Scheduling Coordinator", "Resource Coordinator"):
            self.assertEqual(classify(title).label, "no", title)

    def test_qualified_project_forms_still_match(self) -> None:
        """The word "project" is the signal that separates the real thing from
        an admin role sharing a noun."""
        set_active_track(TRACK_COORDINATOR)
        for title in ("Project Operations Coordinator", "Project Planning Coordinator",
                      "Project Implementation Coordinator"):
            self.assertEqual(classify(title).label, "yes", title)

    def test_same_noun_different_profession_is_rejected(self) -> None:
        """Clinical and trade roles share the word but not the job."""
        set_active_track(TRACK_COORDINATOR)
        for title in ("Patient Care Coordinator", "Care Coordinator", "Nurse Coordinator"):
            self.assertEqual(classify(title).label, "no", title)

    def test_unrelated_coordinator_titles_do_not_match(self) -> None:
        set_active_track(TRACK_COORDINATOR)
        for title in ("Marketing Coordinator", "Event Coordinator", "Payroll Coordinator"):
            self.assertEqual(classify(title).label, "no", title)


class TrackIsolationTests(unittest.TestCase):
    """The three tracks keep separate databases with no shared dedup, so a
    title matching two of them would be emailed twice."""

    def tearDown(self) -> None:
        set_active_track(TRACK_DATA)

    def test_keyword_lists_do_not_overlap(self) -> None:
        coordinator = set(COORDINATOR_STRONG) | set(COORDINATOR_WEAK)
        data = set(DATA_STRONG) | set(DATA_WEAK)
        software = set(SOFTWARE_STRONG) | set(SOFTWARE_WEAK)
        self.assertEqual(coordinator & data, set())
        self.assertEqual(coordinator & software, set())

    def test_every_title_scores_on_at_most_one_track(self) -> None:
        titles = [
            "Project Coordinator", "Program Coordinator", "PMO Analyst", "Project Manager",
            "Data Analyst", "Data Scientist", "Business Analyst", "Operations Analyst",
            "Software Engineer", "Backend Engineer",
        ]
        for title in titles:
            hits = []
            for track in (TRACK_DATA, TRACK_SOFTWARE, TRACK_COORDINATOR):
                set_active_track(track)
                if classify(title).label != "no":
                    hits.append(track)
            self.assertLessEqual(len(hits), 1, f"{title!r} matched {hits}")

    def test_coordinator_titles_are_invisible_to_the_other_tracks(self) -> None:
        for track in (TRACK_DATA, TRACK_SOFTWARE):
            set_active_track(track)
            for title in ("Project Coordinator", "Program Coordinator", "PMO Analyst"):
                self.assertEqual(classify(title).label, "no", f"{title} on {track}")

    def test_other_tracks_titles_are_invisible_here(self) -> None:
        set_active_track(TRACK_COORDINATOR)
        for title in ("Data Engineer", "Data Scientist", "Software Engineer",
                      "Machine Learning Engineer", "Site Reliability Engineer"):
            self.assertEqual(classify(title).label, "no", title)


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        set_active_track(TRACK_COORDINATOR)

    def tearDown(self) -> None:
        set_active_track(TRACK_DATA)

    def test_target_roles_switch_with_the_track(self) -> None:
        from src.evaluation import _active_target_roles
        self.assertEqual(_active_target_roles(), list(COORDINATOR_TARGET_ROLES))
        set_active_track(TRACK_DATA)
        self.assertNotEqual(_active_target_roles(), list(COORDINATOR_TARGET_ROLES))

    def test_four_plus_years_is_blocked(self) -> None:
        """The shared experience gate applies here too — no track-specific
        wiring needed, but it must actually fire."""
        self.assertEqual(_score("5+ years of experience required. "), 0)

    def test_junior_posting_outscores_a_senior_one(self) -> None:
        self.assertGreater(_score("1-2 years of experience. "), _score("4+ years of experience. "))

    def test_unstated_experience_is_still_scored(self) -> None:
        """Most coordinator postings omit a number; dropping them would gut the
        track. The 0-3 unstated cap is software-only by design."""
        self.assertGreater(_score(""), 0)


class NotifierTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_active_track(TRACK_DATA)

    def test_email_announces_the_coordinator_track(self) -> None:
        """The first software email announced itself as a data-roles alert;
        this makes sure the same mistake is not repeated here."""
        from src.notifier import _build_html
        from src.sources.base import Job

        job = Job(
            key="k", source="greenhouse", company="Acme", title="Project Coordinator",
            location="Remote - US", url="https://example.com/1", posted="",
            score=90, label="yes",
        )
        set_active_track(TRACK_COORDINATOR)
        html = _build_html([job], [], mode="digest")
        self.assertIn("Project Coordinator Roles Alert", html)
        self.assertNotIn("Data Roles Alert", html)
        self.assertNotIn("Software Roles Alert", html)


if __name__ == "__main__":
    unittest.main()
