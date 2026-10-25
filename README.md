<p align='center'><img src='docs/logo.png' alt='logo' height="400"/><br>
</p>

Cirro is an extensible security research platform that enables researchers and penetration testers to collect, analyze, and visualize cloud environments and identity relationships through graph databases. Built with a modular architecture, Cirro can be extended to support multiple platforms and data sources.

You can check out the [Documentation](https://bishopfox.github.io/cirro) for more info.

## Features

- **Multi-platform Data Collection**: Extensible architecture supporting multiple cloud platforms and identity providers
- **Flexible Authentication**: Support for various authentication methods depending on the target platform
- **Cross-platform**: Available for Windows, macOS, and Linux
- **Modular Design**: Optional platform functionality through feature flags and extensible plugin architecture
- **Network Topology Analysis**: Support for network infrastructure platforms like Tailscale
- **Standalone Graphs**: Store and query graphs in a local SQLite file using the optional embedded GraphQLite backend, or connect to an external Neo4j database

## Architecture

Cirro has two main functional areas:

- **Collection (`cirro collect`)**: Gathers information from various platforms and APIs
- **Graph Operations (`cirro graph`)**: Manages graph database operations including data ingestion and export

The modular architecture uses feature flags to enable platform-specific functionality, allowing users to build only the components they need.

## CLI Structure

Cirro uses a hierarchical command structure organized by function and platform:

```
cirro <function> <platform> <command> [options]
```

### Data Collection

**Azure (collect az)**

```bash
# Available authentication methods:
cirro collect az azcli           # Azure CLI authentication
cirro collect az client-secret   # Client ID and secret
cirro collect az client-cert     # Client certificate
cirro collect az access-token    # Pre-obtained access token
cirro collect az user-pass       # Username and password
```

**Tailscale (collect ts)**

```bash
# Tailscale data collection
cirro collect ts <auth-method> [options]
```

### Graph Operations

**Data Ingestion (graph ingest)**

```bash
# Ingest collected data into graph database
cirro graph ingest --type <platform> --file <data-file> [database options]
```

**Data Export (graph export)**

```bash
# Export graph data to various formats
cirro graph export --format <format> [options]
```

## Installation

### Pre-built Binaries

The [fork's releases](https://github.com/moloch--/cirro/releases) provide standard
`cirro-<target>` packages for Neo4j and `cirro-graphqlite-<target>` packages with
both Neo4j and the standalone backend. GraphQLite packages embed the native
extension; local graphs need no database server or separate extension installation.

### Building from Source

```bash
git clone https://github.com/moloch--/cirro.git
cd cirro
cargo build --release
```

**NOTE: BUILDING WITH --RELEASE IS IMPORTANT FOR GRAPH FUNCTIONALITY SINCE IT EMBEDS YAML CONFIGURATION FILES IN THE BINARY!**

The binary will be available at `target/release/cirro`.

#### Build Options

By default, Cirro includes Azure and Tailscale collection plus the Neo4j backend.
This fork also supports an optional embedded GraphQLite backend. Build it with
`tools/build-graphqlite.sh`; see the [GraphQLite guide](docs/usage/graphqlite.md)
for local graph ingestion, Cypher queries, export and compatibility checks.
To build with specific features:

```bash
# Build with only collection features
cargo build --release --no-default-features --features collector

# Build with only graph features
cargo build --release --no-default-features --features graph

# Build with specific platform support
cargo build --release --no-default-features --features "azure"
cargo build --release --no-default-features --features "tailscale"
```

#### Standalone Quick Start

Build a binary with both database backends, then import collected data into a
separate local graph file:

```bash
tools/build-graphqlite.sh --locked

target/release/cirro graph ingest --backend graphqlite --graph-file analysis.db \
  --type az --file cirro_output.db

target/release/cirro graph query --backend graphqlite --graph-file analysis.db \
  --cypher 'MATCH (n) RETURN count(n) AS nodes'

target/release/cirro graph export --backend graphqlite --graph-file analysis.db \
  --format opengraph --output graph.json
```

The same binary supports `--backend neo4j` with `--server`, `--user`, and
`--password`. Neo4j remains the default when `--backend` is omitted.

## Data Ingestion

Cirro supports external Neo4j and local GraphQLite databases. For Neo4j, the
Docker Compose file in [tools](tools/) can start a database server. For standalone
use, select `--backend graphqlite --graph-file analysis.db` with a GraphQLite build.

After collecting data, ingest it into your graph database:

```bash
# Ingest data for specific platforms
cirro graph ingest --type az --file cirro_output.db         # Azure data
cirro graph ingest --type ts-status --file tailscale-status.json    # Tailscale data

# Specify custom database connection
cirro graph ingest --type az --file cirro_output.db \
  --server bolt://localhost:7687 \
  --user neo4j \
  --password password
```

## Dry-Run Mode

Preview what would be ingested and post-processed without writing any data to the graph database:

```bash
# Preview Azure ingestion and see which resource types have no implemented specs
cirro graph ingest --type az --file cirro_output.db --dry-run

# Preview Tailscale ingestion
cirro graph ingest --type ts-status --file tailscale-status.json --dry-run
```

In dry-run mode, Cirro will:

- Report each spec and its row count that **would** be processed
- Skip all graph writes (no ingestion or post-processing queries execute)
- For Azure, list resource types in the input data that have no implemented spec

## Dashboard

CirroDash can be located here: [https://github.com/bishopfox/cirrodash](https://github.com/bishopfox/cirrodash)

## Debug Mode

Enable debug logging for detailed information:

```bash
# Collection debug mode
cirro collect az azcli --debug

# Ingestion debug mode
cirro graph ingest --type az --file cirro_output.db --debug
```

---

**Note**: Cirro is designed for authorized security testing and research. Ensure you have proper permissions before running against any cloud or network environment.
