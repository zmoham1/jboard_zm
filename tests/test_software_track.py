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

    def test_senior_titles_are_capped(self) -> None:
        set_active_track(TRACK_SOFTWARE)
        self.assertEqual(classify("Senior Software Engineer").label, "maybe")
        self.assertEqual(classify("Director of Engineering").label, "no")


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


if __name__ == "__main__":
    unittest.main()
