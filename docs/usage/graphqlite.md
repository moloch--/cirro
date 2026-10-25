# Optional GraphQLite backend

GraphQLite stores Cirro's graph in a local SQLite file. Neo4j remains the default
backend, and ordinary builds retain the existing Neo4j behavior. Enable the
`graphqlite` Cargo feature when building a binary that also supports local graphs.
Collection commands and source data formats are unchanged.

## Release packages

Download a `cirro-graphqlite-<target>.tar.xz` archive (or `.zip` on Windows) from
the [fork's releases](https://github.com/moloch--/cirro/releases). Extract it and
place `cirro` or `cirro.exe` on your PATH. These packages include all default
features plus GraphQLite. The extension is embedded in the executable; there is
no separate extension installation or database server to configure. Neo4j
remains the default backend, so select `--backend graphqlite` for local graphs.

The archives include Cirro, GraphQLite and PCRE2 licenses, a SHA-256 checksum
alongside the download, and `build-info.json` recording the Cirro and GraphQLite
revisions and binary and embedded-extension hashes. The original `cirro-<target>` packages and their
installers continue to provide the default-feature build.

GraphQLite packages cover the same six release targets: macOS Intel and Apple
Silicon, Linux GNU x86-64 and ARM64, Linux musl x86-64, and Windows x86-64.
GNU builds require the system libraries provided by Ubuntu 22.04 or newer
(x86-64), or Ubuntu 24.04 or newer (ARM64). The musl package is built for
Alpine 3.23 and uses its system musl loader, `/lib/ld-musl-x86_64.so.1`, because
loading an extension requires dynamic linking. The original musl package
remains static. Windows packages include the extension's non-system
dependencies statically. All platforms need a writable temporary directory
that permits loading shared libraries.

The release workflow tests each downloaded archive on a fresh runner, with
no checkout or GraphQLite installation, before allowing release publication.
The **GraphQLite release packages** workflow can also be run manually to build
and validate all archives or one selected target without creating a tag or
publishing a release. Tagged releases always build and verify all six targets.

## Build

The build helper reads the GraphQLite revision from `Cargo.toml`, fetches that
exact commit, builds its native extension, and embeds it in the Cirro binary.
The current pin is
[`cc044d4b71f6801c23ba69a801a274dfcdc78a2c`](https://github.com/moloch--/graphqlite/commit/cc044d4b71f6801c23ba69a801a274dfcdc78a2c).

```sh
tools/build-graphqlite.sh --locked
```

This requires Rust, Git, Python 3.11 or newer, Make, a C compiler, Bison and Flex.
On macOS, install Homebrew Bison and Flex; the helper adds them to the build path.
The extension is built directly with the pinned fork's Makefile.

If the pinned native extension is already built, use it directly:

```sh
CIRRO_GRAPHQLITE_EXTENSION=/absolute/path/graphqlite.dylib \
  cargo build --release --features graphqlite
```

Use a `.so` extension on Linux. The native library must match the target operating
system and architecture. The resulting binary includes it and extracts it to a
private temporary directory when opening a GraphQLite connection; users do not
need a separate extension file. `--graphqlite-extension PATH` overrides the
embedded library for development.

The fork implements the Cypher constructs and APOC subset used by Cirro's
templates. It is not a complete Neo4j or APOC implementation. In particular,
`apoc.periodic.iterate` runs sequentially in one atomic SQLite transaction, even
when a template requests parallel batches. Cypher's `=~` operator uses bundled
PCRE2 with whole-string matching and inline flags such as `(?i)`; APOC regular
expression helpers still use POSIX expressions. See the
[fork's compatibility reference](https://github.com/moloch--/graphqlite/blob/cc044d4b71f6801c23ba69a801a274dfcdc78a2c/docs/src/content/docs/reference/apoc-compatibility.md)
for the supported subset.

At this revision, accessing a resource's `id` property inside a `nodes(path)`
list comprehension (for example, `[n IN nodes(p) | n.id]`) returns internal node
IDs instead. Query the `id` property on a directly bound node, such as
`MATCH (n) RETURN n.id`, when resource identity matters. The end-to-end report
records this known query limitation separately from import/export parity.

## Import, query and export

Keep the collected source database and output graph at different paths:

```sh
cirro graph ingest --backend graphqlite --graph-file analysis.db \
  --type az --file collected-azure.db

cirro graph ingest --backend graphqlite --graph-file analysis.db \
  --type ts-status --file tailscale-status.json
```

The existing ingestion specifications, label filters and postprocessing run
against the selected backend. Preview an import without creating or changing a
graph, or run only postprocessing:

```sh
cirro graph ingest --backend graphqlite --graph-file analysis.db \
  --type az --file collected-azure.db --dry-run

cirro graph ingest --backend graphqlite --graph-file analysis.db \
  --type az --file collected-azure.db --post-process
```

Query the graph using Cypher and JSON parameters. Results are written as JSON:

```sh
cirro graph query --backend graphqlite --graph-file analysis.db \
  --cypher 'MATCH (u:GraphUser)-[:MEMBER_OF]->(g:GraphGroup) RETURN u.displayName AS user, g.displayName AS group'

cirro graph query --backend graphqlite --graph-file analysis.db \
  --cypher 'MATCH (u:GraphUser {id:$id}) RETURN properties(u) AS properties' \
  --params '{"id":"user-id"}'

cirro graph query --backend graphqlite --graph-file analysis.db \
  --cypher 'MATCH (t:TSUser)-[:SAME_USER]->(u:GraphUser) RETURN t.loginName AS tailscale, u.userPrincipalName AS azure'
```

Ingestion creates the graph file if needed. Queries and exports require an
existing file so a mistyped path cannot silently create an empty graph. To start
a graph directly with Cypher, create an empty file first (`touch analysis.db` on
Unix, or `New-Item analysis.db` in PowerShell).

Both existing export formats are available:

```sh
cirro graph export --backend graphqlite --graph-file analysis.db \
  --format opengraph --output graph.json

cirro graph export --backend graphqlite --graph-file analysis.db \
  --format grass --output graph
```

The second command writes `graph.grass`. OpenGraph exports preserve graph entity
IDs for relationship endpoints; these internal IDs are separate from each
resource's `id` property.

## Reproduce the end-to-end checks

The repository does not include collected cloud datasets. The test harness
generates **synthetic** Azure collector SQLite and Tailscale status JSON inputs,
then invokes the actual Cirro CLI for ingestion, queries, postprocessing and
both exports:

```sh
python3 tools/test_graphqlite_e2e.py --binary target/release/cirro
```

Add `--pagination-rows 2501` to verify the 2,500-row import batch boundary with
a separate synthetic source and graph. This checks the total, first, boundary
and last users; it is a correctness check, not a performance benchmark.

It uses a fresh temporary directory and prints the path to `report.json`.
The directory retains source fixtures, output graphs, exports and command logs
for inspection. The report contains source schemas and counts, hashes, executed
queries and check names. Supplied collection reports omit query-result property
values; synthetic compatibility diagnostics include expected and actual fixture
values under `known_limitations`.

Checks cover user/group membership, manager direction, Tailscale identity links,
nested network relationships, dynamic role types and properties, serialized VM
settings, vault version parents, node and edge deduplication, label filtering,
file persistence, repeated-import idempotence and dry-run behavior. Query checks
cover whole-string regular expressions, scoped `CALL UNION` result growth and
types, named paths and optional relationship lists. They also
check that ingestion cannot use its source as the graph, and that exporting
cannot overwrite the graph, including through hardlinks and symbolic links.
Source hashes must remain unchanged throughout the run.

To exercise an existing collection, supply one or both source options. The
harness reads the sources and writes a separate graph in a new output directory:

```sh
python3 tools/test_graphqlite_e2e.py --binary target/release/cirro \
  --azure-source /absolute/path/collected-azure.db \
  --tailscale-source /absolute/path/tailscale-status.json \
  --work-dir /absolute/path/new-test-output
```

Supplied datasets run import, persistence, idempotence, dry-run, postprocessing
and export checks. Fixture-specific expectations and injected duplicate test
nodes are limited to synthetic runs. Exported files contain the imported data,
so keep the output directory with the source collection.

For a backend comparison, point the harness at a disposable, empty Neo4j instance
with APOC enabled. It refuses to ingest into a nonempty baseline:

```sh
python3 tools/test_graphqlite_e2e.py --binary target/release/cirro \
  --neo4j-server bolt://127.0.0.1:17687 \
  --neo4j-user neo4j --neo4j-password TEST_PASSWORD
```

The comparison normalizes internal node IDs, label order and JSON object ordering
inside the serialized `VMExtension.settings` string. The injected duplicate
fixture's `E2E_OWNS.source` provenance values are sorted because its Cypher
`collect` has no defined order. Other property types and list order are preserved.
The report compares the imported graph before adding that fixture separately,
then checks the final nodes, directed relationships, properties and OpenGraph
exports. These checks exercise
the local ingestion pipeline; they do not perform live cloud collection or
establish performance limits for large deployments.

## Validate with an on-premises AD snapshot

For a lab with no Azure or Tailscale collection, the repository includes a
test-only AD adapter. This exercises the graph query and export paths using
collected directory objects. It does not add an AD collection command or an AD
ingestion type to Cirro.

Run the read-only PowerShell snapshot script on a domain controller, or a
domain-joined host with the ActiveDirectory module and directory read access:

```powershell
.\tools\collect_ad_snapshot.ps1 | Set-Content -Encoding utf8 ad-snapshot.json
```

The script emits selected users, groups, computers, OUs, domain identity and
trust observations. It excludes passwords, descriptions and arbitrary directory
attributes. Copy the JSON to the machine running Cirro, then run:

```sh
python3 tools/test_graphqlite_ad.py \
  --source /absolute/path/ad-snapshot.json \
  --binary target/release/cirro \
  --work-dir /absolute/path/new-ad-validation
```

The adapter uses explicit `ADUser`, `ADGroup`, `ADComputer`, `ADOU` and `ADDomain`
labels. It resolves direct membership and SID-derived primary group membership,
preserving booleans, integers and lists. References to objects outside the
snapshot are reported as unresolved. Trust observations remain in the report;
foreign domain nodes and trust relationships are not inferred. This is not a
complete forest, effective-permission or BloodHound collection.

Checks compare the graph with the source, repeat the load, reopen the database,
exercise scoped `CALL` and `WHEN` branches, and verify both export formats.
For a comparison against an empty disposable Neo4j instance, set
`CIRRO_NEO4J_PASSWORD` and add `--neo4j-server bolt://127.0.0.1:17687`.
The adapter refuses an existing output directory or a nonempty Neo4j database.
It compares complete graph contents and OpenGraph exports, normalizing internal
IDs, label order and JSON object key order.

To use a separate Cirro binary built with only its default features on the
Neo4j side:

```sh
CARGO_TARGET_DIR=target/neo4j-comparison cargo build --locked --release

python3 tools/test_graphqlite_ad.py \
  --source /absolute/path/ad-snapshot.json \
  --binary target/release/cirro \
  --neo4j-binary target/neo4j-comparison/release/cirro \
  --neo4j-server bolt://127.0.0.1:17687 \
  --work-dir /absolute/path/new-query-comparison
```

Set `CIRRO_NEO4J_PASSWORD` before running the comparison. Both binaries receive
identical Cypher and parameters. `query-parity.json` records the input and result
hashes for each paired execution. Data results preserve scalar types, duplicate
rows and list order; top-level row order is compared when the query specifies
`ORDER BY`. Snapshot queries compare graph identities semantically because
internal node IDs differ between engines.

Schema operation acknowledgments are reported separately: GraphQLite currently
returns a zero-valued mutation-statistics row for `CREATE CONSTRAINT`, while
Neo4j returns no rows. The report records this difference rather than treating
the raw results as identical.

Each run retains the graph, exports and reports under its output directory.
These files contain directory data and query results; keep them local with the
source snapshot. The source hash is checked after validation. Collected lab
snapshots are not included in the repository.
