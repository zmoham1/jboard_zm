import unittest
from unittest.mock import patch

from src.classifier import (
    TRACK_DATA,
    TRACK_SOFTWARE,
    classify,
    get_active_track,
    set_active_track,
)
from src.evaluation import evaluate_job

RESUME = "Built REST APIs in Python, React frontends, automated tests, CI pipelines."

# Identical posting text apart from the experience clause, so comparisons
# isolate the years requirement rather than incidental wording differences.
JD = (
    "Software Engineer. Responsibilities: build and ship features across our "
    "Python and React stack, write tests, review code. "
    "Requirements: {req}strong CS fundamentals."
)


def _score(req: str) -> int:
    with patch("src.evaluation._resume_evidence_text", return_value=RESUME):
        return evaluate_job(
            "Software Engineer", JD.format(req=req), company="X",
            location="Atlanta, GA", source="greenhouse", require_us_location=False,
        ).score


class TrackSwitchTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_active_track(TRACK_DATA)

    def test_defaults_to_data_track(self) -> None:
        self.assertEqual(get_active_track(), TRACK_DATA)

    def test_data_track_is_unchanged_by_the_software_lists(self) -> None:
        set_active_track(TRACK_DATA)
        self.assertEqual(classify("Data Scientist").label, "yes")
        # A software title must not leak into the data track.
        self.assertEqual(classify("Backend Engineer").label, "no")

    def test_software_track_matches_software_titles(self) -> None:
        set_active_track(TRACK_SOFTWARE)
        for title in ("Software Engineer", "Backend Engineer", "Full Stack Developer",
                      "Java Developer", "iOS Developer"):
            self.assertEqual(classify(title).label, "yes", title)
        # And stops matching data titles.
        self.assertEqual(classify("Data Scientist").label, "no")

    def test_software_track_rejects_non_software_engineering(self) -> None:
        set_active_track(TRACK_SOFTWARE)
        for title in ("Civil Engineer", "Sales Engineer", "Mechanical Engineer",
                      "Business Development Manager", "Registered Nurse"):
            self.assertEqual(classify(title).label, "no", title)

    def test_internships_excluded_but_new_grad_included(self) -> None:
        set_active_track(TRACK_SOFTWARE)
        self.assertEqual(classify("Software Engineer Intern").label, "no")
        self.assertEqual(classify("Software Engineer, Co-op").label, "no")
        self.assertEqual(classify("Software Engineer, New Grad").label, "yes")
        self.assertEqual(classify("Software Engineer I").label, "yes")

    def test_seniority_disqualifies_on_a_0_3_year_track(self) -> None:
        # Senior/staff/lead are outside 0-3 years by definition, so this track
        # rejects them outright rather than demoting them to "maybe".
        set_active_track(TRACK_SOFTWARE)
        for title in ("Senior Software Engineer", "Staff Software Engineer, Full Stack",
                      "Lead Software Engineer", "Principal Software Engineer",
                      "Software Development Manager", "Director of Engineering",
                      "Software Engineer III", "Software Architect"):
            self.assertEqual(classify(title).label, "no", title)

    def test_data_roles_do_not_leak_into_the_software_track(self) -> None:
        set_active_track(TRACK_SOFTWARE)
        for title in ("Data Platform Engineer", "Data Scientist, Application Developer",
                      "Machine Learning Infrastructure Engineer", "BI Analyst / Developer"):
            self.assertEqual(classify(title).label, "no", title)


class ExperienceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        set_active_track(TRACK_SOFTWARE)

    def tearDown(self) -> None:
        set_active_track(TRACK_DATA)

    def test_blocks_roles_requiring_more_than_three_years(self) -> None:
        self.assertEqual(_score("4+ years of experience, "), 0)
        self.assertEqual(_score("6+ years of experience, "), 0)
        self.assertEqual(_score("10 years of experience, "), 0)

    def test_allows_three_years_or_less(self) -> None:
        for req in ("1 year of experience, ", "2+ years of experience, ", "3 years of experience, "):
            self.assertGreater(_score(req), 0, req)

    def test_explicit_junior_outranks_unstated(self) -> None:
        stated = _score("2+ years of experience, ")
        unstated = _score("")
        self.assertGreater(
            stated, unstated,
            "a posting explicitly asking for 0-3 years should rank above one that says nothing",
        )

    def test_unstated_is_still_included(self) -> None:
        self.assertGreater(_score(""), 0)

    def test_experience_ranking_does_not_apply_to_the_data_track(self) -> None:
        set_active_track(TRACK_DATA)
        self.assertEqual(_score("2+ years of experience, "), _score(""))


class ClassifierRegressionTests(unittest.TestCase):
    """Bugs found by running the classifier over real scraped postings."""

    def tearDown(self) -> None:
        set_active_track(TRACK_DATA)

    def test_strictest_seniority_cap_wins_regardless_of_token_order(self) -> None:
        # "senior" precedes "director" in SENIORITY_TOKENS. Stopping at the
        # first match capped "Sr. Director" at 65 (a surfaced match) instead of
        # 34 (rejected), so director-level roles reached the inbox.
        set_active_track(TRACK_DATA)
        for title in ("Sr. Director, Data Platform Engineering",
                      "Senior Director, Analytics",
                      "Senior Data Engineer - Vice President"):
            self.assertEqual(classify(title).label, "no", title)
        # Plain senior roles are still only demoted, not rejected.
        self.assertEqual(classify("Senior Data Scientist").label, "maybe")

    def test_clearance_participle_is_excluded(self) -> None:
        # \bclearance\b missed the participle, so "(Secret cleared)" scored 90.
        set_active_track(TRACK_DATA)
        for title in ("Data Scientist/Application Developer (Secret cleared)",
                      "Cleared AI/ML Engineer",
                      "Data Analyst in California (Secret Cleared)"):
            self.assertEqual(classify(title).label, "no", title)
        self.assertEqual(classify("Data Scientist").label, "yes")


if __name__ == "__main__":
    unittest.main()
