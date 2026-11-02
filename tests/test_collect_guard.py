"""Guard: pytest sammelt nur tests/, nie die historischen Skripte in tools/."""
import sys; from pathlib import Path  # noqa: E702
sys.path[:0] = [str(Path(__file__).resolve().parents[1])]

import importlib.util  # noqa: E402
import subprocess  # noqa: E402
import unittest  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest nicht installiert")
class CollectGuard(unittest.TestCase):
    def test_collect_only_tests_dir(self):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", "."],
            cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        items = [line.strip() for line in result.stdout.splitlines() if "::" in line]
        self.assertTrue(any(item.startswith("tests/") for item in items), result.stdout)
        for item in items:
            self.assertNotIn("tools/", item)
            self.assertNotIn("audio_sync_variants", item)


if __name__ == "__main__":
    unittest.main()
