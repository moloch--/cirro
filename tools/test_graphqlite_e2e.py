#!/usr/bin/env python3
"""Exercise the real Cirro CLI with a separate GraphQLite output database.

By default, generate explicitly synthetic Azure/Tailscale input fixtures. Pass
--azure-source and/or --tailscale-source to exercise existing collected data.
Source files are never edited; their SHA-256 hashes are checked after the run.
Only aggregate counts, check names and hashes are written to the report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = {"vmType": "synthetic", "enabled": True, "ports": [80, 443], "literal": "[1]"}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def identifier(name):
    return '"' + name.replace('"', '""') + '"'


def canonical_node(labels, properties):
    properties = dict(properties)
    # This property is explicitly produced by apoc.convert.toJson. Preserve
    # its string type while ignoring JSON object order/whitespace differences
    # between the engines. Ordinary JSON-looking strings are left untouched.
    if "VMExtension" in labels and isinstance(properties.get("settings"), str):
        try:
            properties["settings"] = json.dumps(json.loads(properties["settings"]), sort_keys=True, separators=(",", ":"))
        except ValueError:
            pass
    return json.dumps([sorted(labels), properties], sort_keys=True)


def canonical_edge(source, kind, target, properties, synthetic):
    properties = dict(properties)
    # The injected duplicate fixture gathers relationships without ORDER BY.
    # Compare its two provenance values without imposing an engine-specific
    # collect order; preserve ordinary resource list order everywhere else.
    if synthetic and kind == "E2E_OWNS" and isinstance(properties.get("source"), list):
        properties["source"] = sorted(properties["source"])
    return json.dumps([source, kind, target, properties], sort_keys=True)


def source_metadata(path, kind):
    result = {"kind": kind, "path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)}
    if kind == "az":
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            result["tables"] = []
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                result["tables"].append({
                    "name": name,
                    "columns": [{"name": row[1], "type": row[2]} for row in db.execute("PRAGMA table_info(" + identifier(name) + ")")],
                    "rows": db.execute("SELECT count(*) FROM " + identifier(name)).fetchone()[0],
                })
    else:
        data = json.loads(path.read_text())
        result["top_level_keys"] = sorted(data)
        result["users"] = len(data.get("User", {}))
        result["peers"] = len(data.get("Peer", {}))
        result["self_present"] = isinstance(data.get("Self"), dict)
    return result


def generate_sources(directory, include_certificate=True):
    """Use the collector/spec schemas; every row below is invented test data."""
    azure = directory / "synthetic-azure.db"
    tailscale = directory / "synthetic-tailscale.json"
    tables = {}
    for spec in (ROOT / "src/graph/config/azure").rglob("*.tera.yaml"):
        text = spec.read_text()
        match = re.search(r'^table_name:\s*"([^"]+)"', text, re.M)
        if match:
            tables[match[1]] = tables.get(match[1], False) or bool(re.search(r"^resource_type:", text, re.M))
    with sqlite3.connect(azure) as db:
        for table, typed in sorted(tables.items()):
            extra = ", sub_id TEXT, rg_id TEXT, resource_type TEXT" if table == "resources" else ", resource_type TEXT" if typed else ""
            db.execute("CREATE TABLE " + identifier(table) + " (id TEXT PRIMARY KEY, data TEXT" + extra + ")")
        db.execute("CREATE TABLE _state(resource_type TEXT PRIMARY KEY,next_uri TEXT NOT NULL,total_collected INTEGER NOT NULL DEFAULT 0,elapsed_secs REAL NOT NULL DEFAULT 0.0)")

        def insert(table, row, resource_type=None):
            columns = ["id", "data"]
            values = [row["id"].lower(), json.dumps(row)]
            if table == "resources":
                columns += ["sub_id", "rg_id"]
                values += ["synthetic-sub", "synthetic-rg"]
            if resource_type is not None:
                columns.append("resource_type")
                values.append(resource_type)
            db.execute("INSERT INTO " + identifier(table) + " (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in values) + ")", values)

        insert("users", {"id": "U1", "@odata.type": "#microsoft.graph.user", "displayName": "Synthetic Alice", "userPrincipalName": "alice@example.test", "accountEnabled": True, "memberOf": ["G1"], "manager": "U2"})
        insert("users", {"id": "U2", "@odata.type": "#microsoft.graph.user", "displayName": "Synthetic Bob", "userPrincipalName": "bob@example.test", "accountEnabled": False, "memberOf": []})
        insert("groups", {"id": "G1", "@odata.type": "#microsoft.graph.group", "displayName": "Synthetic Team", "securityEnabled": True, "members": ["U1"], "owners": ["U2"], "memberOf": []})
        insert("roleAssignments", {"id": "assignment", "roleName": "Custom Reader", "description": "Synthetic role", "type": "roleAssignments", "permissions": [{"actions": ["read"], "notActions": [], "dataActions": [], "notDataActions": []}], "properties": {"principalId": "U1", "scope": "net", "roleDefinitionId": "role"}})
        resources = [
            ("net", "microsoft.network/virtualnetworks", {"properties": {"enableDdosProtection": True, "addressSpace": {"addressPrefixes": ["10.0.0.0/16"]}, "subnets": [{"id": "subnet", "name": "Synthetic Subnet", "properties": {"routeTable": {"id": "route"}, "networkSecurityGroup": {"id": "nsg"}}}], "virtualNetworkPeerings": [{"id": "peering", "name": "Synthetic Peering", "properties": {"remoteVirtualNetwork": {"id": "remote"}, "allowVirtualNetworkAccess": True}}]}}),
            ("database", "oracle.database/autonomousdatabases", {"properties": {"subnetId": "subnet", "connectionUrls": {"serviceUrl": "https://db.example.test", "jdbcUrl": "jdbc:synthetic"}, "isMtlsConnectionRequired": True}}),
            ("extension", "microsoft.compute/virtualmachines/extensions", {"properties": {"settings": SETTINGS, "autoUpgradeMinorVersion": False}}),
            ("conditional", "synthetic.example/resources", {"managedBy": "host", "identity": {"type": "SystemAssigned", "principalId": "principal"}, "properties": {}}),
        ]
        for resource_id, resource_type, extra in resources:
            insert("resources", {"id": resource_id, "type": resource_type, "name": "Synthetic " + resource_id, "tags": {"origin": "synthetic-e2e"}, **extra}, resource_type)
        insert("resources", {"id": "missing-tags", "type": "synthetic.example/resources", "name": "Synthetic missing tags", "properties": {}}, "synthetic.example/resources")
        insert("resources", {"id": "null-tags", "type": "synthetic.example/resources", "name": "Synthetic null tags", "properties": {}, "tags": None}, "synthetic.example/resources")
        insert("keyVaultItems", {"id": "https://vault.example.test/secrets/sample/v1", "name": "Synthetic secret", "value": "synthetic-not-a-credential", "attributes": {"enabled": False}, "tags": {}}, "secretvalue")
        if include_certificate:
            insert("keyVaultItems", {"id": "https://vault.example.test/certificates/sample/v1", "name": "Synthetic certificate", "x509ThumbprintHex": "synthetic-thumbprint", "attributes": {"enabled": True}, "policy": {}, "tags": {}}, "certificatevalue")

    tailscale.write_text(json.dumps({
        "CurrentTailnet": {"Name": "example.test", "MagicDNSEnabled": True},
        "Self": {"ID": "device1", "UserID": 1, "Hostname": "synthetic-one", "Online": True, "TailscaleIPs": ["100.64.0.1"]},
        "Peer": {"synthetic-key": {"ID": "device2", "UserID": 2, "Hostname": "synthetic-two", "Online": False}},
        "User": {"1": {"ID": 1, "LoginName": "alice@example.test", "DisplayName": "Synthetic Alice"}, "2": {"ID": 2, "LoginName": "bob@example.test", "DisplayName": "Synthetic Bob"}},
    }, indent=2) + "\n")
    return [("az", azure), ("ts-status", tailscale)]


class EndToEnd:
    def __init__(self, args, directory, engine="graphqlite"):
        self.args = args
        self.directory = directory
        self.graph = directory / "graph.db"
        self.engine = engine
        self.commands = 0
        self.report = {"backend": engine, "dataset_kind": "supplied" if args.azure_source or args.tailscale_source else "synthetic", "binary_sha256": sha256(args.binary), "checks": [], "queries": [], "sources": []}
        if engine == "graphqlite":
            self.report["extension"] = {"mode": "override", "path": str(args.extension), "sha256": sha256(args.extension)} if args.extension else {"mode": "embedded"}
        self.report["comparison_normalization"] = ["Internal graph IDs", "Label order", "VMExtension.settings JSON object order and whitespace, preserving string type"]
        if self.report["dataset_kind"] == "synthetic":
            self.report["comparison_normalization"].append("Synthetic E2E_OWNS.source ordering from unordered collect")

    def run(self, arguments, query=False, succeeds=True):
        self.commands += 1
        command = [str(self.args.binary), "graph", *arguments]
        process = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=self.args.timeout)
        # Query results can contain collected data; keep them in memory only.
        log = self.directory / ("command-%02d.log" % self.commands)
        log.write_text(process.stderr + ("" if query else process.stdout))
        if succeeds:
            require(process.returncode == 0, "CLI command %d failed; see %s" % (self.commands, log))
        else:
            require(process.returncode != 0, "CLI command unexpectedly succeeded")
        return process

    def backend(self, graph=None):
        if self.engine == "neo4j":
            return ["--backend", "neo4j", "--server", self.args.neo4j_server,
                    "--user", self.args.neo4j_user, "--password", self.args.neo4j_password]
        args = ["--backend", "graphqlite", "--graph-file", str(graph or self.graph)]
        if self.args.extension:
            args += ["--graphqlite-extension", str(self.args.extension)]
        return args

    def query(self, name, cypher, params=None):
        result = self.run(["query", *self.backend(), "--cypher", cypher, "--params", json.dumps(params or {})], query=True)
        value = json.loads(result.stdout)
        self.report["queries"].append({"name": name, "cypher": cypher, "rows": len(value) if isinstance(value, list) else None})
        return value

    def check(self, name, condition):
        require(condition, name)
        self.report["checks"].append(name)

    def ingest(self, source, graph=None, extra=()):
        kind, path = source
        return self.run(["ingest", *self.backend(graph), "--type", kind, "--file", str(path), *extra])

    def snapshot(self):
        nodes = self.query("snapshot nodes", "MATCH (n) RETURN id(n) AS internal, labels(n) AS labels, properties(n) AS properties")
        keys = {node["internal"]: canonical_node(node["labels"], node["properties"]) for node in nodes}
        edges = self.query("snapshot edges", "MATCH (a)-[r]->(b) RETURN id(a) AS source, id(b) AS target, type(r) AS type, properties(r) AS properties")
        canonical = [sorted(keys.values()), sorted(canonical_edge(keys[edge["source"]], edge["type"], keys[edge["target"]], edge["properties"], self.report["dataset_kind"] == "synthetic") for edge in edges)]
        return hashlib.sha256(json.dumps(canonical).encode()).hexdigest(), len(nodes), len(edges)

    def synthetic_checks(self):
        users = self.query("enabled user types", "MATCH (u:GraphUser) RETURN u.id AS id, u.accountEnabled AS enabled ORDER BY id")
        self.check("true and false user properties survive", len(users) == 2 and users[0]["id"] == "u1" and users[0]["enabled"] is True and users[1]["id"] == "u2" and users[1]["enabled"] is False)
        metadata = self.query("dotted OData map keys", "MATCH (n:GraphObject) WHERE n.id IN ['u1','u2','g1'] RETURN n.id AS id, n.odataType AS kind ORDER BY id")
        self.check("dynamic dotted map keys preserve OData types", metadata == [{"id": "g1", "kind": "#microsoft.graph.group"}, {"id": "u1", "kind": "#microsoft.graph.user"}, {"id": "u2", "kind": "#microsoft.graph.user"}])
        tags = self.query("resource tag comprehensions", "MATCH (n:ArmResource) WHERE n.id IN ['net','database','extension','conditional'] RETURN n.tags AS tags")
        self.check("list comprehensions retain nonempty tag lists", len(tags) == 4 and all(row["tags"] == ["origin:synthetic-e2e"] for row in tags))
        empty_tags = self.query("empty tag comprehensions", "MATCH (n) WHERE n:KeyVaultSecretValue OR n:KeyVaultCertificateValue RETURN n.tags AS tags")
        self.check("list comprehensions retain empty tag lists", empty_tags == [{"tags": []}, {"tags": []}])
        absent_tags = self.query("missing and null tag comprehensions", "MATCH (n:ArmResource) WHERE n.id IN ['missing-tags','null-tags'] RETURN n.id AS id, properties(n) AS properties")
        self.check("missing and null tags do not create a list property", len(absent_tags) == 2 and all("tags" not in row["properties"] for row in absent_tags))
        memberships = self.query("group membership", "MATCH (u:GraphUser)-[:MEMBER_OF]->(g:GraphGroup) RETURN u.id AS user, g.id AS group")
        self.check("user-group membership has correct endpoints", memberships == [{"user": "u1", "group": "g1"}])
        managers = self.query("management chain", "MATCH (m:GraphUser)-[:MANAGES]->(u:GraphUser) RETURN m.id AS manager, u.id AS user")
        self.check("management direction survives", managers == [{"manager": "u2", "user": "u1"}])
        links = self.query("cross-platform identity", "MATCH (t:TSUser)-[:SAME_USER]->(u:GraphUser) RETURN t.id AS tailscale, u.id AS azure ORDER BY tailscale")
        self.check("postprocessing links Tailscale and Azure identities", links == [{"tailscale": "1", "azure": "u1"}, {"tailscale": "2", "azure": "u2"}])
        network = self.query("nested subnet configuration", "MATCH (:VirtualNetwork {id:$id})-[:CONTAINS]->(s)-[:HAS_NSG]->(n) RETURN s.id AS subnet, n.id AS nsg", {"id": "net"})
        self.check("parameterized nested-network query", network == [{"subnet": "subnet", "nsg": "nsg"}])
        role = self.query("dynamic role permissions", "MATCH (:GraphUser {id:'u1'})-[r:CustomReader]->(:ArmResource {id:'net'}) RETURN r.actions AS actions")
        self.check("dynamic relationship and list property", role == [{"actions": ["read"]}])
        database = self.query("dynamic resource properties", "MATCH (n {id:'database'}) RETURN n.serviceUrl AS url, n.isMtlsConnectionRequired AS mtls")
        self.check("dynamic property and boolean", len(database) == 1 and database[0]["url"] == "https://db.example.test" and database[0]["mtls"] is True)
        settings = self.query("serialized settings", "MATCH (n {id:'extension'}) RETURN n.settings AS settings, n.autoUpgradeMinorVersion AS upgrade")
        self.check("APOC JSON stays a string", len(settings) == 1 and isinstance(settings[0]["settings"], str) and json.loads(settings[0]["settings"]) == SETTINGS and settings[0]["upgrade"] is False)
        conditional = self.query("conditional managed identity", "MATCH (r {id:'conditional'})-[:HAS_IDENTITY]->(i) RETURN i.id AS identity")
        self.check("conditional identity branch", conditional == [{"identity": "principal"}])
        versions = self.query("vault version parents", "MATCH (p)-[:HAS_VERSION]->(v) RETURN p.id AS parent, v.id AS version")
        self.check("regex and projected imports preserve vault parent IDs", len(versions) == 2 and all(row["version"] == row["parent"] + "/v1" for row in versions))

    def execute(self):
        started = time.monotonic()
        if self.report["dataset_kind"] == "synthetic":
            sources = generate_sources(self.directory)
        else:
            sources = [(kind, path) for kind, path in [("az", self.args.azure_source), ("ts-status", self.args.tailscale_source)] if path]
        self.report["sources"] = [source_metadata(path, kind) for kind, path in sources]
        dry_graph = self.directory / "dry-run-must-not-exist.db"
        for source in sources:
            self.ingest(source, dry_graph, ["--dry-run"])
        if self.engine == "graphqlite":
            self.check("dry-run does not create graph file", not dry_graph.exists())
        for source in sources:
            self.ingest(source)
        if self.engine == "graphqlite":
            self.check("graph database persists on disk", self.graph.is_file() and self.graph.stat().st_size > 0)
        if self.report["dataset_kind"] == "synthetic":
            self.synthetic_checks()

        before = self.snapshot()
        self.report["import_snapshot_sha256"] = before[0]
        self.report["import_nodes"] = before[1]
        self.report["import_edges"] = before[2]
        self.check("populated graph", before[1] > 0)
        for source in sources:
            self.ingest(source, extra=["--dry-run"])
        self.check("dry-run leaves existing graph unchanged", self.snapshot() == before)
        for source in sources:
            self.ingest(source)
        self.check("repeated import and postprocessing are idempotent", self.snapshot() == before)
        self.ingest(sources[0], extra=["--post-process"])
        self.check("postprocess-only is idempotent", self.snapshot() == before)

        if self.report["dataset_kind"] == "synthetic":
            self.query("seed duplicate fixture", "CREATE (a:E2EDuplicate {id:'E2E-DUP',values:[1,2],mixed:true}), (b:E2EDuplicate:AdditionalLabel {id:'e2e-dup',values:[2,3],mixed:false}), (o:E2EOwner {id:'e2e-owner'}), (o)-[:E2E_OWNS {source:'first'}]->(a), (o)-[:E2E_OWNS {source:'second'}]->(b)")
            self.ingest(sources[0], extra=["--post-process"])
            merged = self.query("deduplicated properties", "MATCH (n:E2EDuplicate) RETURN n.id AS id, n.values AS values, n.mixed AS mixed, labels(n) AS labels")
            self.check("deduplication combines typed properties and labels", len(merged) == 1 and merged[0]["id"] == "e2e-dup" and merged[0]["values"] == [1, 2, 3] and json.dumps(merged[0]["mixed"]) == "[true, false]" and set(merged[0]["labels"]) == {"E2EDuplicate", "AdditionalLabel"})
            edges = self.query("deduplicated incident edges", "MATCH (:E2EOwner)-[r:E2E_OWNS]->(:E2EDuplicate) RETURN r.source AS sources")
            self.check("deduplication preserves incident edge properties", len(edges) == 1 and len(edges[0]["sources"]) == 2 and set(edges[0]["sources"]) == {"first", "second"})
            if self.engine == "graphqlite":
                filtered = self.directory / "filtered.db"
                self.ingest(sources[0], filtered, ["--labels", "GraphUser"])
                result = self.run(["query", *self.backend(filtered), "--cypher", "MATCH (n:GraphUser) RETURN count(n) AS count"], query=True)
                self.check("label-filtered ingestion retains users", json.loads(result.stdout) == [{"count": 2}])
                result = self.run(["query", *self.backend(filtered), "--cypher", "MATCH (n) WHERE n:GraphGroup OR n:ArmResource RETURN count(n) AS count"], query=True)
                self.check("label-filtered ingestion excludes other resource specs", json.loads(result.stdout) == [{"count": 0}])

        final = self.snapshot()
        export_path = self.directory / "opengraph.json"
        self.run(["export", *self.backend(), "--format", "opengraph", "--output", str(export_path)])
        exported = json.loads(export_path.read_text())["graph"]
        ids = {node["id"] for node in exported["nodes"]}
        self.check("OpenGraph preserves all nodes and edges", len(exported["nodes"]) == final[1] and len(exported["edges"]) == final[2])
        self.check("OpenGraph relationship endpoints resolve", all(edge["start"]["value"] in ids and edge["end"]["value"] in ids for edge in exported["edges"]))
        node_keys = {node["id"]: canonical_node(node["kinds"], node["properties"]) for node in exported["nodes"]}
        normalized_export = [sorted(node_keys.values()), sorted(canonical_edge(node_keys[edge["start"]["value"]], edge["kind"], node_keys[edge["end"]["value"]], edge.get("properties", {}), self.report["dataset_kind"] == "synthetic") for edge in exported["edges"])]
        self.report["export_sha256"] = hashlib.sha256(json.dumps(normalized_export).encode()).hexdigest()
        self.run(["export", *self.backend(), "--format", "grass", "--output", str(self.directory / "styles")])
        self.check("GraSS export contains node styles", "node." in (self.directory / "styles.grass").read_text())
        self.check("read queries and exports leave graph unchanged", self.snapshot() == final)
        if self.engine == "graphqlite":
            self.alias_protection_checks(sources)
            if self.args.pagination_rows:
                self.pagination_checks(self.args.pagination_rows)
        self.check("input sources unchanged", all(sha256(Path(source["path"])) == source["sha256"] for source in self.report["sources"]))
        self.report.update({"passed": True, "nodes": final[1], "edges": final[2], "snapshot_sha256": final[0], "cli_commands": self.commands, "elapsed_seconds": round(time.monotonic() - started, 3)})
        if self.engine == "graphqlite":
            self.report["graph_file"] = str(self.graph)

    def pagination_checks(self, count):
        directory = self.directory / "pagination"
        directory.mkdir()
        sources = generate_sources(directory)
        source = sources[0][1]
        with sqlite3.connect(source) as db:
            db.execute("DELETE FROM users")
            db.executemany("INSERT INTO users(id,data) VALUES (?,?)", (
                ("pagination-%06d" % number, json.dumps({"id": "PAGINATION-%06d" % number, "displayName": "Synthetic Pagination User %d" % number, "accountEnabled": number % 2 == 0, "@odata.type": "#microsoft.graph.user", "memberOf": []}))
                for number in range(count)
            ))
        source_hash = sha256(source)
        graph = directory / "graph.db"
        self.ingest(("az", source), graph, ["--labels", "GraphUser"])
        total = self.run(["query", *self.backend(graph), "--cypher", "MATCH (u:GraphUser) RETURN count(u) AS count"], query=True)
        self.check("pagination ingests all rows across the 2500-row boundary", json.loads(total.stdout) == [{"count": count}])
        boundary = [0, 2499, count - 1]
        ids = ["pagination-%06d" % number for number in boundary]
        rows = self.run(["query", *self.backend(graph), "--cypher", "MATCH (u:GraphUser) WHERE u.id IN $ids RETURN u.id AS id, u.displayName AS name, u.accountEnabled AS enabled ORDER BY id", "--params", json.dumps({"ids": ids})], query=True)
        expected = [{"id": "pagination-%06d" % number, "name": "Synthetic Pagination User %d" % number, "enabled": number % 2 == 0} for number in boundary]
        self.check("pagination preserves first, boundary and last rows", json.dumps(json.loads(rows.stdout), sort_keys=True) == json.dumps(expected, sort_keys=True))
        self.check("pagination source remains unchanged", sha256(source) == source_hash)
        self.report["pagination"] = {"dataset_kind": "synthetic", "input_user_rows": count, "source": str(source), "source_sha256": source_hash, "graph_file": str(graph)}

    def alias_protection_checks(self, sources):
        graph_hash = sha256(self.graph)
        hardlink = self.directory / "export-hardlink.json"
        symlink = self.directory / "export-symlink.json"
        os.link(self.graph, hardlink)
        symlink.symlink_to(self.graph)
        for name, output in [("same file", self.graph), ("hardlink", hardlink), ("symlink", symlink)]:
            self.run(["export", *self.backend(), "--format", "opengraph", "--output", str(output)], succeeds=False)
            self.check("export rejects graph " + name + " without changing it", sha256(self.graph) == graph_hash)
        grass = self.directory / "export-alias.grass"
        grass.symlink_to(self.graph)
        self.run(["export", *self.backend(), "--format", "grass", "--output", str(grass.with_suffix(".json"))], succeeds=False)
        self.check("GraSS export resolves its suffix before rejecting a graph alias", sha256(self.graph) == graph_hash)

        # Deliberately invalid configurations use a disposable source copy.
        # The original collection is never the target of a mutation attempt.
        kind, source = sources[0]
        copy = self.directory / "alias-source-copy.db"
        shutil.copyfile(source, copy)
        copy_hash = sha256(copy)
        hardlink = self.directory / "source-hardlink.db"
        symlink = self.directory / "source-symlink.db"
        os.link(copy, hardlink)
        symlink.symlink_to(copy)
        for name, graph in [("same file", copy), ("hardlink", hardlink), ("symlink", symlink)]:
            self.run(["ingest", *self.backend(graph), "--type", kind, "--file", str(copy)], succeeds=False)
            self.check("ingest rejects source " + name + " without changing it", sha256(copy) == copy_hash)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "target/release/cirro")
    parser.add_argument("--extension", type=Path, help="Override embedded extension for local development")
    parser.add_argument("--work-dir", type=Path, help="New, nonexistent output directory; defaults to a temporary directory")
    parser.add_argument("--azure-source", type=Path, help="Existing collector SQLite database (read only)")
    parser.add_argument("--tailscale-source", type=Path, help="Existing Tailscale status JSON (read only)")
    parser.add_argument("--timeout", type=int, default=180, help="Timeout in seconds per CLI command")
    parser.add_argument("--pagination-rows", type=int, default=0, help="Also test at least 2501 synthetic users across the ingestion batch boundary")
    parser.add_argument("--neo4j-server", help="Optional EMPTY disposable Neo4j baseline; never point this at an existing graph")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password", help="Credentials for the disposable baseline")
    args = parser.parse_args()
    require(args.pagination_rows == 0 or args.pagination_rows > 2500, "--pagination-rows must be at least 2501")
    for name in ("binary", "extension", "azure_source", "tailscale_source"):
        value = getattr(args, name)
        if value:
            value = value.resolve(strict=True)
            require(value.is_file(), name + " must be a file")
            setattr(args, name, value)
    if args.work_dir:
        directory = args.work_dir.resolve()
        directory.mkdir(parents=True, exist_ok=False)
    else:
        directory = Path(tempfile.mkdtemp(prefix="cirro-graphqlite-e2e-"))
    test = EndToEnd(args, directory)
    try:
        test.execute()
        test.report["cli_pipeline_passed"] = True
        if args.neo4j_server:
            require(args.neo4j_password, "--neo4j-password is required with --neo4j-server")
            baseline_dir = directory / "neo4j-baseline"
            baseline_dir.mkdir()
            baseline = EndToEnd(args, baseline_dir, "neo4j")
            try:
                rows = baseline.query("require empty baseline", "MATCH (n) RETURN count(n) AS count")
                require(rows == [{"count": 0}], "Neo4j baseline must be empty; existing data was not modified")
                baseline.execute()
            except Exception as error:
                baseline.report.update({"passed": False, "error": str(error)})
                raise
            finally:
                (baseline_dir / "report.json").write_text(json.dumps(baseline.report, indent=2) + "\n")
                test.report["neo4j_baseline"] = str(baseline_dir / "report.json")
            test.check("Neo4j and GraphQLite use identical ordered input sources", [(source["kind"], source["sha256"]) for source in test.report["sources"]] == [(source["kind"], source["sha256"]) for source in baseline.report["sources"]])
            test.check("Neo4j and GraphQLite import snapshots match before injected duplicates", test.report["import_snapshot_sha256"] == baseline.report["import_snapshot_sha256"])
            test.check("Neo4j and GraphQLite graph snapshots match", test.report["snapshot_sha256"] == baseline.report["snapshot_sha256"])
            test.check("Neo4j and GraphQLite normalized OpenGraph exports match", test.report["export_sha256"] == baseline.report["export_sha256"])
    except Exception as error:
        test.report.update({"passed": False, "error": str(error), "cli_commands": test.commands})
    finally:
        unchanged = all(Path(source["path"]).is_file() and sha256(Path(source["path"])) == source["sha256"] for source in test.report["sources"])
        test.report["sources_unchanged"] = unchanged
        if not unchanged:
            test.report.update({"passed": False, "source_integrity_error": "An input source changed during the run"})
    report_path = directory / "report.json"
    report_path.write_text(json.dumps(test.report, indent=2) + "\n")
    print(json.dumps({"passed": test.report["passed"], "dataset_kind": test.report["dataset_kind"], "checks": len(test.report["checks"]), "report": str(report_path)}))
    return 0 if test.report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
