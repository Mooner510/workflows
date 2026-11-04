#!/usr/bin/env python3
"""Decide whether a Gradle version catalog change really affects one component.

The detector calls this only for a component whose sole reason to be affected is a
changed ``*.versions.toml`` file it watches. It prints exactly one decision on stdout:

* ``keep`` - the component references a changed alias, or the answer is uncertain (fail-safe);
* ``drop`` - no file the component builds from references any changed alias.

Diagnostics go to stderr as ``catalog: <component>: ...`` lines.

Changed aliases come from parsing the catalog at the diff base and at the head revision
(not from patch lines). A changed ``[versions]`` key also changes every library and plugin
whose ``version.ref`` points at it, and a changed library changes every bundle listing it.

Reference matching rule. The scanned files are the tracked ``*.gradle``, ``*.kts``, ``*.kt``,
``*.groovy`` and ``*.java`` files under the component path and its watch entries (so
convention plugins and included build-logic builds the component watches are covered).
Every maximal dotted chain ``<catalog>.<seg>.<seg>...`` (whitespace around dots allowed,
``<catalog>`` not preceded by an identifier character) is read as follows:

* ``<catalog>.plugins.*``, ``<catalog>.versions.*`` and ``<catalog>.bundles.*`` address the
  plugin, version and bundle namespaces; anything else addresses libraries;
* the chain refers to the alias whose accessor (alias split on ``-``, ``_`` and ``.``,
  compared case-insensitively) is the LONGEST segment prefix of the chain among all aliases
  of that namespace in the base or head catalog. ``libs.androidx.core.ktx`` therefore means
  ``androidx-core-ktx`` when that alias exists, never ``androidx-core``; trailing segments
  that are not part of a longer alias (``.get()``, ``.asProvider()``) are ignored.

String lookups ``findLibrary("a-b")`` / ``findPlugin`` / ``findVersion`` / ``findBundle`` are
matched by alias as well; a lookup with a non-literal argument keeps the component.

Fail-safe (``keep``): Python without tomllib, a diff base or head revision that is missing,
a catalog added/removed/renamed in the diff, TOML that does not parse or has an unexpected
shape, a ``[metadata]`` change, an unreadable file, or a catalog accessor name that cannot be
determined. The name comes from a ``create("<name>") { from(files("<literal>")) }`` block in a
scanned settings file, or from Gradle's own ``<settings dir>/gradle/libs.versions.toml`` import;
with no such settings file only ``libs.versions.toml`` defaults to ``libs``. A settings file that
declares a catalog from a non-literal source (a variable, a computed path) fails safe.
"""

import argparse
import json
import os
import posixpath
import re
import subprocess
import sys

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

SECTIONS = ("versions", "libraries", "plugins", "bundles")
ACCESSOR_NAMESPACE = {"libraries": None, "plugins": "plugins", "versions": "versions", "bundles": "bundles"}
NAMESPACE_KIND = {"plugins": "plugins", "versions": "versions", "bundles": "bundles"}
SCANNED_SUFFIXES = (".gradle", ".kts", ".kt", ".groovy", ".java")
SETTINGS_NAMES = ("settings.gradle", "settings.gradle.kts")
ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*$")
FIND_LITERAL_RE = re.compile(r"\bfind(Library|Plugin|Version|Bundle)\s*\(\s*([\"'])([^\"'\n]*)\2\s*\)")
FIND_ANY_RE = re.compile(r"\bfind(?:Library|Plugin|Version|Bundle)\s*\(")
FIND_KIND = {"Library": "libraries", "Plugin": "plugins", "Version": "versions", "Bundle": "bundles"}
CATALOG_BLOCK_RE = re.compile(
    r"(?:\b(?:create|register)\s*\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\)|\b([A-Za-z_][A-Za-z0-9_]*))"
    r"\s*\{\s*from\s*\(?\s*files\s*\(\s*[\"']([^\"'\n]+)[\"']"
)
CATALOG_FILES_RE = re.compile(r"\bfiles\s*\(\s*[\"']([^\"'\n]+\.versions\.toml)[\"']")
CATALOG_FROM_RE = re.compile(r"\bfrom\s*\(\s*|\bfrom\s+(?=files\b)")
CATALOG_SOURCE_OK_RE = re.compile(r"[\"']|files\s*\(\s*[\"'][^\"'\n]+[\"']\s*\)")
MAX_LISTED = 12


class FailSafe(Exception):
    """The impact cannot be narrowed safely; the component stays affected."""


def segments(alias):
    return tuple(part.lower() for part in re.split(r"[-_.]", alias))


def accessor(root, kind, segs):
    namespace = ACCESSOR_NAMESPACE[kind]
    return ".".join([root] + ([namespace] if namespace else []) + list(segs))


def git_show(workspace, rev, path):
    result = subprocess.run(
        ["git", "-C", workspace, "show", f"{rev}:{path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise FailSafe(f"{path} is missing at {rev[:12]} (added, removed, renamed, or base unavailable)")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FailSafe(f"{path} at {rev[:12]} is not UTF-8") from error


def parse_catalog(text, label):
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise FailSafe(f"{label} does not parse as TOML: {error}") from error
    unknown = sorted(set(data) - set(SECTIONS) - {"metadata"})
    if unknown:
        raise FailSafe(f"{label} has unsupported top-level keys {unknown}")
    for section in SECTIONS:
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise FailSafe(f"{label} [{section}] is not a table")
        for alias, value in table.items():
            if not ALIAS_RE.match(alias):
                raise FailSafe(f"{label} [{section}] alias {alias!r} is not a valid catalog alias")
            if section == "bundles":
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise FailSafe(f"{label} bundle {alias!r} is not a list of library aliases")
            elif not isinstance(value, (str, dict)):
                raise FailSafe(f"{label} [{section}] {alias!r} has an unsupported value")
    return data


def version_ref(entry):
    if isinstance(entry, dict):
        version = entry.get("version")
        if isinstance(version, dict) and isinstance(version.get("ref"), str):
            return segments(version["ref"])
    return None


def changed_aliases(base, head):
    """Return ({kind: {segments}} changed, {kind: {segments}} known)."""
    if base.get("metadata") != head.get("metadata"):
        raise FailSafe("[metadata] changed")
    changed = {kind: set() for kind in SECTIONS}
    known = {kind: set() for kind in SECTIONS}
    for kind in SECTIONS:
        old, new = base.get(kind, {}), head.get(kind, {})
        for alias in set(old) | set(new):
            known[kind].add(segments(alias))
            if old.get(alias) != new.get(alias):
                changed[kind].add(segments(alias))
    for kind in ("libraries", "plugins"):
        for catalog in (base, head):
            for alias, entry in catalog.get(kind, {}).items():
                if version_ref(entry) in changed["versions"]:
                    changed[kind].add(segments(alias))
    for catalog in (base, head):
        for alias, members in catalog.get("bundles", {}).items():
            if any(segments(member) in changed["libraries"] for member in members):
                changed["bundles"].add(segments(alias))
    return changed, known


def scan_files(workspace, roots):
    if not roots:
        return []
    result = subprocess.run(
        ["git", "--literal-pathspecs", "-C", workspace, "ls-files", "-z", "--", *roots],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise FailSafe("git ls-files failed for the component scan set")
    files = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", "surrogateescape")
        if relative.endswith(SCANNED_SUFFIXES):
            files.append(relative)
    return sorted(set(files))


def read_text(workspace, relative):
    workspace_real = os.path.realpath(workspace)
    target = os.path.realpath(os.path.join(workspace, relative))
    if target != workspace_real and not target.startswith(workspace_real + os.sep):
        raise FailSafe(f"{relative} resolves outside the repository")
    if not os.path.isfile(target):
        return ""  # Tracked but deleted in the working tree: nothing to reference.
    try:
        with open(target, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as error:
        raise FailSafe(f"cannot read {relative}: {error}") from error


def catalog_roots(catalog_path, texts):
    """Return the accessor names (e.g. {"libs"}) the scanned settings files give this catalog."""
    names = set()
    target = posixpath.normpath(catalog_path)
    for relative, text in texts.items():
        if posixpath.basename(relative) not in SETTINGS_NAMES:
            continue
        directory = posixpath.dirname(relative)
        if posixpath.normpath(posixpath.join(directory, "gradle", "libs.versions.toml")) == target:
            names.add("libs")  # Gradle imports <settings dir>/gradle/libs.versions.toml as `libs`.
        named_positions = set()
        for match in CATALOG_BLOCK_RE.finditer(text):
            name = match.group(1) or match.group(2)
            if posixpath.normpath(posixpath.join(directory, match.group(3))) == target:
                names.add(name)
                named_positions.add(match.start(3))
        for match in CATALOG_FILES_RE.finditer(text):
            if posixpath.normpath(posixpath.join(directory, match.group(1))) == target and match.start(1) not in named_positions:
                raise FailSafe(f"{relative} imports {catalog_path} without a recognizable catalog name")
        if "versionCatalogs" in text:
            # from(files("<literal>")) is resolved above and from("group:artifact:version") is a published
            # catalog; any other source (a variable, a computed path) might be this file under any name.
            for match in CATALOG_FROM_RE.finditer(text):
                if not CATALOG_SOURCE_OK_RE.match(text, match.end()):
                    raise FailSafe(f"{relative} has a version catalog source that is not a literal path")
    if not names:
        if posixpath.basename(catalog_path) != "libs.versions.toml":
            raise FailSafe(f"no scanned settings file names the catalog {catalog_path}")
        names.add("libs")
    return names


def references(text, roots, changed, known):
    """Yield accessor strings in text that resolve to a changed alias."""
    root_pattern = "|".join(re.escape(root) for root in sorted(roots))
    chain_re = re.compile(rf"(?<![A-Za-z0-9_$])({root_pattern})((?:\s*\.\s*[A-Za-z_][A-Za-z0-9_]*)+)")
    for match in chain_re.finditer(text):
        segs = [part.strip().lower() for part in match.group(2).split(".")[1:]]
        kind = NAMESPACE_KIND.get(segs[0], "libraries")
        if kind != "libraries":
            segs = segs[1:]
        for length in range(len(segs), 0, -1):
            candidate = tuple(segs[:length])
            if candidate in known[kind]:
                if candidate in changed[kind]:
                    yield accessor(match.group(1), kind, candidate)
                break
    for match in FIND_LITERAL_RE.finditer(text):
        kind = FIND_KIND[match.group(1)]
        if segments(match.group(3)) in changed[kind]:
            yield f'find{match.group(1)}("{match.group(3)}")'
    literal_starts = {match.start() for match in FIND_LITERAL_RE.finditer(text)}
    for match in FIND_ANY_RE.finditer(text):
        if match.start() not in literal_starts:
            raise FailSafe("dynamic catalog lookup with a non-literal alias")


def summarize(items):
    items = sorted(items)
    if len(items) > MAX_LISTED:
        return ", ".join(items[:MAX_LISTED]) + f", ... (+{len(items) - MAX_LISTED} more)"
    return ", ".join(items) if items else "(none)"


def decide(workspace, base, head, component, catalogs, announce):
    if tomllib is None:
        raise FailSafe("python3 >= 3.11 (tomllib) is unavailable")
    path = component["path"]
    watches = [watch.rstrip("/") or "." for watch in component.get("watch") or []]
    roots = sorted({path.rstrip("/") or "."} | {watch for watch in watches if not watch.endswith(".versions.toml")})
    files = scan_files(workspace, roots)
    texts = {relative: read_text(workspace, relative) for relative in files}

    for catalog_path in catalogs:
        old = parse_catalog(git_show(workspace, base, catalog_path), f"{catalog_path}@base")
        new = parse_catalog(git_show(workspace, head, catalog_path), f"{catalog_path}@head")
        changed, known = changed_aliases(old, new)
        names = catalog_roots(catalog_path, texts)
        accessors = {accessor(root, kind, segs) for root in names for kind in SECTIONS for segs in changed[kind]}
        announce(f"catalog: {catalog_path} changed accessors: {summarize(accessors)}")
        for relative in files:
            for hit in references(texts[relative], names, changed, known):
                return f"kept ({relative} uses {hit})"
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--component", required=True, help="Component JSON object (name, path, watch).")
    parser.add_argument("--summary-log", default="", help="File that de-duplicates the changed-accessor summary across calls.")
    parser.add_argument("catalogs", nargs="+", help="Changed catalog files that matched the component's watch.")
    args = parser.parse_args(argv)

    try:
        component = json.loads(args.component)
        name = component["name"]
    except (ValueError, KeyError, TypeError):
        print("catalog: fail-safe (invalid component JSON)", file=sys.stderr)
        print("keep")
        return 0

    def log(message):
        print(f"catalog: {name}: {message}", file=sys.stderr)

    def announce(line):
        if args.summary_log:
            try:
                with open(args.summary_log, "a+", encoding="utf-8") as handle:
                    handle.seek(0)
                    if line in handle.read().splitlines():
                        return
                    handle.write(line + "\n")
            except OSError:
                pass
        print(line, file=sys.stderr)

    try:
        if not args.base or not args.head:
            raise FailSafe("diff base unavailable")
        reason = decide(args.workspace, args.base, args.head, component, args.catalogs, announce)
    except FailSafe as error:
        log(f"fail-safe ({error})")
        print("keep")
        return 0
    if reason:
        log(reason)
        print("keep")
    else:
        log("dropped (no scanned build file references a changed alias)")
        print("drop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
