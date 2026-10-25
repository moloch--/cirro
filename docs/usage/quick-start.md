# Setup

!!! tip "Prerequisites"

    - Valid cloud platform credentials (currently Azure)
    - A GraphQLite-enabled Cirro binary for standalone use, or a running Neo4j instance
    - Appropriate permissions for target cloud environment

!!! warning

    - The external Neo4j backend requires the [APOC plugin](https://neo4j.com/docs/apoc/current/).
  
## Install Cirro

For standalone use, build this fork with `tools/build-graphqlite.sh --locked`,
or select a `cirro-graphqlite-<target>` package from the
[fork's releases](https://github.com/moloch--/cirro/releases). The binary supports
both backends and includes its GraphQLite extension. See the
[GraphQLite guide](graphqlite.md) for build prerequisites and platform details.
The upstream installers below provide the Neo4j build.

=== "Shell Script (Unix/Linux/macOS)"

    ```bash
    curl -sSL https://github.com/bishopfox/cirro/releases/latest/download/cirro-installer.sh | sh
    ```

=== "PowerShell (Windows)"

    ```powershell
    irm https://github.com/bishopfox/cirro/releases/latest/download/cirro-installer.ps1 | iex
    ```

=== "Manual Download"

    Download pre-built binaries from the [releases page](https://github.com/bishopfox/cirro/releases).

## Collect Data (Azure)

```bash
# Using Azure CLI authentication
cirro collect az azcli

# Using client secret
cirro collect az client-secret \
  --client-id <CLIENT_ID> \
  --client-secret <CLIENT_SECRET> \
  --tenant-id <TENANT_ID>
```

## Ingest into Graph Database

For standalone use, ingest into a local graph file separate from the collection:

```bash
cirro graph ingest --backend graphqlite --graph-file analysis.db \
  --type az --file cirro_output.db

cirro graph query --backend graphqlite --graph-file analysis.db \
  --cypher 'MATCH (n) RETURN count(n) AS nodes'

cirro graph export --backend graphqlite --graph-file analysis.db \
  --format opengraph --output graph.json
```

For external Neo4j, set up the server before ingesting. The
[tools Compose file](https://github.com/moloch--/cirro/blob/main/tools/docker-compose.yml)
starts a database with APOC:

```bash
# For Neo4j
docker compose -f tools/docker-compose.yml up -d
```

```bash
# For Neo4j
cirro graph ingest --backend neo4j --type az --file cirro_output.db \
  --server bolt://localhost:7688 \
  --user neo4j \
  --password password
```

## Next Steps

After completing the quick start:

1. **Explore Your Data**: Use your graph database's query interface to explore the collected data
2. **Learn Query Patterns**: Check out our [dashboard examples](../analysis/dashboard.md) for common analysis patterns
3. **Set Up Visualization**: Configure dashboards and visualizations for your specific use case
4. **Advanced Features**: Explore data enrichment and custom collection options
