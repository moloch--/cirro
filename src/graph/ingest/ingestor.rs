use crate::errors::CirroError;
use crate::graph::backend::GraphBackend;
use crate::graph::ingest::CREATE_CONSTRAINT_QUERY;
use crate::graph::specs::{SpecLoader, SpecRegistry, SpecTrait};
use clap::ValueEnum;
use log::{debug, info};
use rusqlite::{Connection, Result};
use serde_json::Map;
use std::path::PathBuf;
use tokio::time::Duration;

#[derive(Debug, Clone, ValueEnum, PartialEq, Eq)]
pub enum IngestType {
    /// Azure data ingestion
    Az,
    /// Tailscale status data ingestion  
    TsStatus,
}
/// Ingestor used by all Cirro ingestion processes to manage database connections and ingestion specs
pub struct CirroIngestor {
    pub r#type: IngestType,
    pub file: PathBuf,
    pub labels: Option<Vec<String>>,
    pub graph: GraphBackend,
    pub sql_conn: Option<Connection>,
    pub specs: SpecRegistry,
}

/// Custom Debug trait for CirroIngestor
impl std::fmt::Debug for CirroIngestor {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CirroIngestor")
            .field("type", &self.r#type)
            .field("file", &self.file)
            .field("graph", &format_args!("<GraphBackend>"))
            .field("specs", &format_args!("<SpecRegistry>"))
            .finish()
    }
}

impl CirroIngestor {
    pub fn new(
        r#type: IngestType,
        file: PathBuf,
        labels: Option<Vec<String>>,
        graph: GraphBackend,
    ) -> Result<Self, CirroError> {
        info!("Loading ingestion specifications...");
        let specs = SpecLoader::load_all_specs_filtered(labels.clone())?;
        let sql_conn = match r#type {
            IngestType::Az => Some(Connection::open_with_flags(
                &file,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
            )?),
            IngestType::TsStatus => None,
        };

        Ok(CirroIngestor {
            r#type,
            file,
            graph,
            sql_conn,
            specs,
            labels,
        })
    }

    /// Runs the ingestor
    pub async fn run(&mut self, post_process_only: bool, dry_run: bool) -> Result<(), CirroError> {
        // Start stopwatch to measure the time taken for the entire ingestion process
        let start_time = std::time::Instant::now();
        debug!("Ingestion started at: {:?}", start_time);

        if dry_run {
            info!("Dry-run enabled: no ingestion or post-processing queries will be executed");
        }

        if !post_process_only {
            // Run the main ingestion logic
            match self.r#type {
                IngestType::Az => self.process_cirro_azure_ingest(dry_run).await?,
                IngestType::TsStatus => self.process_cirro_tailscale_status_ingest(dry_run).await?,
            }

            // Ensure all transactions are committed before post-processing
            if !dry_run {
                self.finalize_transactions().await?;
            }
        }

        if dry_run && self.r#type == IngestType::Az {
            self.show_unimplemented_azure_resource_types().await?;
        }

        self.generic_post_process(dry_run).await?;

        // Calculate the total time taken for the ingestion process
        if !dry_run {
            let duration = start_time.elapsed();
            info!("Cirro ingestion process completed in: {:.2?}", duration);
        }

        Ok(())
    }

    pub async fn create_constraints_by_name(&self, label_name: &str) -> Result<(), CirroError> {
        let constraint_query = CREATE_CONSTRAINT_QUERY
            .replacen("{}", label_name, 1)
            .replacen("{}", "id", 1);
        debug!("Executing query: {}", constraint_query);
        let _ = self
            .graph
            .run(&constraint_query, Map::new())
            .await
            .map_err(|e| {
                CirroError::DatabaseError(format!(
                    "Failed to create constraint for {}: {}",
                    label_name, e
                ))
            })?;
        Ok(())
    }

    pub async fn create_constraints_and_indexes_by_spec<T>(
        &self,
        spec: &T,
    ) -> Result<(), CirroError>
    where
        T: SpecTrait,
    {
        let label_name = spec.get_label();
        self.create_constraints_by_name(&label_name).await?;
        Ok(())
    }

    /// Ensures all transactions are committed before proceeding
    async fn finalize_transactions(&self) -> Result<(), CirroError> {
        info!("Finalizing all transactions...");
        self.graph.finalize().await.map_err(|e| {
            CirroError::DatabaseError(format!("Failed to finalize ingestion transactions: {}", e))
        })?;

        // Add a brief pause to ensure commit propagation
        tokio::time::sleep(Duration::from_millis(100)).await;

        Ok(())
    }

    async fn generic_post_process(&mut self, dry_run: bool) -> Result<(), CirroError> {
        if dry_run {
            info!("Dry-run: post-processing queries that would run:");
            let mut post_processing_specs = self.specs.cirro_post_processing_specs.clone();
            post_processing_specs.sort_by_key(|spec| spec.priority);
            for spec in &post_processing_specs {
                info!(
                    "[dry-run] priority {} post-process spec: {}",
                    spec.priority, spec.name
                );
                debug!("[dry-run] cypher: {}", spec.cypher);
            }
            return Ok(());
        }

        info!("Waiting for all transactions to commit...");

        // Force a transaction commit by running a simple query that requires a read
        let _ = self
            .graph
            .query_count(
                "MATCH (n) RETURN count(n) AS count LIMIT 1",
                Map::new(),
                "count",
            )
            .await
            .map_err(|e| {
                CirroError::DatabaseError(format!("Failed to execute commit check query: {}", e))
            })?;

        // Add a small delay to ensure commit propagation
        tokio::time::sleep(Duration::from_millis(500)).await;

        info!("Running post-processing queries");

        // First, order the post-processing specs by priority
        let mut post_processing_specs = self.specs.cirro_post_processing_specs.clone();
        post_processing_specs.sort_by_key(|spec| spec.priority);

        for spec in &post_processing_specs {
            debug!("Running post-processing spec: {}", spec.name);

            self.graph
                .run(&spec.cypher, Map::new())
                .await
                .map_err(|e| {
                    CirroError::DatabaseError(format!(
                        "Failed to execute post-processing query for spec {}: {}",
                        spec.name, e
                    ))
                })?;
        }

        Ok(())
    }
}
