#!/usr/bin/env python3
"""Smoke-test a packaged GraphQLite-enabled Cirro without source/build files.

Run this script on a fresh runner with only the release archive and this script:
  python test_graphqlite_release.py --archive cirro-graphqlite-TARGET.tar.xz \
    --work-dir /tmp/cirro-release-check

Alternatively pass --binary to test a copied executable. Each child runs in an
empty working directory with new cache/temp directories and a minimal environment;
no extension override is accepted. --absent-path can assert that known checkout
or build paths are genuinely absent. Isolation does not hide arbitrary host files,
so copying a local build alone is not proof that its original paths are absent.
No network services, third-party Python modules, or repository files are needed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile


ANSI = re.compile(r"\x1b\[[0-9;]*m")
NATIVE_SUFFIXES = (".so", ".dylib", ".dll")
PACKAGE_FILES = ("README.md", "LICENSE", "graphqlite-LICENSE", "pcre2-LICENSE", "build-info.json")
REQUIRED_FEATURES = {"azure", "tailscale", "graph", "graphqlite"}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_archive_checksum(archive):
    checksum = archive.with_name(archive.name + ".sha256")
    require(checksum.is_file(), "Release archive is missing its .sha256 file")
    match = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?([^\r\n]+)\r?\n?",
                         checksum.read_text(encoding="utf-8"))
    require(match is not None and match.group(2) == archive.name,
            "Archive checksum must name exactly the supplied archive")
    require(sha256(archive) == match.group(1).lower(), "Archive checksum does not match")


def verify_build_info(binary, expected_target=None, expected_revision=None):
    metadata = json.loads((binary.parent / "build-info.json").read_text(encoding="utf-8"))
    require(isinstance(metadata, dict), "build-info.json must be an object")
    for key in ("version", "target", "cirro_revision", "graphqlite_revision",
                "binary_sha256", "embedded_extension_sha256"):
        require(isinstance(metadata.get(key), str) and metadata[key],
                "Build metadata must contain a nonempty string: " + key)
    for key in ("cirro_revision", "graphqlite_revision"):
        require(re.fullmatch(r"[0-9a-f]{40}", metadata[key]) is not None,
                "Build metadata must contain an exact Git revision: " + key)
    for key in ("binary_sha256", "embedded_extension_sha256"):
        require(re.fullmatch(r"[0-9a-f]{64}", metadata[key]) is not None,
                "Build metadata must contain a SHA-256 digest: " + key)
    features = metadata.get("features")
    require(isinstance(features, list) and all(isinstance(item, str) for item in features)
            and len(features) == len(set(features)) and REQUIRED_FEATURES <= set(features),
            "Build metadata must enable graph, GraphQLite, Azure and Tailscale")
    require(sha256(binary) == metadata["binary_sha256"], "Packaged binary checksum does not match metadata")
    if expected_target is not None:
        require(metadata["target"] == expected_target, "Packaged target differs from expected target")
    if expected_revision is not None:
        require(metadata["cirro_revision"] == expected_revision, "Packaged Cirro revision differs from expected revision")
    return metadata


def member_path(name, destination):
    """Reject traversal and Windows path aliases before extracting any member."""
    path = PurePosixPath(name)
    require(name and "\\" not in name and ":" not in name and not path.is_absolute()
            and ".." not in path.parts, "Unsafe archive member: " + name)
    output = destination.joinpath(*path.parts)
    require(output.is_relative_to(destination), "Archive member escapes destination")
    return output


def extract_archive(archive, destination):
    """Only regular files/directories; never follow archive links or devices."""
    seen = set()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for item in bundle.infolist():
                output = member_path(item.filename, destination)
                require(output not in seen, "Duplicate archive member: " + item.filename)
                seen.add(output)
                mode = item.external_attr >> 16
                require(mode & 0o170000 in (0, 0o100000, 0o040000), "Archive links/devices are unsupported")
                if item.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(item) as source, output.open("xb") as target:
                        shutil.copyfileobj(source, target)
    else:
        with tarfile.open(archive, "r:*") as bundle:
            for item in bundle:
                output = member_path(item.name, destination)
                require(output not in seen, "Duplicate archive member: " + item.name)
                seen.add(output)
                require(item.isfile() or item.isdir(), "Archive links/devices are unsupported")
                if item.isdir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.extractfile(item) as source, output.open("xb") as target:
                        shutil.copyfileobj(source, target)
    binaries = [p for p in destination.rglob("*") if p.is_file() and p.name in ("cirro", "cirro.exe")]
    require(len(binaries) == 1, "Archive must contain exactly one cirro executable")
    binary = binaries[0]
    binary.chmod(0o755)
    for name in PACKAGE_FILES:
        require((binary.parent / name).is_file(), "Release package is missing " + name)
    return binary


def isolated_environment(cache, temporary):
    # Do not carry caller credentials, extension settings, linker paths, or a
    # compiler/toolchain PATH into the executable under test. Windows needs its
    # system directory to resolve OS DLLs; MSYS/MinGW directories stay excluded.
    environment = {"PATH": os.defpath, "NO_COLOR": "1", "TERM": "dumb"}
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
            if key in os.environ:
                environment[key] = os.environ[key]
        system = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        environment["PATH"] = os.pathsep.join((str(system / "System32"), str(system)))
    environment.update({"TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary),
                        "XDG_CACHE_HOME": str(cache), "LOCALAPPDATA": str(cache),
                        "APPDATA": str(cache)})
    return environment


class ReleaseSmoke:
    def __init__(self, binary, directory, timeout, report):
        self.binary, self.directory, self.timeout, self.report = binary, directory, timeout, report
        self.cwd, self.cache, self.temporary = (directory / p for p in ("cwd", "cache", "tmp"))
        for path in (self.cwd, self.cache, self.temporary):
            path.mkdir()
        self.environment = isolated_environment(self.cache, self.temporary)
        self.graph = directory / "release-graph.db"
        self.commands = 0

    def check(self, name, condition):
        require(condition, name)
        self.report["checks"].append(name)

    def command(self, arguments, succeeds=True, include_stderr=True):
        self.commands += 1
        require(not any(self.cwd.iterdir()), "Working directory must stay empty")
        require(not any(self.cache.iterdir()), "Cache must stay empty")
        require(not any(self.temporary.iterdir()), "Temporary extension was not cleaned up")
        try:
            result = subprocess.run([str(self.binary), *arguments], cwd=self.cwd,
                                    env=self.environment, text=True, capture_output=True,
                                    timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Release smoke command %d timed out" % self.commands) from None
        log = self.directory / ("command-%02d.log" % self.commands)
        log.write_text(result.stderr + result.stdout, encoding="utf-8")
        require((result.returncode == 0) == succeeds,
                "Release smoke command %d had unexpected exit status; see %s" % (self.commands, log))
        require(not any(self.temporary.iterdir()), "Bundled extension must be removed when its process exits")
        return ANSI.sub("", result.stdout + (result.stderr if include_stderr else ""))

    def query(self, cypher, params=None):
        output = self.command(["graph", "query", "--backend", "graphqlite", "--graph-file", str(self.graph),
                               "--cypher", cypher, "--params", encoded(params or {})], include_stderr=False)
        # Parse the entire stdout document: formatted JSON spans several lines,
        # while progress logging belongs exclusively to stderr.
        rows = json.loads(output)
        require(isinstance(rows, list), "Query result must be an array")
        return rows

    def execute(self):
        self.check("version executes outside the build tree", "cirro" in self.command(["--version"]).lower())
        help_text = self.command(["--help"])
        self.check("collector and graph commands remain available", "collect" in help_text and "graph" in help_text)
        collect = self.command(["collect", "--help"])
        self.check("Azure and Tailscale collectors remain available", bool(re.search(r"^\s+az\s", collect, re.M))
                   and bool(re.search(r"^\s+ts\s", collect, re.M)))
        query_help = self.command(["graph", "query", "--help"])
        self.check("Neo4j remains the default with both backends selectable", "default: neo4j" in query_help
                   and "neo4j, graphqlite" in query_help)
        for backend in ([], ["--backend", "neo4j"]):
            output = self.command(["graph", "query", *backend, "--server", "invalid-scheme://unused",
                                   "--cypher", "RETURN 1 AS value"], succeeds=False)
            self.check("Neo4j validates locally without a service" + (" (default)" if not backend else " (explicit)"),
                       "Neo4j server must use" in output)
        self.graph.touch(exist_ok=False)
        rows = [{"id": "a", "name": "Release A", "enabled": True, "count": 1, "score": 1.5,
                 "tags": {"origin": "release"}, "raw": "[true]", "optional": None},
                {"id": "b", "name": "Release B", "enabled": False, "count": 2, "score": 2.5,
                 "tags": {}, "raw": "false", "optional": None}]
        loaded = self.query("UNWIND $rows AS row MERGE (n:ReleaseProbe {id:row.id}) "
            "SET n += {name:row.name, enabled:row.enabled, count:row.count, score:row.score, "
            "tags:[key IN keys(row.tags) | key + ':' + row.tags[key]], raw:row.raw, optional:row.optional} "
            "RETURN count(n) AS count", {"rows": rows})
        self.check("embedded extension writes parameterized Cypher", encoded(loaded) == encoded([{"count": 2}]))
        decisions = self.query("MATCH (n:ReleaseProbe) CALL (n) { WHEN n.enabled THEN { "
            "SET n += {decision:CASE WHEN n.count=1 THEN 'selected' ELSE 'other' END} } "
            "WHEN NOT n.enabled THEN { SET n.decision='disabled' } } "
            "RETURN n.id AS id,n.decision AS decision ORDER BY id")
        self.check("scoped CALL, conditional WHEN and CASE execute", encoded(decisions) == encoded([
            {"id": "a", "decision": "selected"}, {"id": "b", "decision": "disabled"}]))
        relation = self.query("MATCH (a:ReleaseProbe {id:'a'}),(b:ReleaseProbe {id:'b'}) "
            "MERGE (a)-[r:RELEASE_LINK]->(b) SET r += {enabled:false,values:[1,2]} "
            "RETURN a.id AS source,b.id AS target,r.enabled AS enabled,r.values AS values")
        self.check("directed edges retain boolean and list properties", encoded(relation) == encoded([
            {"source": "a", "target": "b", "enabled": False, "values": [1, 2]}]))
        expected = [{"id": row["id"], "name": row["name"], "enabled": row["enabled"], "count": row["count"],
                     "score": row["score"], "tags": ["origin:release"] if row["id"] == "a" else [],
                     "raw": row["raw"], "decision": "selected" if row["id"] == "a" else "disabled"} for row in rows]
        read_query = "MATCH (n:ReleaseProbe) RETURN properties(n) AS properties ORDER BY n.id"
        actual = self.query(read_query)
        self.check("separate processes preserve booleans, integers, reals, arrays and JSON-looking strings",
                   encoded(actual) == encoded([{"properties": row} for row in expected]))
        self.check("graph persists without external extension files", self.graph.stat().st_size > 0)
        export = self.directory / "opengraph.json"
        self.command(["graph", "export", "--backend", "graphqlite", "--graph-file", str(self.graph),
                      "--format", "opengraph", "--output", str(export)])
        graph = json.loads(export.read_text())["graph"]
        self.check("OpenGraph exports all typed node properties", sorted(encoded(n["properties"]) for n in graph["nodes"])
                   == sorted(map(encoded, expected)) and all(n["kinds"] == ["ReleaseProbe"] for n in graph["nodes"]))
        ids = {n["id"]: n["properties"]["id"] for n in graph["nodes"]}
        edges = [{"source": ids[e["start"]["value"]], "target": ids[e["end"]["value"]],
                  "kind": e["kind"], "properties": e["properties"]} for e in graph["edges"]]
        self.check("OpenGraph retains relationship direction and property types", encoded(edges) == encoded([
            {"source": "a", "target": "b", "kind": "RELEASE_LINK", "properties": {"enabled": False, "values": [1, 2]}}]))
        styles = self.directory / "styles"
        self.command(["graph", "export", "--backend", "graphqlite", "--graph-file", str(self.graph),
                      "--format", "grass", "--output", str(styles)])
        self.check("GraSS export works", "node.ReleaseProbe {" in styles.with_suffix(".grass").read_text())
        self.check("exports leave persisted graph unchanged", encoded(self.query(read_query)) == encoded(actual))
        self.check("cache, temp and working directory remain empty", all(not any(p.iterdir())
                   for p in (self.cwd, self.cache, self.temporary)))
        self.report["cli_commands"] = self.commands


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--binary", type=Path)
    source.add_argument("--archive", type=Path, help="Release .tar.gz/.tar.xz/.zip package")
    parser.add_argument("--work-dir", type=Path, help="New, nonexistent output directory")
    parser.add_argument("--absent-path", action="append", type=Path, default=[],
                        help="Known source/build path that must not exist on this test host")
    parser.add_argument("--expected-target", help="Required build-info target (archive mode)")
    parser.add_argument("--expected-revision", help="Required exact Cirro Git revision (archive mode)")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if not args.archive and (args.expected_target or args.expected_revision):
        parser.error("--expected-target and --expected-revision require --archive")
    original = (args.binary or args.archive).resolve(strict=True)
    require(original.is_file(), "Input must be a file")
    directory = args.work_dir.resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="cirro-release-smoke-"))
    if args.work_dir:
        directory.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "input": str(original), "input_sha256": sha256(original), "checks": [],
              "mode": "archive" if args.archive else "binary", "platform": sys.platform,
              "extension_mode": "embedded", "absent_paths": [str(p) for p in args.absent_path],
              "isolation": "Detached package, minimal environment, empty cache/temp/cwd; host filesystem is not sandboxed"}
    started = time.monotonic()
    try:
        for path in args.absent_path:
            require(not path.exists() and not path.is_symlink(), "Expected source/build path is present: " + str(path))
        package = directory / "package"
        package.mkdir()
        if args.archive:
            verify_archive_checksum(original)
            report["checks"].append("archive checksum matches its sidecar")
            binary = extract_archive(original, package)
            report["build_info"] = verify_build_info(binary, args.expected_target, args.expected_revision)
            report["checks"].append("package provenance, required features and binary checksum are valid")
        else:
            binary = package / original.name
            shutil.copyfile(original, binary)
            binary.chmod(0o755)
        require(not any(p.is_file() and p.name.lower().endswith(NATIVE_SUFFIXES) for p in package.rglob("*")),
                "Package contains a loose native extension/library; embedded-only artifact expected")
        report["binary_sha256"] = sha256(binary)
        report["checks"].append("package contains no loose native extensions")
        test = ReleaseSmoke(binary, directory, args.timeout, report)
        test.execute()
        require(sha256(original) == report["input_sha256"], "Input package/binary changed during validation")
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    output = directory / "report.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks": len(report["checks"]), "report": str(output)}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
