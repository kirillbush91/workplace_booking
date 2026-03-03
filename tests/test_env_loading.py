from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from booking_bot.run import _load_env_files


class EnvLoadingTests(unittest.TestCase):
    def test_local_env_overrides_shared_but_not_process_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / ".env.shared"
            local = Path(tmp) / ".env"
            shared.write_text("A=shared\nB=shared\nC=shared\n", encoding="utf-8")
            local.write_text("B=local\nD=local\n", encoding="utf-8")

            with patch.dict(os.environ, {"C": "process"}, clear=True):
                loaded = _load_env_files(shared_path=shared, local_path=local)
                self.assertEqual(loaded, [str(shared), str(local)])
                self.assertEqual(os.environ["A"], "shared")
                self.assertEqual(os.environ["B"], "local")
                self.assertEqual(os.environ["C"], "process")
                self.assertEqual(os.environ["D"], "local")


if __name__ == "__main__":
    unittest.main()
