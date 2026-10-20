#!/usr/bin/env python3
"""Validate a supplied on-premises AD snapshot through Cirro's graph CLI.

This is a test adapter, not a supported Cirro collector or BloodHound importer.
The version-1 JSON snapshot is read only. AD labels identify on-premises objects;
no Azure schemas, cloud postprocessing, ACL inference, or foreign placeholder
nodes are used. Direct membership includes SID-derived primary groups.

Example:
  python tools/test_graphqlite_ad.py --source /tmp/ad-snapshot.json \
    --work-dir /tmp/cirro-ad-validation

Optional parity requires an EMPTY disposable Neo4j database and its password in
CIRRO_NEO4J_PASSWORD. Cirro currently accepts that password only as a child CLI
argument; this script never records credentials or command arguments.
Use --neo4j-binary to compare a separately built default-feature Cirro binary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
COMMON = ("objectGUID", "objectSid", "distinguishedName", "name")
FIELDS = {
    "ADDomain": COMMON + ("dnsRoot",),
    "ADUser": COMMON + ("samAccountName", "userPrincipalName", "enabled", "memberOf", "primaryGroupID"),
    "ADGroup": COMMON + ("samAccountName", "groupCategory", "groupScope", "members", "memberOf"),
    "ADComputer": COMMON + ("samAccountName", "dnsHostName", "enabled", "operatingSystem", "memberOf", "primaryGroupID"),
    "ADOU": COMMON,
}
SOURCES = (("users", "ADUser"), ("groups", "ADGroup"),
           ("computers", "ADComputer"), ("organizationalUnits", "ADOU"))
TRUST_FIELDS = ("source", "target", "direction", "trustType", "intraForest")
SOURCE_FIELDS = ("host", "hostname", "kind", "computerName", "domain", "dnsRoot", "collected_at",
                 "collectedAt", "collector", "collection_method", "method")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def encoded(value):
    """Canonical object order only; retain every scalar type and list order."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def result_rows(rows, ordered):
    """Compare a row sequence or multiset without changing values inside rows."""
    values = [encoded(row) for row in rows]
    return values if ordered else sorted(values)


def compare_query_traces(graphqlite, neo4j):
    """Pair every query by position and require identical inputs and semantics."""
    left, right = graphqlite["queries"], neo4j["queries"]
    entries = []
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else None
        b = right[index] if index < len(right) else None
        if a is None or b is None:
            entries.append({"position": index + 1, "name": (a or b)["name"],
                            "match": False, "error": "Query is missing from one backend trace"})
            continue
        metadata = ("name", "cypher", "params_sha256", "ordered", "comparison")
        same_input = all(a[key] == b[key] for key in metadata)
        canonical = a["comparison"] == b["comparison"] == "canonical_graph"
        ddl = a["comparison"] == b["comparison"] == "schema_ddl"
        if canonical:
            # These are the same canonical node/edge rows used by snapshot().
            # Only internal transport IDs and label ordering are normalized.
            a_rows, b_rows = a["canonical_results"], b["canonical_results"]
        else:
            a_rows = result_rows(a["results"], a["ordered"])
            b_rows = result_rows(b["results"], b["ordered"])
        same_results = a_rows == b_rows
        operation_match = (a.get("success") is True and b.get("success") is True) if ddl else same_results
        entries.append({
            "position": index + 1, "name": a["name"], "cypher": a["cypher"],
            "params_sha256": a["params_sha256"],
            "neo4j_params_sha256": b["params_sha256"],
            "comparison": a["comparison"],
            "order_policy": ("schema DDL success; acknowledgement payloads disclosed separately" if ddl else
                             "canonical graph rows, internal IDs and label order normalized" if canonical else
                             "exact ordered rows" if a["ordered"] else "unordered row multiset"),
            "inputs_match": same_input,
            "graphqlite_rows": len(a["results"]), "neo4j_rows": len(b["results"]),
            "graphqlite_results_sha256": hashlib.sha256(encoded(a_rows).encode()).hexdigest(),
            "neo4j_results_sha256": hashlib.sha256(encoded(b_rows).encode()).hexdigest(),
            "match": same_input and operation_match,
            "strict_results_match": same_input and same_results,
            **({"graphqlite_results": a["results"], "neo4j_results": b["results"],
                "graphqlite_success": a.get("success"), "neo4j_success": b.get("success"),
                "policy": "Schema DDL returns no Cypher data rows. Compare execution success; disclose acknowledgement differences without treating them as data parity."} if ddl else
               {} if canonical else {"graphqlite_sample": a["results"][:3], "neo4j_sample": b["results"][:3]}),
        })
    return {
        "passed": len(left) == len(right) and all(entry["match"] for entry in entries),
        "strict_result_parity": len(left) == len(right) and all(entry.get("strict_results_match", False) for entry in entries),
        "data_result_parity": len(left) == len(right) and all(entry["match"] for entry in entries
                                                              if entry.get("comparison") != "schema_ddl"),
        "ddl_acknowledgement_parity": all(entry["strict_results_match"] for entry in entries
                                         if entry.get("comparison") == "schema_ddl"),
        "graphqlite_binary_sha256": graphqlite["binary_sha256"],
        "neo4j_binary_sha256": neo4j["binary_sha256"],
        "graphqlite_query_count": len(left), "neo4j_query_count": len(right),
        "direct_query_count": sum(entry.get("comparison") == "direct" for entry in entries),
        "canonical_graph_query_count": sum(entry.get("comparison") == "canonical_graph" for entry in entries),
        "schema_ddl_query_count": sum(entry.get("comparison") == "schema_ddl" for entry in entries),
        "queries": entries,
    }


def batches(rows, max_bytes=60000):
    # --params is one argv string; stay below Linux's per-argument size limit.
    batch = []
    size = 20
    for row in rows:
        length = len(encoded(row).encode()) + 1
        require(length < max_bytes, "One object exceeds the CLI parameter batch limit")
        if batch and (size + length > max_bytes or len(batch) >= 250):
            yield batch
            batch, size = [], 20
        batch.append(row)
        size += length
    if batch:
        yield batch


def normalize_snapshot(data):
    require(data.get("schema_version") == 1, "Expected AD snapshot schema_version 1")
    require(isinstance(data.get("domain"), dict), "Snapshot must contain one domain object")
    nodes = []
    by_dn, by_sid, by_id = {}, {}, {}
    unresolved = []

    def add(raw, label):
        require(isinstance(raw, dict), label + " must be an object")
        props = {key: raw[key] for key in FIELDS[label] if key in raw and raw[key] is not None}
        require(isinstance(props.get("name"), str) and props["name"], label + " requires name")
        for key, value in props.items():
            if key in ("memberOf", "members"):
                require(isinstance(value, list) and all(isinstance(item, str) for item in value),
                        label + "." + key + " must be a string list")
            elif key == "enabled":
                require(type(value) is bool, label + ".enabled must be a boolean")
            elif key == "primaryGroupID":
                require(type(value) is int and value >= 0, label + ".primaryGroupID must be a nonnegative integer")
            else:
                require(isinstance(value, str), label + "." + key + " must be a string")
        sid, guid = props.get("objectSid"), props.get("objectGUID")
        require(bool(sid or guid), label + " needs objectSid or objectGUID")
        identity = "sid:" + sid.upper() if sid else "guid:" + guid.strip("{}").lower()
        require(identity not in by_id, "Duplicate collected object identity: " + identity)
        props["id"] = identity
        node = {"id": identity, "label": label, "properties": props}
        nodes.append(node)
        by_id[identity] = node
        if sid:
            require(sid.upper() not in by_sid, "Duplicate SID: " + sid)
            by_sid[sid.upper()] = node
        dn = props.get("distinguishedName")
        if dn:
            require(dn.casefold() not in by_dn, "Duplicate distinguished name: " + dn)
            by_dn[dn.casefold()] = node
        return node

    domain = add(data["domain"], "ADDomain")
    counts = {"ADDomain": 1}
    for key, label in SOURCES:
        rows = data.get(key, [])
        require(isinstance(rows, list), key + " must be an array")
        counts[label] = len(rows)
        for raw in rows:
            add(raw, label)
    require(len(nodes) > 1, "Snapshot must contain collected AD objects beyond the domain")

    membership = {}

    def member(source, target, kind):
        key = (source["id"], target["id"])
        flags = membership.setdefault(key, {"explicit": False, "primary": False})
        flags[kind] = True

    def resolve(dn, owner, relation):
        node = by_dn.get(dn.casefold())
        if node is None:
            unresolved.append({"object": owner["id"], "reference": dn, "kind": relation})
        return node

    for node in nodes:
        props = node["properties"]
        for dn in props.get("memberOf", []):
            group = resolve(dn, node, "memberOf")
            if group:
                require(group["label"] == "ADGroup", "memberOf resolves to a non-group")
                member(node, group, "explicit")
        if node["label"] == "ADGroup":
            for dn in props.get("members", []):
                principal = resolve(dn, node, "members")
                if principal:
                    member(principal, node, "explicit")
        if node["label"] in ("ADUser", "ADComputer") and "primaryGroupID" in props:
            sid = props.get("objectSid")
            require(sid and "-" in sid, "Primary group resolution requires a SID")
            group_sid = sid.rsplit("-", 1)[0] + "-" + str(props["primaryGroupID"])
            group = by_sid.get(group_sid.upper())
            if group:
                require(group["label"] == "ADGroup", "Primary group SID resolves to a non-group")
                member(node, group, "primary")
            else:
                unresolved.append({"object": node["id"], "reference": group_sid, "kind": "primaryGroupID"})

    edges = [{"source": source, "target": target, "kind": "MEMBER_OF", "properties": flags}
             for (source, target), flags in sorted(membership.items())]
    edges += [{"source": node["id"], "target": domain["id"], "kind": "IN_DOMAIN", "properties": {}}
              for node in nodes if node is not domain]
    trusts = data.get("trusts", [])
    require(isinstance(trusts, list), "trusts must be an array")
    safe_trusts = []
    for trust in trusts:
        require(isinstance(trust, dict), "Each trust must be an object")
        safe = {key: trust[key] for key in TRUST_FIELDS if key in trust and trust[key] is not None}
        require(all(isinstance(value, str) for key, value in safe.items() if key != "intraForest"),
                "Trust names, direction and type must be strings")
        require("intraForest" not in safe or type(safe["intraForest"]) is bool,
                "Trust intraForest must be a boolean")
        safe_trusts.append(safe)
    return {
        "nodes": nodes, "edges": edges, "counts": counts,
        "unresolved_references": unresolved, "trusts": safe_trusts,
        "trust_note": "Trust observations are retained here; foreign domains were not collected and no placeholder nodes or trust edges are inferred.",
        "source_metadata": {key: value for key, value in data.get("source", {}).items()
                            if key in SOURCE_FIELDS and isinstance(value, (str, bool, int, float))},
    }


class ADValidation:
    def __init__(self, args, directory, model, backend="graphqlite"):
        self.args, self.directory, self.model, self.backend = args, directory, model, backend
        self.binary = args.neo4j_binary if backend == "neo4j" and args.neo4j_binary else args.binary
        self.graph = directory / "graph.db"
        self.commands = 0
        self.report = {"backend": backend, "passed": False, "checks": [], "queries": [],
                       "scope": "Test-only on-premises AD snapshot adapter through Cirro graph query",
                       "comparison_normalization": ["Internal graph IDs", "Label order", "JSON object key order"],
                       "source": {"path": str(args.source), "sha256": sha256(args.source),
                                  "bytes": args.source.stat().st_size},
                       "binary": str(self.binary), "binary_sha256": sha256(self.binary), "input_counts": model["counts"],
                       "source_metadata": model["source_metadata"],
                       "unresolved_references": model["unresolved_references"],
                       "trust_observations": model["trusts"], "trust_note": model["trust_note"]}
        if backend == "graphqlite":
            # Query deliberately refuses nonexistent graph paths. Create only
            # this new empty file, inside the exclusively created work folder.
            self.graph.touch(exist_ok=False)
            self.report["extension"] = ({"mode": "override", "sha256": sha256(args.extension)}
                                        if args.extension else {"mode": "embedded"})

    def connection(self):
        if self.backend == "neo4j":
            return ["--backend", "neo4j", "--server", self.args.neo4j_server,
                    "--user", self.args.neo4j_user, "--password",
                    os.environ[self.args.neo4j_password_env]]
        options = ["--backend", "graphqlite", "--graph-file", str(self.graph)]
        if self.args.extension:
            options += ["--graphqlite-extension", str(self.args.extension)]
        return options

    def command(self, arguments):
        self.commands += 1
        try:
            process = subprocess.run([str(self.binary), "graph", *arguments, *self.connection()],
                                     cwd=ROOT, text=True, capture_output=True, timeout=self.args.timeout)
        except subprocess.TimeoutExpired:
            # TimeoutExpired includes all argv in its default string, including
            # the Neo4j password and source data in --params.
            raise RuntimeError("CLI command %d exceeded %d seconds" %
                               (self.commands, self.args.timeout)) from None
        log = self.directory / ("command-%03d.log" % self.commands)
        # Never log argv, parameters, credentials, or query stdout.
        stderr = process.stderr
        secret = os.environ.get(self.args.neo4j_password_env)
        if secret:
            stderr = stderr.replace(secret, "<redacted>")
        log.write_text(stderr)
        require(process.returncode == 0, "CLI command %d failed; see %s" % (self.commands, log))
        return process.stdout

    def query(self, name, cypher, params=None, *, ordered=False, comparison="direct"):
        # This fixed query catalog explicitly marks top-level ORDER BY queries.
        # Nested expressions and list values never inherit row-order treatment.
        parameters = encoded(params or {})
        rows = json.loads(self.command(["query", "--cypher", cypher, "--params", parameters]))
        require(isinstance(rows, list), name + " did not return rows")
        self.report["queries"].append({"name": name, "cypher": cypher,
                                       "params_sha256": hashlib.sha256(parameters.encode()).hexdigest(),
                                       "ordered": ordered, "comparison": comparison, "success": True, "results": rows})
        return rows

    def check(self, name, condition):
        require(condition, name)
        self.report["checks"].append(name)

    def load(self):
        for label in FIELDS:
            rows = [{"id": node["id"], "properties": node["properties"]}
                    for node in self.model["nodes"] if node["label"] == label]
            for batch in batches(rows):
                result = self.query("load " + label,
                    "UNWIND $batch AS row MERGE (n:ADObject {id:row.id}) SET n:" + label +
                    " SET n += row.properties RETURN count(n) AS count", {"batch": batch})
                self.check(label + " batch loaded", result == [{"count": len(batch)}])
        for kind in ("MEMBER_OF", "IN_DOMAIN"):
            for batch in batches([edge for edge in self.model["edges"] if edge["kind"] == kind]):
                result = self.query("load " + kind,
                    "UNWIND $batch AS row MATCH (a:ADObject {id:row.source}), (b:ADObject {id:row.target}) "
                    "MERGE (a)-[r:" + kind + "]->(b) SET r += row.properties RETURN count(r) AS count",
                    {"batch": batch})
                self.check(kind + " batch resolves every endpoint", result == [{"count": len(batch)}])

    def snapshot(self):
        nodes = self.query("complete node snapshot",
                           "MATCH (n) RETURN id(n) AS internal, labels(n) AS labels, properties(n) AS properties",
                           comparison="canonical_graph")
        edges = self.query("complete relationship snapshot",
                           "MATCH (a)-[r]->(b) RETURN id(a) AS source, id(b) AS target, type(r) AS kind, properties(r) AS properties",
                           comparison="canonical_graph")
        keys = {node["internal"]: encoded([sorted(node["labels"]), node["properties"]]) for node in nodes}
        self.check("internal node identities are unique", len(keys) == len(nodes))
        value = [sorted(keys.values()),
                 sorted(encoded([keys[edge["source"]], edge["kind"], keys[edge["target"]], edge["properties"]])
                        for edge in edges)]
        self.report["queries"][-2]["canonical_results"] = value[0]
        self.report["queries"][-1]["canonical_results"] = value[1]
        return value

    def validate_source_model(self):
        expected_nodes = sorted(encoded([sorted(["ADObject", node["label"]]), node["properties"]])
                                for node in self.model["nodes"])
        snapshot = self.snapshot()
        self.check("all allowlisted node properties and exact value types match the source", snapshot[0] == expected_nodes)
        node_keys = {node["id"]: encoded([sorted(["ADObject", node["label"]]), node["properties"]])
                     for node in self.model["nodes"]}
        expected_edges = sorted(encoded([node_keys[edge["source"]], edge["kind"],
                                         node_keys[edge["target"]], edge["properties"]])
                                for edge in self.model["edges"])
        self.check("all directed relationships exactly match resolved source facts", snapshot[1] == expected_edges)
        memberships = self.query("direct and primary group memberships",
            "MATCH (a:ADObject)-[r:MEMBER_OF]->(g:ADGroup) RETURN a.id AS source, g.id AS target, "
            "r.explicit AS explicit, r.primary AS primary ORDER BY source, target", ordered=True)
        expected = [{"source": edge["source"], "target": edge["target"], **edge["properties"]}
                    for edge in self.model["edges"] if edge["kind"] == "MEMBER_OF"]
        self.check("direct and primary memberships have exact endpoints and boolean flags",
                   encoded(memberships) == encoded(expected))
        count = self.query("domain membership count",
            "MATCH (:ADObject)-[:IN_DOMAIN]->(:ADDomain) RETURN count(*) AS count")
        self.check("every collected non-domain object belongs to the domain",
                   count == [{"count": len(self.model["nodes"]) - 1}])
        for label, total in self.model["counts"].items():
            rows = self.query(label + " count", "MATCH (n:" + label + ") RETURN count(n) AS count")
            self.check(label + " count matches collection", rows == [{"count": total}])
        self.check("relationship count matches resolved source facts", len(snapshot[1]) == len(self.model["edges"]))
        return snapshot

    def expression_probe(self):
        rows = self.query("scoped CALL and conditional typed SET",
            "MATCH (d:ADDomain) CALL (d) { "
            "WHEN true THEN { SET d += {__cirroAdBool:true, __cirroAdInt:42, "
            "__cirroAdList:[v IN [1,2] | v+1], __cirroAdText:'[1,2]'} } "
            "WHEN false THEN { SET d.__cirroAdNever=true } } "
            "RETURN d.__cirroAdBool AS flag, d.__cirroAdInt AS number, d.__cirroAdList AS values, "
            "d.__cirroAdText AS text, d.__cirroAdNever AS absent")
        self.check("CALL and WHEN preserve boolean, integer, list and JSON-looking string types",
                   encoded(rows) == encoded([{"flag": True, "number": 42, "values": [2, 3],
                                             "text": "[1,2]", "absent": None}]))
        self.query("remove expression probe properties",
            "MATCH (d:ADDomain) REMOVE d.__cirroAdBool, d.__cirroAdInt, d.__cirroAdList, "
            "d.__cirroAdText, d.__cirroAdNever RETURN count(d) AS count")

    def named_queries(self):
        by_id = {node["id"]: node for node in self.model["nodes"]}
        memberships = [edge for edge in self.model["edges"] if edge["kind"] == "MEMBER_OF"]
        rows = self.query("user and group membership names",
            "MATCH (u:ADUser)-[r:MEMBER_OF]->(g:ADGroup) RETURN u.name AS userName, "
            "g.name AS groupName, r.primary AS primary, r.explicit AS explicit")
        expected = [{"userName": by_id[edge["source"]]["properties"]["name"],
                     "groupName": by_id[edge["target"]]["properties"]["name"], **edge["properties"]}
                    for edge in memberships if by_id[edge["source"]]["label"] == "ADUser"]
        self.check("named user-group memberships match independently resolved collection facts",
                   sorted(map(encoded, rows)) == sorted(map(encoded, expected)))
        for enabled in (True, False):
            rows = self.query(("enabled" if enabled else "disabled") + " user count",
                "MATCH (u:ADUser) WHERE u.enabled = $enabled RETURN count(u) AS count",
                {"enabled": enabled})
            expected_count = sum(node["label"] == "ADUser" and
                                 node["properties"].get("enabled") is enabled for node in self.model["nodes"])
            self.check(("enabled" if enabled else "disabled") + " user count matches source booleans",
                       rows == [{"count": expected_count}])
        domain = next(node["properties"] for node in self.model["nodes"] if node["label"] == "ADDomain")
        rows = self.query("computers and domain DNS names",
            "MATCH (c:ADComputer)-[:IN_DOMAIN]->(d:ADDomain) RETURN c.name AS computerName, "
            "c.dnsHostName AS dnsHostName, c.operatingSystem AS operatingSystem, d.dnsRoot AS domainDNS")
        expected = [{"computerName": node["properties"]["name"],
                     "dnsHostName": node["properties"].get("dnsHostName"),
                     "operatingSystem": node["properties"].get("operatingSystem"),
                     "domainDNS": domain.get("dnsRoot")}
                    for node in self.model["nodes"] if node["label"] == "ADComputer"]
        self.check("computer and domain DNS results match source records",
                   sorted(map(encoded, rows)) == sorted(map(encoded, expected)))
        rows = self.query("nested group membership names",
            "MATCH (child:ADGroup)-[:MEMBER_OF]->(parent:ADGroup) "
            "RETURN child.name AS childGroup, parent.name AS parentGroup")
        expected = [{"childGroup": by_id[edge["source"]]["properties"]["name"],
                     "parentGroup": by_id[edge["target"]]["properties"]["name"]}
                    for edge in memberships if by_id[edge["source"]]["label"] == "ADGroup"]
        self.check("nested group relationships match collected direct membership",
                   sorted(map(encoded, rows)) == sorted(map(encoded, expected)))
        targets = {node["id"] for node in self.model["nodes"]
                   if node["label"] == "ADGroup" and node["properties"]["name"] == "Domain Admins"}
        adjacency = {}
        for edge in memberships:
            adjacency.setdefault(edge["source"], set()).add(edge["target"])
        accounts = set()
        for node in self.model["nodes"]:
            if node["label"] != "ADUser":
                continue
            frontier, visited = {node["id"]}, {node["id"]}
            for _ in range(5):
                frontier = {target for source in frontier for target in adjacency.get(source, ())}
                if frontier & targets:
                    accounts.add(node["properties"].get("samAccountName"))
                    break
                frontier -= visited
                visited.update(frontier)
                if not frontier:
                    break
        rows = self.query("users reaching Domain Admins within five membership hops",
            "MATCH (u:ADUser)-[:MEMBER_OF*1..5]->(g:ADGroup) WHERE g.name = $group "
            "RETURN DISTINCT u.samAccountName AS account ORDER BY account", {"group": "Domain Admins"}, ordered=True)
        expected = [{"account": account} for account in accounts]
        self.check("bounded Domain Admins membership paths match independent source traversal",
                   sorted(map(encoded, rows)) == sorted(map(encoded, expected)))
        rows = self.query("group direct-member counts including empty groups",
            "MATCH (g:ADGroup) OPTIONAL MATCH (m:ADObject)-[:MEMBER_OF]->(g) "
            "RETURN g.id AS groupId, count(m) AS directMembers ORDER BY groupId", ordered=True)
        expected = [{"groupId": node["id"],
                     "directMembers": sum(edge["target"] == node["id"] for edge in memberships)}
                    for node in sorted(self.model["nodes"], key=lambda item: item["id"])
                    if node["label"] == "ADGroup"]
        self.check("OPTIONAL MATCH aggregation retains empty groups with zero members", encoded(rows) == encoded(expected))

        # Cypher variable-length paths are trails: a relationship may not be
        # reused, but a vertex can be revisited. Count each distinct trail.
        path_counts = {}
        def visit(user_id, current, depth, used_edges):
            if depth and current in targets:
                key = (user_id, depth)
                path_counts[key] = path_counts.get(key, 0) + 1
            if depth == 5:
                return
            for target in adjacency.get(current, ()):
                edge = (current, target)
                if edge not in used_edges:
                    visit(user_id, target, depth + 1, used_edges | {edge})
        for node in self.model["nodes"]:
            if node["label"] == "ADUser":
                visit(node["id"], node["id"], 0, set())
        rows = self.query("Domain Admins membership path multiplicity",
            "MATCH p=(u:ADUser)-[:MEMBER_OF*1..5]->(g:ADGroup) WHERE g.name=$group "
            "RETURN u.id AS userId, length(p) AS hops, count(*) AS paths ORDER BY userId,hops",
            {"group": "Domain Admins"}, ordered=True)
        expected = [{"userId": user, "hops": hops, "paths": count}
                    for (user, hops), count in sorted(path_counts.items())]
        self.check("path multiplicity and lengths match independent source trail enumeration", encoded(rows) == encoded(expected))

        rows = self.query("ordered collection of each user's direct group IDs",
            "MATCH (u:ADUser)-[:MEMBER_OF]->(g:ADGroup) WITH u,g ORDER BY u.id,g.id "
            "RETURN u.id AS userId,collect(g.id) AS groups ORDER BY userId", ordered=True)
        expected = [{"userId": node["id"], "groups": sorted(adjacency[node["id"]])}
                    for node in sorted(self.model["nodes"], key=lambda item: item["id"])
                    if node["label"] == "ADUser" and adjacency.get(node["id"])]
        self.check("ordered collect preserves source-derived group list order", encoded(rows) == encoded(expected))

        rows = self.query("stable ordered object pagination",
            "MATCH (n:ADObject) RETURN n.id AS id,n.name AS name ORDER BY name,id SKIP $skip LIMIT $limit",
            {"skip": 3, "limit": 7}, ordered=True)
        ordered_nodes = sorted(self.model["nodes"], key=lambda node: (node["properties"]["name"], node["id"]))
        expected = [{"id": node["id"], "name": node["properties"]["name"]} for node in ordered_nodes[3:10]]
        self.check("ordered SKIP and LIMIT return the exact source-derived page", encoded(rows) == encoded(expected))

    def execute(self):
        start = time.monotonic()
        self.check("target graph starts empty",
                   self.query("empty target guard", "MATCH (n) RETURN count(n) AS count") == [{"count": 0}])
        self.query("AD identity constraint",
                   "CREATE CONSTRAINT ad_object_identity IF NOT EXISTS FOR (n:ADObject) REQUIRE n.id IS UNIQUE",
                   comparison="schema_ddl")
        self.load()
        before = self.validate_source_model()
        self.named_queries()
        self.expression_probe()
        self.check("temporary expression checks leave real data unchanged", self.snapshot() == before)
        self.load()
        self.check("repeated load is idempotent", self.snapshot() == before)
        self.check("separate CLI processes read the persisted graph", self.validate_source_model() == before)
        if self.backend == "graphqlite":
            self.check("graph persists to a separate nonempty SQLite file",
                       self.graph.stat().st_size > 0 and self.graph.resolve() != self.args.source)
        path = self.directory / "opengraph.json"
        self.command(["export", "--format", "opengraph", "--output", str(path)])
        graph = json.loads(path.read_text())["graph"]
        keys = {node["id"]: encoded([sorted(node["kinds"]), node["properties"]]) for node in graph["nodes"]}
        self.check("OpenGraph node IDs are unique", len(keys) == len(graph["nodes"]))
        self.check("OpenGraph relationship endpoints resolve",
                   all(edge["start"]["value"] in keys and edge["end"]["value"] in keys for edge in graph["edges"]))
        exported = [sorted(keys.values()),
                    sorted(encoded([keys[edge["start"]["value"]], edge["kind"],
                                    keys[edge["end"]["value"]], edge["properties"]]) for edge in graph["edges"])]
        self.check("OpenGraph retains all labels, properties, types and directed relationships", exported == before)
        self.command(["export", "--format", "grass", "--output", str(self.directory / "styles")])
        styles = (self.directory / "styles.grass").read_text()
        self.check("GraSS includes every collected AD label",
                   all("node." + label + " {" in styles for label, count in self.model["counts"].items() if count))
        self.check("queries and exports do not mutate data", self.snapshot() == before)
        self.report.update({"passed": True, "nodes": len(before[0]), "edges": len(before[1]),
                            "snapshot_sha256": hashlib.sha256(encoded(before).encode()).hexdigest(),
                            "export_sha256": hashlib.sha256(encoded(exported).encode()).hexdigest(),
                            "cli_commands": self.commands, "elapsed_seconds": round(time.monotonic() - start, 3)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Read-only schema-version-1 AD snapshot JSON")
    parser.add_argument("--binary", type=Path, default=ROOT / "target/release/cirro")
    parser.add_argument("--neo4j-binary", type=Path, help="Separate default-feature Cirro binary for the Neo4j baseline")
    parser.add_argument("--extension", type=Path, help="Optional development extension override")
    parser.add_argument("--work-dir", type=Path, help="New, nonexistent output directory")
    parser.add_argument("--neo4j-server", help="Optional EMPTY disposable Neo4j baseline")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password-env", default="CIRRO_NEO4J_PASSWORD")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    for name in ("source", "binary", "neo4j_binary", "extension"):
        value = getattr(args, name)
        if value:
            setattr(args, name, value.resolve(strict=True))
            require(getattr(args, name).is_file(), name + " must be a file")
    if args.neo4j_server:
        require(bool(os.environ.get(args.neo4j_password_env)), "Neo4j password environment variable is missing")
    require(not args.neo4j_binary or args.neo4j_server, "--neo4j-binary requires --neo4j-server")
    source_hash = sha256(args.source)
    model = normalize_snapshot(json.loads(args.source.read_text(encoding="utf-8-sig")))
    directory = args.work_dir.resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="cirro-ad-validation-"))
    if args.work_dir:
        directory.mkdir(parents=True, exist_ok=False)
    tests = []
    error = None
    parity = None
    try:
        for engine in ["graphqlite"] + (["neo4j"] if args.neo4j_server else []):
            output = directory / engine
            output.mkdir()
            test = ADValidation(args, output, model, engine)
            tests.append(test)
            test.execute()
        if len(tests) == 2:
            parity = compare_query_traces(tests[0].report, tests[1].report)
            (directory / "query-parity.json").write_text(json.dumps(parity, indent=2) + "\n")
            tests[0].check("paired data-result queries match typed results and identical schema DDL succeeds",
                           parity["passed"])
            tests[0].check("GraphQLite and Neo4j canonical graph snapshots match",
                           tests[0].report["snapshot_sha256"] == tests[1].report["snapshot_sha256"])
            tests[0].check("GraphQLite and Neo4j OpenGraph exports match",
                           tests[0].report["export_sha256"] == tests[1].report["export_sha256"])
    except Exception as exc:
        error = str(exc)
        if tests:
            tests[-1].report.update({"passed": False, "error": error})
    finally:
        unchanged = args.source.is_file() and sha256(args.source) == source_hash
        for test in tests:
            test.report["source_unchanged"] = unchanged
            test.report["cli_commands"] = test.commands
            if not unchanged:
                test.report.update({"passed": False, "error": "Source snapshot changed during validation"})
            (test.directory / "report.json").write_text(json.dumps(test.report, indent=2) + "\n")
    report = {"passed": error is None and unchanged, "source_sha256": source_hash,
              "source_unchanged": unchanged, "input_counts": model["counts"],
              "unresolved_references": len(model["unresolved_references"]),
              "trust_observations": len(model["trusts"]),
              "backends": [{"backend": test.backend, "report": str(test.directory / "report.json"),
                            "passed": test.report["passed"]} for test in tests]}
    if parity is not None:
        report["query_parity"] = {"path": str(directory / "query-parity.json"), "passed": parity["passed"],
                                  "strict_result_parity": parity["strict_result_parity"],
                                  "data_result_parity": parity["data_result_parity"],
                                  "ddl_acknowledgement_parity": parity["ddl_acknowledgement_parity"],
                                  "direct_queries": parity["direct_query_count"],
                                  "canonical_graph_queries": parity["canonical_graph_query_count"],
                                  "schema_ddl_queries": parity["schema_ddl_query_count"]}
    if error:
        report["error"] = error
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "report": str(directory / "report.json")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
