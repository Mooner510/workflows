"""Persistent tool downloads and package caches must honour GHARP_DEPENDENCY_CACHE.

Gharp gives rw jobs GHARP_DEPENDENCY_CACHE (the persistent /cache/tool). With a runner image
that carries its own tool cache, RUNNER_TOOL_CACHE points into the image instead, so anything
cached only under RUNNER_TOOL_CACHE would be downloaded again on every job.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTIONS = ROOT / ".github" / "actions"

# Assignments that root a persistent cache directly at RUNNER_TOOL_CACHE.
BARE_ROOT = re.compile(r'^\s*\w+="\$\{RUNNER_TOOL_CACHE:-[^}]*\}')
PREFERRED_ROOT = re.compile(r'^\s*\w+="\$\{GHARP_DEPENDENCY_CACHE:-\$\{RUNNER_TOOL_CACHE:-[^}]*\}\}')

# Places that intentionally read RUNNER_TOOL_CACHE itself.
ALLOWED = {
    # The uv binary may be preinstalled in the runner image tool cache.
    (".github/actions/ci/python/action.yml", 'tool_base="${RUNNER_TOOL_CACHE:-$RUNNER_TEMP}/uv"'),
}


class DependencyCacheRootTest(unittest.TestCase):
    def test_cache_roots_prefer_gharp_dependency_cache(self):
        offenders = []
        for path in sorted(ACTIONS.rglob("action.yml")):
            rel = path.relative_to(ROOT).as_posix()
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not BARE_ROOT.search(line) or PREFERRED_ROOT.search(line):
                    continue
                if (rel, line.strip()) in ALLOWED:
                    continue
                offenders.append(f"{rel}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "cache roots must use ${GHARP_DEPENDENCY_CACHE:-${RUNNER_TOOL_CACHE:-...}}")

    def test_uv_package_cache_and_python_use_dependency_base(self):
        text = (ACTIONS / "ci" / "python" / "action.yml").read_text(encoding="utf-8")
        self.assertIn('dependency_base="${GHARP_DEPENDENCY_CACHE:-${RUNNER_TOOL_CACHE:-$RUNNER_TEMP}}/uv"', text)
        self.assertIn('export UV_CACHE_DIR="$dependency_base/cache"', text)
        self.assertIn('export UV_PYTHON_INSTALL_DIR="$dependency_base/python"', text)

    def test_node_package_caches_use_dependency_cache(self):
        text = (ACTIONS / "ci" / "node" / "action.yml").read_text(encoding="utf-8")
        self.assertIn(
            'cache_root="${GHARP_DEPENDENCY_CACHE:-${RUNNER_TOOL_CACHE:-$HOME/.cache/github-actions-tool-cache}}"',
            text,
        )


if __name__ == "__main__":
    unittest.main()
