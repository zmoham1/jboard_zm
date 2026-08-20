import unittest
from unittest.mock import patch

from src.classifier import TRACK_DATA, classify, set_active_track
from src.evaluation import SENIORITY_REVIEW_CAP, evaluate_job

JD = (
    "Data Scientist. Responsibilities: build and ship machine learning models, "
    "run experiments, build dashboards and data pipelines in Python and SQL. "
    "Requirements: 2 years of experience with Python, SQL, machine learning, "
    "ETL, Spark, dashboards."
)
RESUME = ("Built ML models in Python and SQL, ETL pipelines, Spark jobs, "
          "dashboards, experimentation frameworks, REST APIs.")


def _score(title: str) -> int:
    with patch("src.evaluation._resume_evidence_text", return_value=RESUME):
        return evaluate_job(title, JD, company="X", location="Atlanta, GA",
                            source="linkedin", require_us_location=False).score


class SeniorityCapTests(unittest.TestCase):
    """The classifier caps a senior title at 65 ("maybe"), but the evaluator
    could score past it — seniority is one dimension at 10% weight, a swing of
    about four points. Live digests listed "Sr. Applied Scientist" at 71 and
    "Senior Machine Learning Engineer" at 70 under STRONG MATCHES."""

    def tearDown(self) -> None:
        set_active_track(TRACK_DATA)

    def test_senior_titles_are_held_at_review(self) -> None:
        for title in ("Sr. Applied Scientist", "Senior Machine Learning Engineer",
                      "Lead Data Engineer", "Principal Data Scientist"):
            self.assertLessEqual(_score(title), SENIORITY_REVIEW_CAP, title)

    def test_cap_is_case_insensitive(self) -> None:
        # _find_seniority_token matches lowercase tokens with a case-sensitive
        # regex, so a raw title had to be lowered before the lookup.
        self.assertLessEqual(_score("SENIOR DATA ANALYST"), SENIORITY_REVIEW_CAP)
        self.assertLessEqual(_score("Senior Data Analyst"), SENIORITY_REVIEW_CAP)

    def test_senior_roles_remain_visible_as_review(self) -> None:
        # Held back, not dropped — they should still reach the maybe band.
        for title in ("Sr. Applied Scientist", "Senior Machine Learning Engineer"):
            self.assertGreater(_score(title), 0, title)

    def test_non_senior_roles_are_unaffected(self) -> None:
        for title in ("Data Scientist", "Applied Scientist", "Machine Learning Engineer"):
            self.assertGreater(_score(title), SENIORITY_REVIEW_CAP, title)

    def test_classifier_and_evaluator_now_agree(self) -> None:
        # Neither path may present a senior title as a strong match.
        for title in ("Senior Data Scientist", "Lead Data Engineer"):
            self.assertNotEqual(classify(title).label, "yes", title)
            self.assertLessEqual(_score(title), SENIORITY_REVIEW_CAP, title)


if __name__ == "__main__":
    unittest.main()
