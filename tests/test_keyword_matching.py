import unittest

from src.classifier import TRACK_DATA, classify, set_active_track


class SubstringCollisionTests(unittest.TestCase):
    """Keywords were matched as raw substrings, so short ones fired inside
    unrelated words and pulled non-data jobs into the digest."""

    def tearDown(self) -> None:
        set_active_track(TRACK_DATA)

    def test_llm_does_not_match_inside_fulfillment(self) -> None:
        for title in ("Retail Customer Service, Stock & Fulfillment - Langhorne Rack",
                      "General Merchandise, Closing, Fulfillment (T1889)",
                      "Seasonal Retail Stock & Fulfillment - Park West Village Rack"):
            self.assertEqual(classify(title).label, "no", title)

    def test_etl_does_not_match_inside_metlife(self) -> None:
        self.assertEqual(classify("Claims Processor at MetLife").label, "no")

    def test_elt_does_not_match_inside_delta(self) -> None:
        self.assertEqual(classify("Flight Attendant - Delta Air Lines").label, "no")

    def test_real_ai_keywords_still_match(self) -> None:
        for title in ("LLM Engineer", "ETL Developer", "NLP Engineer"):
            self.assertEqual(classify(title).label, "yes", title)


class WarehouseTests(unittest.TestCase):
    """'warehouse' alone means a physical warehouse far more often than a data
    one — literal warehouse jobs were scoring 55 and filling the review bucket."""

    def test_physical_warehouse_roles_are_rejected(self) -> None:
        for title in ("Driver CDL/Warehouse Associate",
                      "Freestyle Automated Warehouse Operator",
                      "Warehouse General Utility - 12-Hour Night Shift",
                      "Warehouse Projects Associate",
                      "Materials Associate, Warehouse"):
            self.assertEqual(classify(title).label, "no", title)

    def test_data_warehouse_roles_still_match(self) -> None:
        for title in ("Data Warehouse Engineer", "Data Warehousing Analyst",
                      "Data Warehouse Developer"):
            self.assertIn(classify(title).label, ("yes", "maybe"), title)


class PluralTests(unittest.TestCase):
    """Strict word boundaries silently dropped every pluralised title, which is
    how real postings are often written."""

    def test_plural_titles_match(self) -> None:
        for title in ("Data Scientists", "Data Analysts", "Data Engineers",
                      "AI Engineers", "Applied Scientists"):
            self.assertEqual(classify(title).label, "yes", title)

    def test_singular_titles_still_match(self) -> None:
        for title in ("Data Scientist", "Data Analyst", "Data Engineer"):
            self.assertEqual(classify(title).label, "yes", title)


class SeparatorTests(unittest.TestCase):
    def test_punctuation_between_words_is_tolerated(self) -> None:
        for title in ("Power BI Developer", "Power-BI Developer"):
            self.assertIn(classify(title).label, ("yes", "maybe"), title)


if __name__ == "__main__":
    unittest.main()
