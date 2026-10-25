# Graph Operations

Cirro's graph functionality manages data ingestion into graph databases and data export operations.

Choose external Neo4j (the default) or standalone GraphQLite with
`--backend graphqlite --graph-file analysis.db`. GraphQLite requires a binary
built with `tools/build-graphqlite.sh --locked`, or a `cirro-graphqlite` release
package. It stores the graph locally and embeds the native extension. See the
[standalone guide](graphqlite.md) for setup, query examples and compatibility.

## Command Structure

```bash
cirro graph <command> [options]
```

## Data Ingestion

Ingest collected data into the selected graph backend:

```bash
cirro graph ingest --type <platform> --file <data-file> [options]
```

### Options

- `--type`, `-t`: Platform type (`az` for Azure, `ts-status` for Tailscale status JSON)
- `--file`, `-f`: Path to the collected data file for the selected platform
- `--backend`: Graph backend (`neo4j` by default; `graphqlite` in standalone builds)
- `--graph-file`: Local graph file for GraphQLite, separate from the collected input; created during ingestion
- `--server`, `-s`: Database server URL (default: `bolt://localhost:7687`)
- `--user`, `-u`: Database username (default: `neo4j`)
- `--password`, `-p`: Database password (default: `password`)
- `--db-name`, `-d`: Database name (default: `neo4j`)
- `--dry-run`: Preview what would be ingested without writing any data to the graph database
- `--debug`: Enable debug logging

### Supported Database Schemes

- `bolt://` - Unencrypted connection
- `bolt+s://` - Encrypted connection with full certificate validation
- `bolt+ssc://` - Encrypted connection with self-signed certificates
- `neo4j://` - Neo4j routing protocol
- `neo4j+s://` - Encrypted Neo4j routing
- `neo4j+ssc://` - Neo4j routing with self-signed certificates

### Examples

```bash
# Ingest Azure data with defaults
cirro graph ingest --type az --file cirro_output.db

# Ingest Azure data without a database server
cirro graph ingest --backend graphqlite --graph-file analysis.db \
  --type az --file cirro_output.db

# Ingest to custom database
cirro graph ingest \
  --type az \
  --file cirro_output.db \
  --server bolt://graph-server:7687 \
  --user admin \
  --password secretpass \
  --db-name azure-prod

# Ingest Tailscale data with debug logging
cirro graph ingest --type ts-status --file tailscale-status.json --debug

# Use encrypted connection
cirro graph ingest \
  --type az \
  --file cirro_output.db \
  --server bolt+s://secure-server:7687

# Dry-run: preview Azure ingestion and see unimplemented resource types
cirro graph ingest --type az --file cirro_output.db --dry-run

# Dry-run: preview Tailscale ingestion
cirro graph ingest --type ts-status --file tailscale-status.json --dry-run
```

### Dry-Run Mode

The `--dry-run` flag lets you preview an ingestion without writing any data to the graph database.
This is useful for validating a data file or auditing spec coverage before a full ingest.

When `--dry-run` is specified, Cirro will:

- Log each spec and the number of rows that **would** be processed
- Skip all graph writes (no ingest or post-processing queries are executed)
- Skip transaction finalization
- Print the list of post-processing specs that would run, in priority order
- **Azure only**: compare the resource types present in the input data against all implemented specs and report any resource types that have no spec (i.e., would be silently skipped during a real ingest)

!!! warning "Security Notice"

    - **Passwords**: Avoid default credentials in production
    - **TLS**: Use encrypted schemes (`bolt+s://`, `neo4j+s://`) for remote databases
    - **Credentials**: Prefer environment variables or secure secret stores

## Data Export

Export graph data to various formats:

```bash
cirro graph export --format <format> [options]
```

### Options

- `--format`, `-f`: Export format (`opengraph` or `grass`)
- `--output`, `-o`: Output file path (default: `cirro_export`)
- `--backend`: Graph backend (`neo4j` by default; `graphqlite` in standalone builds)
- `--graph-file`: Existing local graph file for GraphQLite
- `--server`, `-s`: Database server URL (default: `bolt://localhost:7687`)
- `--user`, `-u`: Database username (default: `neo4j`)
- `--password`, `-p`: Database password (default: `password`)
- `--db-name`, `-d`: Database name (default: `neo4j`)
- `--debug`: Enable debug logging

### Examples

```bash
# Export to OpenGraph format
cirro graph export --format opengraph --output my-export.json

# Export a standalone graph
cirro graph export --backend graphqlite --graph-file analysis.db \
  --format opengraph --output my-export.json

# Export from custom database
cirro graph export \
  --format opengraph \
  --output export.json \
  --server bolt://graph-server:7687 \
  --user admin \
  --password secretpass
```

## Database Setup

### Using Docker Compose

Cirro includes a docker-compose file for easy database setup:

```bash
cd tools
docker-compose up -d
```

This starts Neo4j with:
- Web UI at `http://localhost:7477`
- Bolt protocol at `bolt://localhost:7688`
- Default credentials: `neo4j/password`
