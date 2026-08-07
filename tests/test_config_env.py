import os
import unittest
from unittest.mock import patch

from src.config import Config


class EmptyEnvIntTests(unittest.TestCase):
    """boards.yml sets BOARDS_RESCAN_COOLDOWN_HOURS='' on non-schedule events to
    mean "use the configured value". os.environ.get returns '' for a set-but-
    empty variable, so the default never applied and int('') raised — every
    manual run of the board sweep died during config load."""

    def test_empty_env_falls_back_instead_of_raising(self) -> None:
        with patch.dict(os.environ, {"BOARDS_RESCAN_COOLDOWN_HOURS": ""}, clear=False):
            cfg = Config.load()
        self.assertIsInstance(cfg.boards.rescan_cooldown_hours, int)

    def test_every_numeric_setting_tolerates_empty(self) -> None:
        blanks = {
            "BOARDS_RESCAN_COOLDOWN_HOURS": "",
            "BOARDS_BATCH_SIZE": "",
            "BOARDS_WORKERS": "",
            "BOARDS_TIMEOUT": "",
            "HTTP_TIMEOUT": "",
        }
        with patch.dict(os.environ, blanks, clear=False):
            cfg = Config.load()
        self.assertGreater(cfg.boards.batch_size, 0)
        self.assertGreater(cfg.boards.workers, 0)
        self.assertGreater(cfg.boards.timeout, 0)
        self.assertGreater(cfg.http_timeout, 0)

    def test_real_values_still_apply(self) -> None:
        with patch.dict(os.environ, {"BOARDS_RESCAN_COOLDOWN_HOURS": "6"}, clear=False):
            self.assertEqual(Config.load().boards.rescan_cooldown_hours, 6)

    def test_whitespace_is_trimmed(self) -> None:
        with patch.dict(os.environ, {"BOARDS_BATCH_SIZE": "  25 "}, clear=False):
            self.assertEqual(Config.load().boards.batch_size, 25)

    def test_non_numeric_falls_back_rather_than_crashing(self) -> None:
        with patch.dict(os.environ, {"BOARDS_WORKERS": "abc"}, clear=False):
            self.assertGreater(Config.load().boards.workers, 0)


if __name__ == "__main__":
    unittest.main()
