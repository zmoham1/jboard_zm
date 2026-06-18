import unittest
from unittest.mock import patch

from src.evaluation import _jd_sections, evaluate_job


class EvaluationEvidenceTests(unittest.TestCase):
    def test_critical_skill_without_resume_evidence_caps_score(self) -> None:
        jd = (
            "Data Engineer. Required: 3 years of enterprise Java experience, "
            "ETL pipelines, BigQuery, Power BI, and data modeling."
        )
        evidence = (
            "Built Python and SQL data pipelines with ETL workflows. "
            "Created BigQuery reporting tables and Power BI dashboards."
        )

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Data Engineer",
                jd,
                company="Google",
                location="New York, NY",
                source="google",
                require_us_location=False,
            )

        self.assertIn("java", result.matched_strong)
        self.assertIn("java", result.unsupported_strong)
        self.assertIn("java", result.critical_skill_gaps)
        self.assertLessEqual(result.score, 72)
        self.assertTrue(any("java" in reason.lower() for reason in result.reasons))

    def test_plain_text_jd_without_headings_still_scores_from_general_text(self) -> None:
        jd = (
            "Data Scientist role focused on python, sql, experimentation, product analytics, "
            "and stakeholder reporting for growth teams."
        )
        evidence = (
            "Built python and sql analytics workflows. Designed experimentation readouts and "
            "product analytics dashboards for growth stakeholders."
        )

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Data Scientist",
                jd,
                company="Example",
                location="Remote, United States",
                source="linkedin",
                require_us_location=False,
            )

        self.assertGreaterEqual(result.score, 60)
        self.assertNotIn("python", result.critical_skill_gaps)

    def test_preferred_skill_gap_does_not_become_critical(self) -> None:
        jd = (
            "Required Qualifications\n"
            "- Python\n"
            "- SQL\n\n"
            "Preferred Qualifications\n"
            "- Kubernetes\n"
        )
        evidence = "Built Python and SQL pipelines for analytics reporting."

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Data Engineer",
                jd,
                company="Example",
                location="Austin, TX",
                source="google",
                require_us_location=False,
            )

        self.assertIn("kubernetes", result.unsupported_strong)
        self.assertNotIn("kubernetes", result.critical_skill_gaps)

    def test_responsibility_skill_gap_uses_middle_tier_penalty(self) -> None:
        jd = (
            "What You'll Do\n"
            "- Build Kafka streaming workflows and python services.\n\n"
            "Minimum Qualifications\n"
            "- Python\n"
        )
        evidence = "Built Python services and analytics APIs."

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Data Engineer",
                jd,
                company="Example",
                location="Remote, United States",
                source="google",
                require_us_location=False,
            )

        self.assertNotIn("kafka", result.critical_skill_gaps)
        self.assertTrue(any("core jd responsibilities" in reason.lower() for reason in result.reasons))
        self.assertLessEqual(result.score, 80)

    def test_nonstandard_required_alias_maps_to_required_bucket(self) -> None:
        jd = (
            "Your Expertise\n"
            "- Strong experience with Java and SQL\n"
        )
        evidence = "Built SQL reporting workflows and python data pipelines."

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Data Engineer",
                jd,
                company="Example",
                location="New York, NY",
                source="google",
                require_us_location=False,
            )

        self.assertIn("java", result.critical_skill_gaps)

    def test_inline_required_heading_is_parsed(self) -> None:
        jd = (
            "Required Qualifications: Python, SQL, Java\n"
            "Preferred Qualifications: Tableau\n"
        )

        sections = _jd_sections(jd)

        self.assertIn("python", sections.get("required", "").lower())
        self.assertIn("tableau", sections.get("preferred", "").lower())

    def test_part_time_in_benefits_text_does_not_hard_block_role(self) -> None:
        jd = (
            "What you'll do: Build python and sql data pipelines.\n"
            "Benefits include support for full-time and part-time associates.\n"
            "Preferred Qualifications: Kafka.\n"
        )
        evidence = "Built Python and SQL data pipelines for production analytics systems."

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Data Engineer II",
                jd,
                company="Walmart",
                location="Sunnyvale, CA",
                source="linkedin",
                require_us_location=False,
            )

        self.assertNotEqual(result.score, 0)
        self.assertNotEqual(result.label, "no")
        self.assertFalse(any("hard exclusion" in reason.lower() for reason in result.reasons))

    def test_equal_opportunity_citizenship_text_does_not_hard_block_role(self) -> None:
        jd = (
            "Build scalable evaluations for LLM performance on scientific reasoning.\n"
            "Strong background in LLM training and deployment.\n"
            "Equal employment opportunity regardless of race, color, age, citizenship, or veteran status.\n"
        )
        evidence = (
            "Built LLM evaluation harnesses, RAG systems, and model quality workflows for scientific use cases."
        )

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Machine Learning Scientist, LLM Training & Inference Research",
                jd,
                company="Lila Sciences",
                location="Cambridge, MA",
                source="linkedin",
                require_us_location=False,
            )

        self.assertNotEqual(result.score, 0)
        self.assertFalse(any("citizenship" in reason.lower() and "blocked" in reason.lower() for reason in result.reasons))

    def test_citizenship_requirement_is_scored_not_blocked(self) -> None:
        jd = (
            "Build scalable evaluations for LLM performance.\n"
            "U.S. citizenship is required for this role.\n"
            "Strong background in model evaluation and inference systems.\n"
        )
        evidence = "Built LLM evaluation harnesses and inference quality workflows."

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Machine Learning Scientist",
                jd,
                company="Example",
                location="Cambridge, MA",
                source="linkedin",
                require_us_location=False,
            )

        self.assertNotEqual(result.score, 0)
        self.assertTrue(any("citizenship requirement detected" in reason.lower() for reason in result.reasons))


if __name__ == "__main__":
    unittest.main()
