"""Regression test: Bash heredocs in the canonical Flyway composite action."""

import pathlib
import re
import subprocess
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/ci/migration/flyway/action.yml"


def get_script():
    lines = ACTION.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "run: |":
            prefix = len(line) - len(line.lstrip()) + 2
            body = []
            for following in lines[index + 1 :]:
                if following.strip() and len(following) - len(following.lstrip()) < prefix:
                    break
                body.append(following[prefix:] if following.strip() else "")
            return "\n".join(body) + "\n"
    raise AssertionError("Flyway action run block not found")


class FlywayShellSyntaxTest(unittest.TestCase):
    def test_script_parses_without_unclosed_heredoc(self):
        script = get_script()
        self.assertIn("<<'JAVA'", script)
        self.assertIn("<<'GRADLE'", script)
        result = subprocess.run(
            ["bash", "-n"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("here-document", result.stderr)


if __name__ == "__main__":
    unittest.main()
