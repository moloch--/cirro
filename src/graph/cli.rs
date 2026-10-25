use crate::errors::CirroError;
use crate::graph::backend::{GraphBackend, GraphConnectionConfig};
use crate::graph::export::exporter::CirroExporter;
use crate::graph::export::types::ExportFormat;
use crate::graph::ingest::ingestor::{CirroIngestor, IngestType};
use crate::graph::logger::{setup_logger, setup_query_logger};
use clap::{Args, Subcommand, ValueEnum};
use serde_json::{Map, Value};
use std::path::{Path, PathBuf};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, ValueEnum)]
pub enum BackendKind {
    #[default]
    Neo4j,
    #[cfg(feature = "graphqlite")]
    Graphqlite,
}

#[derive(Debug, Args)]
pub struct GraphConnectionArgs {
    /// Graph database backend
    #[arg(long, value_enum, default_value_t = BackendKind::Neo4j)]
    backend: BackendKind,
    /// Neo4j server URL
    #[arg(short, long, default_value = "bolt://localhost:7687")]
    server: String,
    /// Neo4j user
    #[arg(short, long, default_value = "neo4j")]
    user: String,
    /// Neo4j password
    #[arg(short, long, default_value = "password")]
    password: String,
    /// Neo4j database name (default: neo4j)
    #[arg(short, long, value_name = "NAME")]
    db_name: Option<String>,
    /// GraphQLite graph database, separate from the collected input file
    #[cfg(feature = "graphqlite")]
    #[arg(long, value_name = "PATH")]
    graph_file: Option<PathBuf>,
    /// Override the bundled GraphQLite native extension
    #[cfg(feature = "graphqlite")]
    #[arg(long, value_name = "PATH")]
    graphqlite_extension: Option<PathBuf>,
}

impl GraphConnectionArgs {
    async fn connect(self, create: bool, dry_run: bool) -> Result<GraphBackend, CirroError> {
        #[cfg(not(feature = "graphqlite"))]
        let _ = (create, dry_run);
        let config = match self.backend {
            BackendKind::Neo4j => {
                #[cfg(feature = "graphqlite")]
                if self.graph_file.is_some() || self.graphqlite_extension.is_some() {
                    return Err(CirroError::Config(
                        "--graph-file and --graphqlite-extension require --backend graphqlite"
                            .into(),
                    ));
                }
                if ![
                    "bolt://",
                    "bolt+s://",
                    "bolt+ssc://",
                    "neo4j://",
                    "neo4j+s://",
                    "neo4j+ssc://",
                ]
                .iter()
                .any(|prefix| self.server.starts_with(prefix))
                {
                    return Err(CirroError::Config(
                        "Neo4j server must use a bolt:// or neo4j:// scheme (including +s or +ssc)"
                            .into(),
                    ));
                }
                GraphConnectionConfig::Neo4j {
                    host: self.server,
                    user: self.user,
                    password: self.password,
                    db_name: self.db_name,
                }
            }
            #[cfg(feature = "graphqlite")]
            BackendKind::Graphqlite => {
                let path = if dry_run {
                    PathBuf::from(":memory:")
                } else {
                    let path = self.graph_file.ok_or_else(|| {
                        CirroError::Config(
                            "--graph-file is required for --backend graphqlite".into(),
                        )
                    })?;
                    if !create && !path.is_file() {
                        return Err(CirroError::Config(format!(
                            "Graph database does not exist: {}",
                            path.display()
                        )));
                    }
                    path
                };
                GraphConnectionConfig::Graphqlite {
                    path,
                    extension_path: self.graphqlite_extension,
                }
            }
        };
        GraphBackend::connect(config).await
    }

    fn check_input(&self, input: &Path) -> Result<(), CirroError> {
        if !input.exists() {
            return Ok(());
        }
        #[cfg(not(feature = "graphqlite"))]
        let _ = input;
        #[cfg(feature = "graphqlite")]
        if self.backend == BackendKind::Graphqlite {
            if let Some(output) = &self.graph_file {
                if output.exists() {
                    if same_file::is_same_file(input, output)? {
                        return Err(CirroError::Config(
                            "--graph-file must be separate from input and export files".into(),
                        ));
                    }
                }
            }
        }
        Ok(())
    }
}

#[derive(Debug, Subcommand)]
pub enum GraphCommands {
    /// Ingest collected data into the graph
    Ingest {
        #[arg(short, long, value_enum)]
        r#type: IngestType,
        #[arg(short, long, value_name = "FILE", value_hint = clap::ValueHint::FilePath)]
        file: PathBuf,
        #[command(flatten)]
        connection: GraphConnectionArgs,
        /// Ingest only these labels (comma-separated)
        #[arg(long, value_name = "LABELS", value_delimiter = ',', num_args = 1..)]
        labels: Option<Vec<String>>,
        /// Run only post-processing specs
        #[arg(long)]
        post_process: bool,
        /// Show ingestion and post-processing without changing the graph
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        debug: bool,
    },
    /// Export the graph in different formats
    Export {
        #[arg(short, long, value_name = "FORMAT")]
        format: ExportFormat,
        #[arg(short, long, default_value = "cirro_export", value_hint = clap::ValueHint::FilePath)]
        output: PathBuf,
        #[command(flatten)]
        connection: GraphConnectionArgs,
        #[arg(long)]
        debug: bool,
    },
    /// Execute Cypher and print rows as JSON
    Query {
        #[arg(short = 'q', long, visible_alias = "query")]
        cypher: String,
        /// JSON object containing Cypher parameters
        #[arg(long, default_value = "{}")]
        params: String,
        #[command(flatten)]
        connection: GraphConnectionArgs,
        #[arg(long)]
        debug: bool,
    },
}

pub async fn handle_graph_command(command: GraphCommands) -> Result<(), CirroError> {
    match command {
        GraphCommands::Ingest {
            r#type,
            file,
            connection,
            labels,
            post_process,
            dry_run,
            debug,
        } => {
            setup_logger(debug)?;
            if !file.is_file() {
                return Err(CirroError::Config(format!(
                    "Input file does not exist: {}",
                    file.display()
                )));
            }
            std::fs::File::open(&file)?;
            connection.check_input(&file)?;
            let backend = connection.connect(true, dry_run).await?;
            CirroIngestor::new(r#type, file, labels, backend)?
                .run(post_process, dry_run)
                .await?;
        }
        GraphCommands::Export {
            format,
            output,
            connection,
            debug,
        } => {
            setup_logger(debug)?;
            let export_path = match format {
                ExportFormat::Opengraph => output.clone(),
                ExportFormat::Grass => output.with_extension("grass"),
            };
            connection.check_input(&export_path)?;
            let backend = connection.connect(false, false).await?;
            CirroExporter::new(format, output, backend).run().await?;
        }
        GraphCommands::Query {
            cypher,
            params,
            connection,
            debug,
        } => {
            setup_query_logger(debug)?;
            let params: Map<String, Value> = serde_json::from_str(&params)
                .map_err(|e| CirroError::Config(format!("--params must be a JSON object: {e}")))?;
            let backend = connection.connect(false, false).await?;
            let rows = backend.query_rows(&cypher, params).await?;
            println!("{}", serde_json::to_string_pretty(&rows)?);
        }
    }
    Ok(())
}

#[cfg(all(test, feature = "graphqlite"))]
mod tests {
    use super::*;

    fn connection(graph_file: PathBuf) -> GraphConnectionArgs {
        GraphConnectionArgs {
            backend: BackendKind::Graphqlite,
            server: "bolt://localhost:7687".into(),
            user: "neo4j".into(),
            password: "password".into(),
            db_name: None,
            graph_file: Some(graph_file),
            graphqlite_extension: None,
        }
    }

    #[test]
    fn graph_file_rejects_same_path_and_hardlinks() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("source.db");
        let hardlink = directory.path().join("alias.db");
        std::fs::write(&source, b"collected data").unwrap();
        std::fs::hard_link(&source, &hardlink).unwrap();

        // Covers both ingestion (the graph aliases the input) and export
        // (the output aliases the graph), including Windows file identities.
        for (graph, input) in [
            (&source, &source),
            (&hardlink, &source),
            (&source, &hardlink),
        ] {
            let error = connection(graph.clone()).check_input(input).unwrap_err();
            assert!(error.to_string().contains("must be separate"));
        }
    }

    #[cfg(unix)]
    #[test]
    fn graph_file_rejects_symbolic_links() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("source.db");
        let symlink = directory.path().join("alias.db");
        std::fs::write(&source, b"collected data").unwrap();
        std::os::unix::fs::symlink(&source, &symlink).unwrap();

        assert!(connection(symlink.clone()).check_input(&source).is_err());
        assert!(connection(source).check_input(&symlink).is_err());
    }

    #[test]
    fn graph_file_allows_distinct_and_missing_files() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("source.db");
        let graph = directory.path().join("graph.db");
        let missing = directory.path().join("missing.db");
        std::fs::write(&source, b"same contents").unwrap();
        std::fs::write(&graph, b"same contents").unwrap();

        assert!(connection(graph.clone()).check_input(&source).is_ok());
        assert!(connection(missing.clone()).check_input(&source).is_ok());
        assert!(connection(graph).check_input(&missing).is_ok());
        assert!(!missing.exists());
    }

    #[test]
    fn neo4j_does_not_apply_local_graph_alias_checks() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("source.db");
        std::fs::write(&source, b"collected data").unwrap();
        let mut args = connection(source.clone());
        args.backend = BackendKind::Neo4j;
        assert!(args.check_input(&source).is_ok());
    }
}
