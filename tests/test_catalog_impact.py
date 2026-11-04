"""Gradle version catalog narrowing in the affected-component detector (ci/detect)."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from test_stacks_contract import ACTIONS, FIXTURES, Workspace, run_block


SCRIPT = ACTIONS / "ci/detect/catalog_impact.py"
CATALOG_FIXTURE = FIXTURES / "gradle-catalog"
DETECT_STEP = "Validate, detect, and normalize affected components"

_spec = importlib.util.spec_from_file_location("catalog_impact", SCRIPT)
catalog_impact = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(catalog_impact)
FailSafe = catalog_impact.FailSafe


def seg(alias):
    return catalog_impact.segments(alias)


def changes(**kinds):
    result = {kind: set() for kind in catalog_impact.SECTIONS}
    for kind, aliases in kinds.items():
        result[kind] = {seg(alias) for alias in aliases}
    return result


def parse(text):
    return catalog_impact.parse_catalog(text, "test")


class ChangedAliasesTest(unittest.TestCase):
    BASE = """
[versions]
core = "1.0"
other = "2.0"

[libraries]
lib-a = { module = "g:a", version.ref = "core" }
lib-b = { module = "g:b", version.ref = "other" }
lib-c = "g:c:3.0"

[plugins]
plug = { id = "p", version.ref = "core" }
plug-other = { id = "q", version.ref = "other" }

[bundles]
with-a = ["lib-a", "lib-c"]
without-a = ["lib-b"]
"""

    def changed(self, head):
        changed, _ = catalog_impact.changed_aliases(parse(self.BASE), parse(head))
        return changed

    def test_version_change_propagates_through_version_ref_and_bundles(self):
        changed = self.changed(self.BASE.replace('core = "1.0"', 'core = "1.1"'))
        self.assertEqual(changed, changes(versions=["core"], libraries=["lib-a"], plugins=["plug"], bundles=["with-a"]))

    def test_direct_library_change_marks_its_bundles(self):
        changed = self.changed(self.BASE.replace("g:c:3.0", "g:c:3.1"))
        self.assertEqual(changed, changes(libraries=["lib-c"], bundles=["with-a"]))

    def test_added_and_removed_aliases_are_changed(self):
        head = self.BASE.replace('lib-c = "g:c:3.0"\n', 'lib-d = "g:d:1.0"\n').replace('"lib-a", "lib-c"', '"lib-a"')
        changed = self.changed(head)
        self.assertEqual(changed, changes(libraries=["lib-c", "lib-d"], bundles=["with-a"]))

    def test_comment_and_formatting_changes_change_nothing(self):
        head = "# reformatted\n" + self.BASE.replace('core = "1.0"', 'core   =   "1.0"   # same')
        self.assertEqual(self.changed(head), changes())

    def test_moved_version_ref_marks_library(self):
        changed = self.changed(self.BASE.replace('module = "g:b", version.ref = "other"', 'module = "g:b", version.ref = "core"'))
        self.assertEqual(changed, changes(libraries=["lib-b"], bundles=["without-a"]))

    def test_unsafe_catalogs_fail_safe(self):
        cases = {
            "invalid toml": "[versions\n",
            "unknown table": self.BASE + "\n[extras]\nx = 1\n",
            "bundle not a list": self.BASE.replace('without-a = ["lib-b"]', 'without-a = "lib-b"'),
            "library not a table or string": self.BASE.replace('lib-c = "g:c:3.0"', "lib-c = 3"),
        }
        for label, text in cases.items():
            with self.subTest(label):
                with self.assertRaises(FailSafe):
                    parse(text)

    def test_metadata_change_fails_safe(self):
        with self.assertRaises(FailSafe):
            self.changed('[metadata]\nformat.version = "1.1"\n' + self.BASE)


class ReferencesTest(unittest.TestCase):
    KNOWN = {
        "libraries": {seg(a) for a in ("androidx-core", "androidx-core-ktx", "androidx-media3-exoplayer", "androidx-media3-exoplayer-dash")},
        "plugins": {seg("android-application")},
        "versions": {seg("kotlin")},
        "bundles": {seg("media3")},
    }

    def hits(self, text, changed, roots=("libs",)):
        return list(catalog_impact.references(text, set(roots), changed, self.KNOWN))

    def test_matching_rule(self):
        core = changes(libraries=["androidx-core"])
        core_ktx = changes(libraries=["androidx-core-ktx"])
        exoplayer = changes(libraries=["androidx-media3-exoplayer"])
        cases = [
            # (text, changed, expected accessors)
            ("implementation(libs.androidx.core.ktx)", core, []),
            ("implementation(libs.androidx.core.ktx)", core_ktx, ["libs.androidx.core.ktx"]),
            ("implementation(libs.androidx.core)", core, ["libs.androidx.core"]),
            ("implementation(libs.androidx.core)", core_ktx, []),
            ("libs.androidx.core.get().version", core, ["libs.androidx.core"]),
            ("implementation(libs.androidx.media3.exoplayer.dash)", exoplayer, []),
            ("implementation(libs.androidx.media3.exoplayer.asProvider())", exoplayer, ["libs.androidx.media3.exoplayer"]),
            ("implementation(\n    libs.androidx\n        .media3.exoplayer\n)", exoplayer, ["libs.androidx.media3.exoplayer"]),
            ("implementation(libs.androidx.media3)", exoplayer, []),
            ("implementation(mylibs.androidx.core)", core, []),
            ("implementation(LIBS_androidx.core)", core, []),
            ('implementation(libs.findLibrary("androidx-core").get())', core, ['findLibrary("androidx-core")']),
            ('implementation(libs.findLibrary("androidx.core.ktx").get())', core, []),
            ("alias(libs.plugins.android.application)", changes(plugins=["android-application"]), ["libs.plugins.android.application"]),
            ("implementation(libs.android.application)", changes(plugins=["android-application"]), []),
            ('"x:${libs.versions.kotlin.get()}"', changes(versions=["kotlin"]), ["libs.versions.kotlin"]),
            ("implementation(libs.bundles.media3)", changes(bundles=["media3"]), ["libs.bundles.media3"]),
            ("implementation(libs.bundles.media3)", exoplayer, []),
            ('files("../gradle/libs.versions.toml")', changes(versions=["kotlin"]), []),
        ]
        for text, changed, expected in cases:
            with self.subTest(text):
                self.assertEqual(self.hits(text, changed), expected)

    def test_case_insensitive_segments(self):
        self.assertEqual(self.hits("libs.AndroidX.Core", changes(libraries=["androidx-core"])), ["libs.AndroidX.Core".lower()])

    def test_catalog_root_comes_from_the_caller(self):
        core = changes(libraries=["androidx-core"])
        self.assertEqual(self.hits("deps.androidx.core", core, roots=("deps",)), ["deps.androidx.core"])
        self.assertEqual(self.hits("deps.androidx.core", core), [])

    def test_dynamic_lookup_fails_safe(self):
        with self.assertRaises(FailSafe):
            self.hits("libs.findLibrary(name).get()", changes(libraries=["androidx-core"]))


class CatalogRootsTest(unittest.TestCase):
    def roots(self, texts, catalog="gradle/libs.versions.toml"):
        return catalog_impact.catalog_roots(catalog, texts)

    def test_names_from_settings(self):
        cases = [
            ('create("libs") { from(files("../gradle/libs.versions.toml")) }', {"libs"}),
            ('create("deps") {\n    from(files("../gradle/libs.versions.toml"))\n}', {"deps"}),
            ("libs { from files('../gradle/libs.versions.toml') }", {"libs"}),
            ('create("other") { from(files("../gradle/other.versions.toml")) }', {"libs"}),
            ('create("pub") { from("com.example:catalog:1.0") }', {"libs"}),
        ]
        for settings, expected in cases:
            with self.subTest(settings):
                text = "dependencyResolutionManagement { versionCatalogs { " + settings + " } }"
                self.assertEqual(self.roots({"app/settings.gradle.kts": text}), expected)

    def test_gradle_default_import(self):
        self.assertEqual(self.roots({"settings.gradle.kts": 'rootProject.name = "x"'}), {"libs"})
        self.assertEqual(self.roots({"app/build.gradle.kts": ""}), {"libs"})

    def test_undeterminable_names_fail_safe(self):
        cases = [
            ({"app/build.gradle.kts": ""}, "gradle/deps.versions.toml"),
            ({"app/settings.gradle.kts": 'versionCatalogs { create("deps") { from(files(catalogPath)) } }'}, "gradle/libs.versions.toml"),
            ({"app/settings.gradle.kts": 'val x = files("../gradle/libs.versions.toml")'}, "gradle/libs.versions.toml"),
        ]
        for texts, catalog in cases:
            with self.subTest(texts=texts, catalog=catalog):
                with self.assertRaises(FailSafe):
                    self.roots(texts, catalog)


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    result = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def fixture_components():
    """The mooner-webapps shape: every Android build watches platform/ and the shared catalog."""
    components = [{"name": "platform", "type": "java-kotlin", "path": "platform", "build_tool": "gradle", "watch": ["gradle/libs.versions.toml"]}]
    for app in ("rutc", "navi", "ssh"):
        components.append(
            {
                "name": f"{app}-android",
                "type": "java-kotlin",
                "path": f"apps/{app}/android",
                "build_tool": "gradle",
                "profile": "android",
                "java_version": "21",
                "watch": ["platform", "gradle/libs.versions.toml"],
            }
        )
    return components


class DetectCatalogTest(unittest.TestCase):
    BASE = (CATALOG_FIXTURE / "base.versions.toml").read_text(encoding="utf-8")
    HEAD = (CATALOG_FIXTURE / "head.versions.toml").read_text(encoding="utf-8")

    def detect(self, head_catalog, base_catalog=BASE, extra_changes=None, base_files=None, helper=True, event="push"):
        workspace = Workspace(self)
        repo = workspace.repo
        shutil.copytree(CATALOG_FIXTURE / "repo", repo, dirs_exist_ok=True)
        git(repo, "init", "-q", "-b", "master")
        if base_catalog is not None:
            workspace.write("gradle/libs.versions.toml", base_catalog)
        for relative, content in (base_files or {}).items():
            workspace.write(relative, content)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        base = git(repo, "rev-parse", "HEAD")
        catalog = repo / "gradle/libs.versions.toml"
        if head_catalog is None:
            catalog.unlink()
        else:
            workspace.write("gradle/libs.versions.toml", head_catalog)
        for relative, content in (extra_changes or {}).items():
            workspace.write(relative, content)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "head")
        head = git(repo, "rev-parse", "HEAD")

        env = {
            "COMPONENTS_JSON": json.dumps(fixture_components()),
            "ARTIFACTS_JSON": "[]",
            "FORCE_ALL": "false",
            "EVENT_NAME": event,
            "HEAD_SHA": head,
        }
        if event == "push":
            env["PUSH_BEFORE_SHA"] = base
        else:
            env["PR_BASE_SHA"] = base
        if helper:
            env["CATALOG_IMPACT_SCRIPT"] = str(SCRIPT)
        result, outputs = workspace.run(run_block("ci/detect", DETECT_STEP), env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = result.stdout + result.stderr
        return sorted(component["name"] for component in json.loads(outputs["all_components"])), log

    ALL = ["navi-android", "platform", "rutc-android", "ssh-android"]

    def test_pr15_catalog_additions_select_only_rutc(self):
        selected, log = self.detect(self.HEAD)
        self.assertEqual(selected, ["rutc-android"], log)
        self.assertIn("catalog: narrowed to [rutc-android] (dropped: [platform navi-android ssh-android])", log)
        self.assertIn("catalog: rutc-android: kept (apps/rutc/android/app/build.gradle.kts uses libs.", log)
        self.assertIn("catalog: navi-android: dropped", log)

    def test_pull_request_event_uses_the_merge_base(self):
        selected, log = self.detect(self.HEAD, event="pull_request")
        self.assertEqual(selected, ["rutc-android"], log)

    def test_app_only_version_bump_selects_that_app(self):
        selected, log = self.detect(self.BASE.replace('sshj = "0.41.2"', 'sshj = "0.42.0"'))
        self.assertEqual(selected, ["ssh-android"], log)

    def test_shared_library_used_by_platform_selects_every_watcher(self):
        # platform/android-core uses kotlinx-coroutines-android; every app watches platform/.
        selected, log = self.detect(self.BASE.replace('coroutines = "1.11.0"', 'coroutines = "1.12.0"'))
        self.assertEqual(selected, self.ALL, log)

    def test_version_read_by_build_logic_selects_every_watcher(self):
        # ksp is read as libs.versions.ksp in platform/build-logic and as a plugin in platform/.
        selected, log = self.detect(self.BASE.replace('ksp = "2.3.12"', 'ksp = "2.3.13"'))
        self.assertEqual(selected, self.ALL, log)

    def test_alias_with_longer_sibling_matches_exactly(self):
        # Only androidx-media3-exoplayer changes; rutc also uses androidx-media3-exoplayer-dash, which stays.
        base = self.HEAD
        head = base.replace(
            'androidx-media3-exoplayer = { module = "androidx.media3:media3-exoplayer", version.ref = "media3" }',
            'androidx-media3-exoplayer = { module = "androidx.media3:media3-exoplayer", version = "1.11.2" }',
        )
        selected, log = self.detect(head, base_catalog=base)
        self.assertEqual(selected, ["rutc-android"], log)
        self.assertIn("uses libs.androidx.media3.exoplayer)", log)

    def test_comment_only_change_selects_nothing(self):
        selected, log = self.detect(self.BASE + "\n# trailing comment\n")
        self.assertEqual(selected, [], log)
        self.assertIn("catalog: narrowed to [] (dropped: [platform rutc-android navi-android ssh-android])", log)

    def test_other_watched_changes_still_select_directly(self):
        selected, log = self.detect(self.HEAD, extra_changes={"apps/navi/android/app/src/Main.kt": "class Main\n"})
        self.assertEqual(selected, ["navi-android", "rutc-android"], log)
        self.assertIn("catalog: narrowed to [rutc-android] (dropped: [platform ssh-android])", log)

    def test_workflow_change_still_selects_everything(self):
        selected, log = self.detect(self.HEAD, extra_changes={".github/workflows/ci.yml": "on: push\n"})
        self.assertEqual(selected, self.ALL, log)
        self.assertNotIn("catalog:", log)

    def test_fail_safe_keeps_every_watcher(self):
        cases = {
            "invalid toml": ({"head_catalog": self.HEAD + "\n[versions\n"}, "does not parse as TOML"),
            "catalog added": ({"head_catalog": self.HEAD, "base_catalog": None}, "is missing at"),
            "catalog removed": ({"head_catalog": None}, "is missing at"),
            "helper missing": ({"head_catalog": self.HEAD, "helper": False}, "catalog impact helper not found"),
            "metadata change": ({"head_catalog": '[metadata]\nformat.version = "1.1"\n' + self.HEAD}, "[metadata] changed"),
            "dynamic lookup": (
                {"head_catalog": self.HEAD, "base_files": {"platform/build-logic/src/Conv.kt": "val lib = libs.findLibrary(alias)\n"}},
                "dynamic catalog lookup",
            ),
        }
        for label, (kwargs, reason) in cases.items():
            with self.subTest(label):
                selected, log = self.detect(**kwargs)
                self.assertEqual(selected, self.ALL, log)
                self.assertIn("fail-safe (", log)
                self.assertIn(reason, log)


if __name__ == "__main__":
    unittest.main()
