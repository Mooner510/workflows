"""Exercise the canonical stacks contract, detector, Python CI, and scheduled-service shells."""

import atexit
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ROOT / ".github/actions"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if os.environ.get("WORKFLOWS_TEST_TOOL_CACHE"):
    TOOL_CACHE = Path(os.environ["WORKFLOWS_TEST_TOOL_CACHE"])
else:
    TOOL_CACHE = Path(tempfile.mkdtemp(prefix="workflows-tool-cache-"))
    atexit.register(shutil.rmtree, TOOL_CACHE, True)


def run_block(action, step):
    """Return the dedented `run: |` script of the named composite step."""
    lines = (ACTIONS / action / "action.yml").read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f"- name: {step}":
            break
    else:
        raise AssertionError(f"step {step!r} not found in {action}")
    for index in range(index + 1, len(lines)):
        if lines[index].strip() == "run: |":
            prefix = len(lines[index]) - len(lines[index].lstrip()) + 2
            body = []
            for following in lines[index + 1 :]:
                if following.strip() and len(following) - len(following.lstrip()) < prefix:
                    break
                body.append(following[prefix:] if following.strip() else "")
            return "\n".join(body) + "\n"
    raise AssertionError(f"run block for {step!r} not found")


def segment(action, step, start, end):
    script = run_block(action, step)
    begin = script.index(start.lstrip())
    return script[begin : script.index(end.lstrip(), begin)]


def read_outputs(path):
    outputs = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        outputs[key] = value
    return outputs


class Workspace:
    def __init__(self, test):
        self.directory = tempfile.TemporaryDirectory()
        test.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = self.root / "repo"
        self.temp = self.root / "tmp"
        self.repo.mkdir()
        self.temp.mkdir()

    def write(self, relative, content=""):
        target = self.repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def run(self, script, extra_env=None):
        output = self.root / "github_output"
        output.write_text("", encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(self.root)),
            "GITHUB_WORKSPACE": str(self.repo),
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(self.temp),
            "RUNNER_TOOL_CACHE": str(TOOL_CACHE),
        }
        env.update(extra_env or {})
        result = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + script],
            env=env,
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, read_outputs(output)


def python_component(**extra):
    component = {"name": "sync", "type": "python", "path": "automation"}
    component.update(extra)
    return component


def scheduled_service(**schedule_overrides):
    service = {
        "name": "sync",
        "path": "automation",
        "component": "sync",
        "expose": False,
        "schedule": {"cron": "0 15 * * *"},
    }
    service["schedule"].update(schedule_overrides)
    return service


class LoaderTest(unittest.TestCase):
    def load(self, contract, files=("automation/pyproject.toml", "automation/uv.lock", "automation/Dockerfile")):
        workspace = Workspace(self)
        for relative in files:
            workspace.write(relative, "")
        workspace.write(".github/stacks.yml", json.dumps(contract))
        return workspace.run(run_block("config/load", "Load project contract"))

    def assert_fails(self, contract, message, **kwargs):
        result, _ = self.load(contract, **kwargs)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stdout)

    def test_valid_python_component(self):
        result, outputs = self.load({"version": 1, "components": [python_component(python_version="3.14.8")]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(outputs["components"])[0]["python_version"], "3.14.8")

    def test_python_component_requires_uv_lock(self):
        self.assert_fails(
            {"version": 1, "components": [python_component()]},
            "must contain uv.lock",
            files=("automation/pyproject.toml",),
        )

    def test_python_component_requires_pyproject(self):
        self.assert_fails(
            {"version": 1, "components": [python_component()]},
            "must contain pyproject.toml",
            files=("automation/uv.lock",),
        )

    def test_python_version_must_be_exact(self):
        self.assert_fails({"version": 1, "components": [python_component(python_version="3.14")]}, "exact x.y.z")

    def test_python_rejects_postgres_test(self):
        self.assert_fails({"version": 1, "components": [python_component(postgres_test=True)]}, "does not support postgres_test")

    def test_python_rejects_non_goose_migration(self):
        self.assert_fails({"version": 1, "components": [python_component(migration_engine="prisma")]}, "supports only the goose")

    def test_python_version_only_for_python(self):
        self.assert_fails(
            {"version": 1, "components": [{"name": "web", "type": "node", "path": "automation", "python_version": "3.14.8"}]},
            "is not a python component",
        )

    def test_valid_schedule_is_normalized_with_defaults(self):
        result, outputs = self.load(
            {"version": 1, "components": [python_component()], "services": [scheduled_service(cron="  0  15 * *   *  ")]}
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        service = json.loads(outputs["services"])[0]
        self.assertEqual(service["schedule"], {"cron": "0 15 * * *", "timezone": "Asia/Seoul", "timeout": "30m"})
        self.assertIs(service["expose"], False)

    def test_explicit_schedule_values_are_kept(self):
        result, outputs = self.load(
            {
                "version": 1,
                "components": [python_component()],
                "services": [scheduled_service(cron="*/15 0-6,22-23 1-31/2 1,6,12 0-7", timezone="UTC", timeout="6h")],
            }
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            json.loads(outputs["services"])[0]["schedule"],
            {"cron": "*/15 0-6,22-23 1-31/2 1,6,12 0-7", "timezone": "UTC", "timeout": "6h"},
        )

    def test_service_without_schedule_has_no_schedule_key(self):
        result, outputs = self.load(
            {"version": 1, "components": [python_component()], "services": [{"name": "sync", "path": "automation", "component": "sync", "expose": False}]}
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("schedule", json.loads(outputs["services"])[0])

    def test_bad_cron_expressions_fail(self):
        for cron in (
            "0 15 * *",
            "0 0 15 * * *",
            "@daily",
            "60 * * * *",
            "* 24 * * *",
            "* * 0 * *",
            "* * * 13 *",
            "* * * * 8",
            "* * * * MON",
            "* * * JAN *",
            "5/2 * * * *",
            "*/0 * * * *",
            "10-5 * * * *",
            "1,,2 * * * *",
            "1, * * * *",
            "? * * * *",
            "",
        ):
            with self.subTest(cron=cron):
                self.assert_fails(
                    {"version": 1, "components": [python_component()], "services": [scheduled_service(cron=cron)]},
                    "schedule.cron must be five numeric cron fields",
                )

    def test_cron_must_be_string(self):
        self.assert_fails(
            {"version": 1, "components": [python_component()], "services": [scheduled_service(cron=5)]},
            "schedule.cron is required and must be a string",
        )

    def test_schedule_requires_cron(self):
        service = scheduled_service()
        del service["schedule"]["cron"]
        self.assert_fails(
            {"version": 1, "components": [python_component()], "services": [service]},
            "schedule.cron is required",
        )

    def test_schedule_with_expose_true_fails(self):
        service = scheduled_service()
        service["expose"] = True
        self.assert_fails(
            {"version": 1, "components": [python_component()], "services": [service]},
            "must set expose: false",
        )

    def test_unknown_schedule_key_fails(self):
        self.assert_fails(
            {"version": 1, "components": [python_component()], "services": [scheduled_service(retries=3)]},
            "schedule must be an object containing only cron, timezone, and timeout",
        )

    def test_null_schedule_fails(self):
        service = scheduled_service()
        service["schedule"] = None
        self.assert_fails(
            {"version": 1, "components": [python_component()], "services": [service]},
            "schedule must be an object",
        )

    def test_bad_timezone_fails(self):
        for timezone in ("Asia/../Seoul", "Asia Seoul", "/UTC", "Asia/"):
            with self.subTest(timezone=timezone):
                self.assert_fails(
                    {"version": 1, "components": [python_component()], "services": [scheduled_service(timezone=timezone)]},
                    "schedule.timezone must be an IANA time zone name",
                )

    def test_timeout_bounds(self):
        for timeout in ("0m", "7h", "361m", "30s", "1d", "30", "05m"):
            with self.subTest(timeout=timeout):
                result, _ = self.load(
                    {"version": 1, "components": [python_component()], "services": [scheduled_service(timeout=timeout)]}
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("schedule.timeout", result.stdout)
        for timeout in ("1m", "360m", "6h", "1h"):
            with self.subTest(timeout=timeout):
                result, _ = self.load(
                    {"version": 1, "components": [python_component()], "services": [scheduled_service(timeout=timeout)]}
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class DetectTest(unittest.TestCase):
    def detect(self, components):
        workspace = Workspace(self)
        for component in components:
            workspace.write(f"{component['path']}/pyproject.toml")
            workspace.write(f"{component['path']}/uv.lock")
        return workspace.run(
            run_block("ci/detect", "Validate, detect, and normalize affected components"),
            {"COMPONENTS_JSON": json.dumps(components), "ARTIFACTS_JSON": "[]", "FORCE_ALL": "true", "EVENT_NAME": "push"},
        )

    def test_python_groups_use_default_and_explicit_versions(self):
        result, outputs = self.detect(
            [
                {"name": "a", "type": "python", "path": "a"},
                {"name": "b", "type": "python", "path": "b"},
                {"name": "c", "type": "python", "path": "c", "python_version": "3.13.9"},
            ]
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        groups = json.loads(outputs["ci_groups_by_target"])
        self.assertEqual(sorted(groups), ["Python 3.13.9", "Python 3.14.8"])
        default_group = groups["Python 3.14.8"]
        self.assertEqual(default_group["type"], "python")
        self.assertEqual(default_group["version"], "3.14.8")
        self.assertFalse(default_group["postgres_required"])
        self.assertEqual([component["name"] for component in default_group["components"]], ["a", "b"])

    def test_python_goose_component_requires_postgres(self):
        result, outputs = self.detect([{"name": "a", "type": "python", "path": "a", "migration_engine": "goose"}])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(outputs["ci_groups_by_target"])["Python 3.14.8"]["postgres_required"])


SERVICE_CONTRACT_VARIABLES = """
service="$(jq -r '.name' <<<"$SERVICE_JSON")"
migrate=false; migration_engine=none; component_path=services/api
migration_path=""; migration_schema=""; migration_config=""; migration_package=""
migration_package_manager=""; migration_build_tool=""; goose_version=""
"""

LEGACY_CONTRACT = (
    '{"name":"api","manual":false,"database":false,"database_scope":"project","migrate":false,'
    '"migration_engine":"none","component_path":"services/api","migration_path":"","migration_schema":"",'
    '"migration_config":"","migration_package":"","migration_package_manager":"","migration_build_tool":"",'
    '"goose_version":"","expose":%s}'
)


class ArtifactMetadataTest(unittest.TestCase):
    def contract(self, service, exposed_port=""):
        workspace = Workspace(self)
        script = (
            SERVICE_CONTRACT_VARIABLES
            + segment("cd/artifact", "Package immutable deployment bundle", "        if jq -e 'has(\"schedule\")'", '        source_digest=""')
            + segment("cd/artifact", "Package immutable deployment bundle", '        service_contract="$(', "        jq -n ")
            + 'printf "%s" "$service_contract" >"$RUNNER_TEMP/contract.json"\n'
        )
        result, _ = workspace.run(script, {"SERVICE_JSON": json.dumps(service), "EXPOSED_PORT": exposed_port})
        contract_file = workspace.temp / "contract.json"
        return result, contract_file.read_text(encoding="utf-8") if contract_file.exists() else ""

    def test_contract_without_schedule_is_byte_identical(self):
        for expose in (True, False):
            with self.subTest(expose=expose):
                result, contract = self.contract(
                    {"name": "api", "path": "services/api", "component": "api", "expose": expose, "manual": False,
                     "database": False, "database_scope": "project", "migrate": False, "watch": []}
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(contract, LEGACY_CONTRACT % json.dumps(expose))

    def test_contract_with_schedule(self):
        schedule = {"cron": "0 15 * * *", "timezone": "Asia/Seoul", "timeout": "30m"}
        result, contract = self.contract(
            {"name": "api", "path": "services/api", "component": "api", "expose": False, "manual": False,
             "database": False, "database_scope": "project", "migrate": False, "watch": [], "schedule": schedule}
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            contract,
            (LEGACY_CONTRACT % "false")[:-1] + ',"schedule":{"cron":"0 15 * * *","timezone":"Asia/Seoul","timeout":"30m"}}',
        )

    def test_scheduled_contract_rejects_exposed_port(self):
        result, _ = self.contract(
            {"name": "api", "expose": False, "schedule": {"cron": "0 15 * * *", "timezone": "Asia/Seoul", "timeout": "30m"}},
            exposed_port="8080",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not EXPOSE a port", result.stdout)

    def test_scheduled_contract_requires_normalized_schedule(self):
        result, _ = self.contract({"name": "api", "expose": False, "schedule": {"cron": "0 15 * * *"}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must carry a normalized schedule", result.stdout)


class ImageExposeTest(unittest.TestCase):
    def check(self, service, exposed_ports, healthcheck=True):
        workspace = Workspace(self)
        config = {"config": {"ExposedPorts": {port: {} for port in exposed_ports}}}
        if healthcheck:
            config["config"]["Healthcheck"] = {"Test": ["CMD", "true"]}
        config_blob = workspace.temp / "config.json"
        config_blob.write_text(json.dumps(config), encoding="utf-8")
        script = (
            'service="$(jq -r .name <<<"$SERVICE_JSON")"\n'
            f'config_blob="{config_blob}"\n'
            + segment("cd/docker/prepare", "Build and scan OCI artifact", "        exposed=", "        trivy_version=")
        )
        return workspace.run(script, {"SERVICE_JSON": json.dumps(service)})[0]

    def test_scheduled_image_without_expose_passes(self):
        result = self.check(scheduled_service(), [], healthcheck=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_scheduled_image_with_expose_fails(self):
        result = self.check(scheduled_service(), ["8080/tcp"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not EXPOSE any port", result.stdout)

    def test_regular_image_with_one_port_and_healthcheck_passes(self):
        result = self.check({"name": "api", "expose": True}, ["8080/tcp"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipIf(os.environ.get("WORKFLOWS_SKIP_NETWORK_TESTS") == "1", "network-dependent Python toolchain test disabled")
class PythonCITest(unittest.TestCase):
    def run_ci(self, mutate=None):
        workspace = Workspace(self)
        shutil.copytree(FIXTURES / "python-app", workspace.repo / "automation")
        if mutate:
            mutate(workspace.repo / "automation")
        components = [{"name": "sync", "type": "python", "path": "automation", "python_version": "3.14.8"}]
        return workspace.run(
            run_block("ci/python", "Sync, compile, and test affected Python components"),
            {"COMPONENTS_JSON": json.dumps(components), "PYTHON_VERSION": "3.14.8", "CI_PRIVATE_REPO_TOKEN": ""},
        )[0]

    def test_locked_project_passes(self):
        result = self.run_ci()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 1 test", result.stderr)

    def test_no_tests_is_a_notice(self):
        result = self.run_ci(lambda path: (path / "test_greeting.py").unlink())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("::notice::Python component 'sync' has no unittest tests", result.stdout)

    def test_failing_test_fails(self):
        def break_module(path):
            (path / "greeting.py").write_text("def greet(name):\n    return name\n", encoding="utf-8")

        result = self.run_ci(break_module)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unittest failed", result.stdout)

    def test_syntax_error_fails_compileall(self):
        result = self.run_ci(lambda path: (path / "broken.py").write_text("def broken(:\n", encoding="utf-8"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error compiling './broken.py'", result.stdout)

    def test_stale_lock_fails(self):
        def add_dependency(path):
            pyproject = path / "pyproject.toml"
            pyproject.write_text(pyproject.read_text(encoding="utf-8").replace("dependencies = []", 'dependencies = ["six"]'), encoding="utf-8")

        result = self.run_ci(add_dependency)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--locked", result.stderr)

    def test_missing_lock_fails(self):
        result = self.run_ci(lambda path: (path / "uv.lock").unlink())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must contain uv.lock", result.stdout)


if __name__ == "__main__":
    unittest.main()
