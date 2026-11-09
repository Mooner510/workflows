"""Read-only jobs reuse the trusted writer's tool downloads and Trivy DB in place.

Same-repository pull requests get the base branch's cache volume read-only at /cache. Tools
that trusted writers downloaded into /cache/tool are run from there (or linked), so a pull
request job does not download and unpack them again.
"""

import datetime
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTIONS = ROOT / ".github" / "actions"


def run_script(action, cache_root, env):
    """Run an action's first `run:` script with /cache/tool redirected to CACHE_ROOT."""
    text = (ACTIONS / action).read_text(encoding="utf-8")
    body = text.split("      run: |\n", 1)[1]
    lines = []
    for line in body.splitlines():
        if line and not line.startswith("        "):
            break
        lines.append(line[8:])
    script = "\n".join(lines)
    script = script.replace("/cache/tool", str(cache_root))
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)


def write_executable(path, body="#!/bin/sh\nexit 0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


class StaticReuseTest(unittest.TestCase):
    def test_every_downloader_checks_the_read_only_cache(self):
        expected = {
            "config/load/action.yml": '-x "$ro_tool_cache/yq/$version/yq"',
            "ci/buildkit/action.yml": '-x "$ro_tool_cache/buildkit/$version/bin/buildctl"',
            "ci/security-tools/action.yml": 'ro_bin_dir="${ro_tool_cache:+$ro_tool_cache/isolated-scanners/bin}"',
            "ci/oci-artifact/action.yml": '-x "$ro_tool_cache/trivy/$trivy_version/trivy"',
            "cd/docker/prepare/action.yml": '-x "$ro_tool_cache/trivy/$trivy_version/trivy"',
            "ci/node/pnpm/action.yml": '-x "$ro_tool_cache/pnpm/$pnpm_version/bin/pnpm"',
            "ci/migration/goose/action.yml": '-x "/cache/tool/goose/$version/$arch/goose"',
        }
        for action, needle in expected.items():
            with self.subTest(action=action):
                self.assertIn(needle, (ACTIONS / action).read_text(encoding="utf-8"))

    def test_trivy_scans_prepare_the_shared_cache_first(self):
        for action, scan in (
            ("ci/oci-artifact/action.yml", '"$trivy" image'),
            ("cd/docker/prepare/action.yml", '"$trivy" image'),
            ("ci/security/action.yml", "trivy fs"),
        ):
            with self.subTest(action=action):
                text = (ACTIONS / action).read_text(encoding="utf-8")
                prepare = text.index("uses: Mooner510/workflows/.github/actions/ci/trivy-cache@v1")
                self.assertLess(prepare, text.index(scan))


class TrivyCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.ro = self.root / "ro"
        self.env_file = self.root / "env"
        self.env_file.write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def seed_ro(self, age_hours, policy=True):
        db = self.ro / "trivy-cache" / "db"
        db.mkdir(parents=True)
        (db / "trivy.db").write_text("db")
        updated = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=age_hours)
        (db / "metadata.json").write_text(json.dumps({"Version": 2, "UpdatedAt": updated.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}))
        if policy:
            (self.ro / "trivy-cache" / "policy").mkdir()
            (self.ro / "trivy-cache" / "policy" / "metadata.json").write_text("{}")

    def run_action(self, **extra):
        env = {"PATH": os.environ["PATH"], "GITHUB_ENV": str(self.env_file), "RUNNER_TEMP": str(self.root / "temp")}
        env.update(extra)
        result = run_script("ci/trivy-cache/action.yml", self.ro, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        values = dict(line.split("=", 1) for line in self.env_file.read_text().splitlines())
        return values, result.stdout

    def test_trusted_writer_keeps_the_cache_on_the_persistent_volume(self):
        dependency_cache = self.root / "dependency"
        values, _ = self.run_action(GHARP_CACHE_MODE="rw", GHARP_DEPENDENCY_CACHE=str(dependency_cache))
        self.assertEqual(values["TRIVY_CACHE_DIR"], f"{dependency_cache}/trivy-cache")
        self.assertEqual(values["TRIVY_CACHE_BACKEND"], "memory")
        self.assertNotIn("TRIVY_SKIP_DB_UPDATE", values)

    def test_read_only_job_links_a_fresh_db_and_checks_bundle(self):
        self.seed_ro(age_hours=5)
        tool_cache = self.root / "job-tools"
        values, out = self.run_action(GHARP_CACHE_MODE="ro", RUNNER_TOOL_CACHE=str(tool_cache))
        cache_dir = pathlib.Path(values["TRIVY_CACHE_DIR"])
        self.assertEqual(cache_dir, tool_cache / "trivy-cache")
        self.assertTrue((cache_dir / "db").is_symlink())
        self.assertEqual(os.readlink(cache_dir / "db"), str(self.ro / "trivy-cache" / "db"))
        self.assertTrue((cache_dir / "policy").is_symlink())
        self.assertEqual(values["TRIVY_SKIP_DB_UPDATE"], "true")
        self.assertEqual(values["TRIVY_SKIP_CHECK_UPDATE"], "true")
        self.assertIn("linked to read-only cache", out)

    def test_read_only_job_downloads_when_the_db_is_stale(self):
        self.seed_ro(age_hours=100, policy=False)
        values, out = self.run_action(GHARP_CACHE_MODE="ro", RUNNER_TOOL_CACHE=str(self.root / "job-tools"))
        self.assertFalse((pathlib.Path(values["TRIVY_CACHE_DIR"]) / "db").exists())
        self.assertNotIn("TRIVY_SKIP_DB_UPDATE", values)
        self.assertNotIn("TRIVY_SKIP_CHECK_UPDATE", values)
        self.assertIn("older than", out)

    def test_uncached_job_uses_a_job_local_cache(self):
        self.seed_ro(age_hours=1)
        values, _ = self.run_action(GHARP_CACHE_MODE="none", RUNNER_TOOL_CACHE=str(self.root / "job-tools"))
        self.assertFalse((pathlib.Path(values["TRIVY_CACHE_DIR"]) / "db").exists())
        self.assertNotIn("TRIVY_SKIP_DB_UPDATE", values)


class ScannerLinkTest(unittest.TestCase):
    def test_read_only_job_links_cached_scanners_without_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            ro = root / "ro"
            for name in ("osv-scanner", "gitleaks", "trivy"):
                write_executable(ro / "isolated-scanners" / "bin" / name)
            stub_bin = root / "stub"
            write_executable(stub_bin / "curl", "#!/bin/sh\necho unexpected download >&2\nexit 9\n")
            path_file = root / "path"
            path_file.write_text("")
            env = {
                "PATH": f"{stub_bin}:{os.environ['PATH']}",
                "GHARP_CACHE_MODE": "ro",
                "RUNNER_TOOL_CACHE": str(root / "job-tools"),
                "RUNNER_TEMP": str(root),
                "GITHUB_PATH": str(path_file),
            }
            if os.uname().machine != "x86_64":
                self.skipTest("scanner bundle is pinned for x86_64")
            result = run_script("ci/security-tools/action.yml", ro, env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            bin_dir = root / "job-tools" / "isolated-scanners" / "bin"
            for name in ("osv-scanner", "gitleaks", "trivy"):
                self.assertTrue((bin_dir / name).is_symlink(), name)
                self.assertEqual(os.readlink(bin_dir / name), str(ro / "isolated-scanners" / "bin" / name))
            self.assertNotIn("Downloading", result.stdout)
            self.assertEqual(path_file.read_text().strip(), str(bin_dir))


class BuildctlReuseTest(unittest.TestCase):
    def test_read_only_job_runs_cached_buildctl_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            ro = root / "ro"
            write_executable(ro / "buildkit" / "v0.33.0" / "bin" / "buildctl")
            stub_bin = root / "stub"
            write_executable(stub_bin / "curl", "#!/bin/sh\necho unexpected download >&2\nexit 9\n")
            path_file = root / "path"
            path_file.write_text("")
            env = {
                "PATH": f"{stub_bin}:{os.environ['PATH']}",
                "GHARP_CACHE_MODE": "ro",
                "BUILDKIT_HOST": "unix:///run/gharp-buildkit/buildkitd.sock",
                "RUNNER_TOOL_CACHE": str(root / "job-tools"),
                "RUNNER_TEMP": str(root),
                "GITHUB_PATH": str(path_file),
            }
            if os.uname().machine != "x86_64" or os.path.exists("/var/run/docker.sock"):
                self.skipTest("needs an x86_64 host without a Docker socket, like the CI runner")
            result = run_script("ci/buildkit/action.yml", ro, env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(path_file.read_text().strip(), str(ro / "buildkit" / "v0.33.0" / "bin"))
            self.assertFalse((root / "job-tools" / "buildkit").exists())


if __name__ == "__main__":
    unittest.main()
