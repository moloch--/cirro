//! Database operations shared by ingestion, querying, and export.

use crate::errors::CirroError;
use log::info;
use neo4rs::{BoltType, ConfigBuilder, Graph, Node, Query, Relation, query};
use serde::Deserialize;
use serde_json::{Map, Value};
#[cfg(feature = "graphqlite")]
use std::{
    path::PathBuf,
    sync::{Arc, Mutex},
};

pub enum GraphConnectionConfig {
    Neo4j {
        host: String,
        user: String,
        password: String,
        db_name: Option<String>,
    },
    #[cfg(feature = "graphqlite")]
    Graphqlite {
        path: PathBuf,
        extension_path: Option<PathBuf>,
    },
}

#[derive(Clone)]
pub struct GraphBackend {
    inner: Backend,
}

#[derive(Clone)]
enum Backend {
    Neo4j(Graph),
    #[cfg(feature = "graphqlite")]
    Graphqlite(Arc<Mutex<EmbeddedGraph>>),
}

#[cfg(feature = "graphqlite")]
struct EmbeddedGraph {
    // The connection must close before its extracted library is removed.
    connection: graphqlite::Connection,
    _bundle: Option<super::graphqlite_bundle::BundledExtension>,
}

impl std::fmt::Debug for GraphBackend {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match &self.inner {
            Backend::Neo4j(_) => "GraphBackend::Neo4j",
            #[cfg(feature = "graphqlite")]
            Backend::Graphqlite(_) => "GraphBackend::Graphqlite",
        })
    }
}

#[derive(Debug, Deserialize)]
pub struct GraphNode {
    pub id: i64,
    pub labels: Vec<String>,
    pub properties: Map<String, Value>,
}

#[derive(Debug, Deserialize)]
pub struct GraphRelationship {
    pub id: i64,
    #[serde(rename = "startNode")]
    pub start_node_id: i64,
    #[serde(rename = "endNode")]
    pub end_node_id: i64,
    #[serde(rename = "type")]
    pub kind: String,
    pub properties: Map<String, Value>,
}

#[derive(Debug, Deserialize)]
pub struct LabelConnection {
    #[serde(rename = "sourceLabels")]
    pub source_labels: Vec<String>,
    #[serde(rename = "targetLabels")]
    pub target_labels: Vec<String>,
}

fn database_error(error: impl std::fmt::Display) -> CirroError {
    CirroError::DatabaseError(error.to_string())
}

fn neo4j_query(cypher: &str, params: Map<String, Value>) -> Result<Query, CirroError> {
    let mut statement = query(cypher);
    for (key, value) in params {
        statement = statement.param(&key, BoltType::try_from(value).map_err(database_error)?);
    }
    Ok(statement)
}

impl GraphBackend {
    pub async fn connect(config: GraphConnectionConfig) -> Result<Self, CirroError> {
        let inner = match config {
            GraphConnectionConfig::Neo4j {
                host,
                user,
                password,
                db_name,
            } => {
                info!("Connecting to Neo4j at {} with user {}", host, user);
                let config = ConfigBuilder::default()
                    .uri(host)
                    .user(user)
                    .password(password)
                    .db(db_name.unwrap_or_else(|| "neo4j".to_string()))
                    .build()
                    .map_err(database_error)?;
                Backend::Neo4j(Graph::connect(config).await.map_err(database_error)?)
            }
            #[cfg(feature = "graphqlite")]
            GraphConnectionConfig::Graphqlite {
                path,
                extension_path,
            } => {
                info!("Opening GraphQLite database at {}", path.display());
                let graph = tokio::task::spawn_blocking(move || {
                    let bundle = if extension_path.is_none() {
                        Some(super::graphqlite_bundle::BundledExtension::extract()?)
                    } else {
                        None
                    };
                    let library = extension_path
                        .as_deref()
                        .or_else(|| bundle.as_ref().map(|bundle| bundle.path()))
                        .ok_or_else(|| database_error("GraphQLite extension path is missing"))?;
                    let connection = graphqlite::Connection::open_with_extension(&path, library)
                        .map_err(database_error)?;
                    connection
                        .sqlite_connection()
                        .busy_timeout(std::time::Duration::from_secs(30))
                        .map_err(database_error)?;
                    let capabilities = connection.capabilities().map_err(database_error)?;
                    validate_capabilities(&capabilities)?;
                    Ok::<_, CirroError>(EmbeddedGraph {
                        connection,
                        _bundle: bundle,
                    })
                })
                .await
                .map_err(database_error)??;
                Backend::Graphqlite(Arc::new(Mutex::new(graph)))
            }
        };
        let backend = Self { inner };
        if backend
            .query_count("RETURN 1 AS connected", Map::new(), "connected")
            .await?
            != 1
        {
            return Err(database_error(
                "Database connection check returned an unexpected value",
            ));
        }
        info!("Successfully connected to the database");
        Ok(backend)
    }

    pub async fn run(&self, cypher: &str, params: Map<String, Value>) -> Result<(), CirroError> {
        match &self.inner {
            Backend::Neo4j(graph) => graph
                .run(neo4j_query(cypher, params)?)
                .await
                .map_err(database_error),
            #[cfg(feature = "graphqlite")]
            Backend::Graphqlite(_) => {
                let cypher = cypher.to_owned();
                self.embedded(move |connection| {
                    connection
                        .cypher_builder(&cypher)
                        .params(&Value::Object(params))
                        .run()
                        .map_err(database_error)?;
                    Ok(())
                })
                .await
            }
        }
    }

    pub async fn query_rows(
        &self,
        cypher: &str,
        params: Map<String, Value>,
    ) -> Result<Vec<Map<String, Value>>, CirroError> {
        match &self.inner {
            Backend::Neo4j(graph) => {
                let mut stream = graph
                    .execute(neo4j_query(cypher, params)?)
                    .await
                    .map_err(database_error)?;
                let mut rows = Vec::new();
                while let Some(row) = stream.next().await.map_err(database_error)? {
                    rows.push(row.to_strict().map_err(database_error)?);
                }
                Ok(rows)
            }
            #[cfg(feature = "graphqlite")]
            Backend::Graphqlite(_) => {
                let cypher = cypher.to_owned();
                self.embedded(move |connection| {
                    let result = connection
                        .cypher_builder(&cypher)
                        .params(&Value::Object(params))
                        .run()
                        .map_err(database_error)?;
                    result
                        .iter()
                        .map(|row| {
                            row.columns()
                                .iter()
                                .map(|column| {
                                    let value = row.get_value(column).ok_or_else(|| {
                                        database_error(format!("Missing result column {column}"))
                                    })?;
                                    Ok((column.clone(), serde_json::to_value(value)?))
                                })
                                .collect()
                        })
                        .collect()
                })
                .await
            }
        }
    }

    pub async fn query_count(
        &self,
        cypher: &str,
        params: Map<String, Value>,
        column: &str,
    ) -> Result<i64, CirroError> {
        let rows = self.query_rows(cypher, params).await?;
        if rows.len() != 1 {
            return Err(database_error(format!(
                "Expected one count row, received {}",
                rows.len()
            )));
        }
        rows[0]
            .get(column)
            .and_then(Value::as_i64)
            .ok_or_else(|| database_error(format!("Expected integer result column {column}")))
    }

    /// All operations have completed before this barrier returns.
    pub async fn finalize(&self) -> Result<(), CirroError> {
        match &self.inner {
            Backend::Neo4j(graph) => {
                let mut transaction = graph.start_txn().await.map_err(database_error)?;
                transaction
                    .run(query("RETURN 1"))
                    .await
                    .map_err(database_error)?;
                transaction.commit().await.map_err(database_error)
            }
            #[cfg(feature = "graphqlite")]
            Backend::Graphqlite(_) => {
                self.embedded(|connection| {
                    if !connection.sqlite_connection().is_autocommit() {
                        return Err(database_error("GraphQLite has an unfinished transaction"));
                    }
                    Ok(())
                })
                .await
            }
        }
    }

    pub async fn nodes(&self) -> Result<Vec<GraphNode>, CirroError> {
        match &self.inner {
            Backend::Neo4j(graph) => {
                let mut stream = graph
                    .execute(query("MATCH (n) RETURN n"))
                    .await
                    .map_err(database_error)?;
                let mut nodes = Vec::new();
                while let Some(row) = stream.next().await.map_err(database_error)? {
                    let node: Node = row.get("n").map_err(database_error)?;
                    let properties = node
                        .keys()
                        .into_iter()
                        .map(|key| {
                            node.get::<Value>(key)
                                .map(|value| (key.to_owned(), value))
                                .map_err(database_error)
                        })
                        .collect::<Result<_, _>>()?;
                    nodes.push(GraphNode {
                        id: node.id(),
                        labels: node.labels().into_iter().map(str::to_owned).collect(),
                        properties,
                    });
                }
                Ok(nodes)
            }
            #[cfg(feature = "graphqlite")]
            Backend::Graphqlite(_) => self.entities("MATCH (n) RETURN n", "n").await,
        }
    }

    pub async fn relationships(&self) -> Result<Vec<GraphRelationship>, CirroError> {
        match &self.inner {
            Backend::Neo4j(graph) => {
                let mut stream = graph
                    .execute(query("MATCH ()-[r]->() RETURN r"))
                    .await
                    .map_err(database_error)?;
                let mut relationships = Vec::new();
                while let Some(row) = stream.next().await.map_err(database_error)? {
                    let relation: Relation = row.get("r").map_err(database_error)?;
                    let properties = relation
                        .keys()
                        .into_iter()
                        .map(|key| {
                            relation
                                .get::<Value>(key)
                                .map(|value| (key.to_owned(), value))
                                .map_err(database_error)
                        })
                        .collect::<Result<_, _>>()?;
                    relationships.push(GraphRelationship {
                        id: relation.id(),
                        start_node_id: relation.start_node_id(),
                        end_node_id: relation.end_node_id(),
                        kind: relation.typ().to_owned(),
                        properties,
                    });
                }
                Ok(relationships)
            }
            #[cfg(feature = "graphqlite")]
            Backend::Graphqlite(_) => self.entities("MATCH ()-[r]->() RETURN r", "r").await,
        }
    }

    pub async fn label_connections(&self) -> Result<Vec<LabelConnection>, CirroError> {
        self.query_rows("MATCH (n)-[r]->(m) RETURN DISTINCT labels(n) AS sourceLabels, type(r) AS relType, labels(m) AS targetLabels", Map::new())
            .await?.into_iter().map(|row| serde_json::from_value(Value::Object(row)).map_err(CirroError::from)).collect()
    }

    #[cfg(feature = "graphqlite")]
    async fn entities<T: serde::de::DeserializeOwned>(
        &self,
        cypher: &str,
        column: &str,
    ) -> Result<Vec<T>, CirroError> {
        self.query_rows(cypher, Map::new())
            .await?
            .into_iter()
            .map(|mut row| {
                let value = row
                    .remove(column)
                    .ok_or_else(|| database_error(format!("Missing entity column {column}")))?;
                serde_json::from_value(value).map_err(CirroError::from)
            })
            .collect()
    }

    #[cfg(feature = "graphqlite")]
    async fn embedded<T: Send + 'static>(
        &self,
        operation: impl FnOnce(&graphqlite::Connection) -> Result<T, CirroError> + Send + 'static,
    ) -> Result<T, CirroError> {
        let Backend::Graphqlite(graph) = &self.inner else {
            return Err(database_error("Expected a GraphQLite connection"));
        };
        let graph = Arc::clone(graph);
        tokio::task::spawn_blocking(move || {
            let graph = graph
                .lock()
                .map_err(|_| database_error("GraphQLite connection lock is poisoned"))?;
            operation(&graph.connection)
        })
        .await
        .map_err(database_error)?
    }
}

#[cfg(feature = "graphqlite")]
fn validate_capabilities(capabilities: &graphqlite::Capabilities) -> Result<(), CirroError> {
    let required = [
        "call_subquery_scoped",
        "call_import_projections",
        "conditional_when",
        "dynamic_property_set",
        "set_typed_expression_values",
        "string_case_aliases",
        "apoc_convert_tojson",
        "apoc_text_replace_posix_regex",
        "apoc_text_indexof",
        "apoc_refactor_mergenodes_subset",
        "apoc_refactor_mergerelationships_subset",
        "apoc_merge_relationship",
        "apoc_periodic_iterate_sequential_atomic",
    ];
    let missing: Vec<_> = required
        .into_iter()
        .filter(|flag| !capabilities.supports(flag))
        .collect();
    if !capabilities.json1 || !missing.is_empty() {
        return Err(database_error(format!(
            "GraphQLite extension lacks required Cirro capabilities (JSON support: {}; missing: {}). Use Cirro's bundled extension or a compatible build of the GraphQLite fork.",
            capabilities.json1,
            missing.join(", ")
        )));
    }
    Ok(())
}

#[cfg(all(test, feature = "graphqlite"))]
mod tests {
    use super::*;
    use serde_json::json;

    struct TestDatabase(PathBuf);

    impl Drop for TestDatabase {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }

    #[tokio::test]
    async fn embedded_queries_preserve_types_persistence_and_errors() {
        let database = TestDatabase(
            std::env::temp_dir().join(format!("cirro-backend-{}.db", uuid::Uuid::new_v4())),
        );
        let config = || GraphConnectionConfig::Graphqlite {
            path: database.0.clone(),
            extension_path: None,
        };
        let backend = GraphBackend::connect(config()).await.unwrap();
        backend
            .run(
                "CREATE CONSTRAINT IF NOT EXISTS FOR (n:BackendTest) REQUIRE n.id IS UNIQUE",
                Map::new(),
            )
            .await
            .unwrap();
        backend.run("CREATE INDEX BackendTest_pair IF NOT EXISTS FOR (n:BackendTest) ON (n.id, n.enabled)", Map::new()).await.unwrap();
        let parameters = json!({"rows": [
            {"id": "first", "enabled": true, "values": [1, 2], "raw": "[1,2]"},
            {"id": "second", "enabled": false, "values": [], "raw": "true"}
        ]})
        .as_object()
        .unwrap()
        .clone();
        backend
            .run(
                "UNWIND $rows AS row MERGE (n:BackendTest {id: row.id}) SET n += row",
                parameters,
            )
            .await
            .unwrap();
        backend.finalize().await.unwrap();
        let rows = backend.query_rows("MATCH (n:BackendTest) RETURN n.id AS id, n.enabled AS enabled, n.values AS values, n.raw AS raw ORDER BY id", Map::new()).await.unwrap();
        assert_eq!(
            rows,
            vec![
                json!({"id":"first", "enabled":true, "values":[1,2], "raw":"[1,2]"})
                    .as_object()
                    .unwrap()
                    .clone(),
                json!({"id":"second", "enabled":false, "values":[], "raw":"true"})
                    .as_object()
                    .unwrap()
                    .clone(),
            ]
        );
        assert!(backend.run("THIS IS NOT CYPHER", Map::new()).await.is_err());
        assert!(
            backend
                .query_count("RETURN \"2\" AS count", Map::new(), "count")
                .await
                .is_err()
        );
        drop(backend);
        let reopened = GraphBackend::connect(config()).await.unwrap();
        assert_eq!(
            reopened
                .query_count(
                    "MATCH (n:BackendTest) RETURN count(n) AS count",
                    Map::new(),
                    "count"
                )
                .await
                .unwrap(),
            2
        );
    }

    #[test]
    fn older_extension_capabilities_are_rejected() {
        let capabilities = graphqlite::Capabilities {
            schema_version: 1,
            graphqlite_version: "0.12.0".to_owned(),
            cypher_dialect: "openCypher 9".to_owned(),
            sqlite_version: "3.50.2".to_owned(),
            json1: true,
            neo4j_compat: false,
            features: Default::default(),
        };
        let error = validate_capabilities(&capabilities)
            .unwrap_err()
            .to_string();
        assert!(error.contains("call_subquery_scoped"));
        assert!(error.contains("bundled extension"));
    }
}
