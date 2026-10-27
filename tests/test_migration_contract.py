"""Deploy-time migration contract: loader gates, Flyway/Goose source packaging, CI file rules."""

import hashlib
import json
import os
from pathlib import Path
import tarfile
import unittest

from test_stacks_contract import ACTIONS, Workspace, run_block, segment


def go_service(name, **extra):
    service = {"name": name, "path": "api", "component": "api", "expose": False, "migrate": True}
    service.update(extra)
    return service


class MigrationLoaderTest(unittest.TestCase):
    def load(self, components, services):
        workspace = Workspace(self)
        for relative, body in (("api/go.mod", "module x\n"), ("api/Dockerfile", ""), ("web/package.json", "{}"),
                               ("web/pnpm-lock.yaml", ""), ("web/Dockerfile", "")):
            workspace.write(relative, body)
        workspace.write(".github/stacks.yml", json.dumps({"version": 1, "components": components, "services": services}))
        return workspace.run(run_block("config/load", "Load project contract"))

    def test_single_project_scope_migrator_passes(self):
        result, _ = self.load([{"name": "api", "type": "go", "path": "api", "migration_engine": "goose"}], [go_service("api")])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_two_project_scope_migrators_fail(self):
        result, _ = self.load(
            [{"name": "api", "type": "go", "path": "api", "migration_engine": "goose"}],
            [go_service("api"), go_service("worker")],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Only one migrate: true service may use database_scope: project", result.stdout)

    def test_service_scope_migrators_are_independent(self):
        result, _ = self.load(
            [{"name": "api", "type": "go", "path": "api", "migration_engine": "goose"}],
            [go_service("api", database_scope="service"), go_service("worker", database_scope="service"), go_service("third")],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_prisma_migrate_fails(self):
        result, _ = self.load(
            [{"name": "web", "type": "node", "path": "web", "package_manager": "pnpm", "migration_engine": "prisma"}],
            [{"name": "web", "path": "web", "component": "web", "expose": False, "migrate": True}],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deploy-time migrations support only goose and flyway", result.stdout)


PACKAGE_VARIABLES = 'target="$RUNNER_TEMP/bundle"; mkdir -p "$target"\n'


class MigrationSourcePackagingTest(unittest.TestCase):
    def package(self, workspace, engine, component_path="app", migration_path=""):
        script = (
            PACKAGE_VARIABLES
            + segment("cd/artifact", "Package immutable deployment bundle",
                      '        if [[ "$migrate" == "true" && -z "$migration_path" ]]; then', '        target="$GHARP_ARTIFACT_SPOOL')
            + segment("cd/artifact", "Package immutable deployment bundle", '        source_digest=""', '        service_contract="$(')
            + 'printf "%s\\n%s" "$source_digest" "$migration_path" >"$RUNNER_TEMP/result"\n'
        )
        env = {"migrate": "true", "migration_engine": engine, "component_path": component_path, "migration_path": migration_path}
        result, _ = workspace.run("".join(f'{k}="{v}"\n' for k, v in env.items()) + script)
        archive = workspace.temp / "bundle" / "source.tar.gz"
        info = (workspace.temp / "result").read_text(encoding="utf-8").split("\n") if result.returncode == 0 else ["", ""]
        return result, archive, info[0], info[1]

    def flyway_workspace(self, files):
        workspace = Workspace(self)
        for name, body in files.items():
            workspace.write(f"app/src/main/resources/db/migration/{name}", body)
        workspace.write("app/build.gradle.kts", "")
        return workspace

    def test_flyway_packages_only_sql_files_with_default_path(self):
        workspace = self.flyway_workspace({"V1__init.sql": "create table t();", "R__views.sql": "select 1;"})
        result, archive, digest, path = self.package(workspace, "flyway")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(path, "src/main/resources/db/migration")
        self.assertTrue(digest.startswith("sha256:"))
        self.assertEqual(digest, "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest())
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
        self.assertEqual(
            sorted(m.name for m in members),
            ["app/src/main/resources/db/migration/R__views.sql", "app/src/main/resources/db/migration/V1__init.sql"],
        )
        self.assertTrue(all(m.isfile() and m.uid == 0 and m.gid == 0 and m.mtime == 0 for m in members))

    def test_flyway_rejections(self):
        cases = {
            "undo": ({"V1__init.sql": "x", "U1__init.sql": "x"}, "Unsupported Flyway migration file"),
            "baseline": ({"V1__init.sql": "x", "B1__base.sql": "x"}, "Unsupported Flyway migration file"),
            "callback": ({"V1__init.sql": "x", "afterMigrate.sql": "x"}, "Unsupported Flyway migration file"),
            "java": ({"V1__init.sql": "x", "V2__Code.java": "x"}, "Unsupported Flyway migration file"),
            "empty": ({"V1__init.sql": ""}, "Empty Flyway migration"),
            "duplicate": ({"V1__a.sql": "x", "V1__b.sql": "x"}, "Duplicate Flyway version"),
            "repeatable only": ({"R__views.sql": "x"}, "requires at least one V<version>"),
            "subdirectory": ({"V1__init.sql": "x", "nested/V2__x.sql": "x"}, "must contain only files"),
        }
        for name, (files, message) in cases.items():
            with self.subTest(name):
                result, _, _, _ = self.package(self.flyway_workspace(files), "flyway")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout)

    def test_flyway_symlink_and_missing_directory_fail(self):
        workspace = self.flyway_workspace({"V1__init.sql": "x"})
        os.symlink("V1__init.sql", workspace.repo / "app/src/main/resources/db/migration/V2__link.sql")
        result, _, _, _ = self.package(workspace, "flyway")
        self.assertIn("must not contain symlinks", result.stdout)
        workspace = Workspace(self)
        workspace.write("app/build.gradle.kts", "")
        result, _, _, _ = self.package(workspace, "flyway")
        self.assertIn("Migration directory is missing", result.stdout)

    def test_source_digest_is_reproducible_across_checkouts(self):
        for engine, files in (
            ("flyway", {"app/src/main/resources/db/migration/V1__init.sql": "create table t();"}),
            ("goose", {"app/db/migrations/00001_init.sql": "-- +goose Up\nselect 1;\n", "app/go.mod": "module x\n"}),
        ):
            with self.subTest(engine):
                digests = []
                for mtime in (1_000_000_000, 1_700_000_000):
                    workspace = Workspace(self)
                    for relative, body in files.items():
                        target = workspace.write(relative, body)
                        os.utime(target, (mtime, mtime))
                    result, _, digest, _ = self.package(workspace, engine)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    digests.append(digest)
                self.assertEqual(digests[0], digests[1])

    def test_unsupported_engine_fails(self):
        workspace = Workspace(self)
        workspace.write("app/prisma/migrations/x.sql", "x")
        result, _, _, _ = self.package(workspace, "prisma", migration_path="prisma/migrations")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Deploy-time migration engine 'prisma' is unsupported", result.stdout)


class MigrationCIRulesTest(unittest.TestCase):
    def test_flyway_ci_rejects_undo_and_baseline_names(self):
        text = (ACTIONS / "ci/migration/flyway/action.yml").read_text(encoding="utf-8")
        self.assertNotIn("B[^_]+__", text)
        self.assertNotIn("U[^_]+__", text)
        self.assertIn("^R__[A-Za-z0-9][A-Za-z0-9_.-]*\\.sql$", text)

    def test_go_command_remnant_is_gone(self):
        for action in ("ci/detect", "cd/artifact", "config/load"):
            self.assertNotIn("go-command", (ACTIONS / action / "action.yml").read_text(encoding="utf-8"), action)


if __name__ == "__main__":
    unittest.main()
