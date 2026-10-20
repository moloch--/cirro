use crate::errors::CirroError;
use crate::graph::backend::GraphBackend;
use crate::graph::export::colors::{
    darken, expand_to_k_hops, generate_palette, greedy_color, text_color_for_bg,
};
use crate::graph::export::types::ExportFormat;
use log::info;
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

/// Exporter used by all Cirro export processes.
#[derive(Debug)]
pub struct CirroExporter {
    pub r#type: ExportFormat,
    pub output: PathBuf,
    pub graph: GraphBackend,
}

impl CirroExporter {
    pub fn new(r#type: ExportFormat, output: PathBuf, graph: GraphBackend) -> Self {
        Self {
            r#type,
            output,
            graph,
        }
    }

    pub async fn run(&mut self) -> Result<(), CirroError> {
        match self.r#type {
            ExportFormat::Opengraph => {
                self.export_opengraph().await?;
            }
            ExportFormat::Grass => {
                self.export_grass().await?;
            }
        }

        Ok(())
    }

    pub async fn export_opengraph(&self) -> Result<(), CirroError> {
        info!("Starting export in BloodHound OpenGraph format...");

        let nodes: Vec<serde_json::Value> = self
            .graph
            .nodes()
            .await?
            .into_iter()
            .map(|node| {
                let mut properties = node.properties;
                if !properties.contains_key("name") {
                    let fallback = properties.get("displayName").cloned().unwrap_or_else(|| {
                        serde_json::Value::String(
                            properties
                                .get("id")
                                .and_then(serde_json::Value::as_str)
                                .unwrap_or("")
                                .to_owned(),
                        )
                    });
                    properties.insert("name".to_owned(), fallback);
                }
                serde_json::json!({
                    "id": node.id.to_string(),
                    "kinds": node.labels,
                    "properties": properties,
                })
            })
            .collect();

        let edges: Vec<serde_json::Value> = self
            .graph
            .relationships()
            .await?
            .into_iter()
            .map(|relation| {
                serde_json::json!({
                    "kind": relation.kind.replace('-', "_"),
                    "start": {"value": relation.start_node_id.to_string(), "match_by": "id"},
                    "end": {"value": relation.end_node_id.to_string(), "match_by": "id"},
                    "properties": relation.properties,
                })
            })
            .collect();

        let node_count = nodes.len();
        let edge_count = edges.len();
        let final_json = serde_json::json!({"graph": {"nodes": nodes, "edges": edges}});
        let output_str = serde_json::to_string_pretty(&final_json).map_err(|e| {
            CirroError::SerializationError(format!("Failed to serialize OpenGraph JSON: {}", e))
        })?;
        tokio::fs::write(&self.output, output_str)
            .await
            .map_err(|e| {
                CirroError::ProcessingError(format!(
                    "Failed to write OpenGraph JSON to file {}: {}",
                    self.output.display(),
                    e
                ))
            })?;
        info!(
            "Successfully exported {} nodes and {} edges to {}",
            node_count,
            edge_count,
            self.output.display()
        );
        Ok(())
    }

    pub async fn export_grass(&self) -> Result<(), CirroError> {
        info!("Starting export in Neo4j GraSS stylesheet format...");

        let connections = self.graph.label_connections().await?;

        // Build adjacency map: label -> set of directly connected labels.
        let mut all_labels: HashSet<String> = HashSet::new();
        let mut adj: HashMap<String, HashSet<String>> = HashMap::new();
        for connection in connections {
            let source_labels = connection.source_labels;
            let target_labels = connection.target_labels;

            for src in &source_labels {
                all_labels.insert(src.clone());
                for tgt in &target_labels {
                    all_labels.insert(tgt.clone());
                    if src != tgt {
                        adj.entry(src.clone()).or_default().insert(tgt.clone());
                        adj.entry(tgt.clone()).or_default().insert(src.clone());
                    }
                }
            }
        }

        if all_labels.is_empty() {
            info!("No labels found in database, writing empty stylesheet");
            let output_path = self.output.with_extension("grass");
            tokio::fs::write(&output_path, "").await.map_err(|e| {
                CirroError::ProcessingError(format!(
                    "Failed to write GraSS file to {}: {}",
                    output_path.display(),
                    e
                ))
            })?;
            return Ok(());
        }

        let labels: Vec<String> = all_labels.into_iter().collect();

        // Expand adjacency to 3 hops
        let expanded_adj = expand_to_k_hops(&labels, &adj, 3);

        // Greedy graph coloring
        let coloring = greedy_color(&labels, &expanded_adj);

        // Generate palette
        let num_colors = coloring.values().max().map_or(0, |m| m + 1);
        let palette = generate_palette(num_colors);

        // Base labels that should appear last in GraSS output
        let base_labels: HashSet<&str> = ["GraphObject", "ArmResource"].into_iter().collect();

        // Sort labels: specific labels first (alphabetical), base labels last
        let mut sorted_labels: Vec<&String> = labels.iter().collect();
        sorted_labels.sort_by(|a, b| {
            let a_is_base = base_labels.contains(a.as_str());
            let b_is_base = base_labels.contains(b.as_str());
            match (a_is_base, b_is_base) {
                (false, true) => std::cmp::Ordering::Less,
                (true, false) => std::cmp::Ordering::Greater,
                _ => a.cmp(b),
            }
        });

        // Build GraSS stylesheet
        let mut grass = String::new();
        for label in &sorted_labels {
            let color_idx = coloring[label.as_str()];
            let color = &palette[color_idx];
            let border = darken(color, 0.8);
            let text = text_color_for_bg(color);
            let caption = if label.starts_with("Graph") {
                "{displayName}"
            } else {
                "{name}"
            };
            grass.push_str(&format!(
                "node.{} {{\n  color: {};\n  border-color: {};\n  text-color-internal: {};\n  caption: '{}';\n}}\n",
                label, color, border, text, caption
            ));
        }

        let output_path = self.output.with_extension("grass");
        tokio::fs::write(&output_path, &grass).await.map_err(|e| {
            CirroError::ProcessingError(format!(
                "Failed to write GraSS file to {}: {}",
                output_path.display(),
                e
            ))
        })?;

        info!(
            "Successfully exported GraSS stylesheet with {} labels to {}",
            sorted_labels.len(),
            output_path.display()
        );

        Ok(())
    }
}

#[cfg(all(test, feature = "graphqlite"))]
mod tests {
    use super::*;
    use crate::graph::backend::GraphConnectionConfig;
    use serde_json::{Map, Value};

    struct TestOutput(PathBuf);

    impl Drop for TestOutput {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[tokio::test]
    async fn embedded_exports_preserve_entities_properties_and_styles() {
        let backend = GraphBackend::connect(GraphConnectionConfig::Graphqlite {
            path: ":memory:".into(),
            extension_path: None,
        })
        .await
        .unwrap();
        backend.run("CREATE (a:GraphUser:GraphObject {id: 'user-1', displayName: 'Alice', enabled: true, values: [1,2], raw: '[1,2]'})-[:`CAN-ACCESS` {enabled: false, raw: 'true'}]->(b:ArmVm:ArmResource {id: 'vm-1', name: 'VM'})", Map::new()).await.unwrap();
        let directory =
            TestOutput(std::env::temp_dir().join(format!("cirro-export-{}", uuid::Uuid::new_v4())));
        std::fs::create_dir(&directory.0).unwrap();
        let output = directory.0.join("graph.json");
        let exporter = CirroExporter::new(ExportFormat::Opengraph, output.clone(), backend);
        exporter.export_opengraph().await.unwrap();
        exporter.export_grass().await.unwrap();

        let document: Value = serde_json::from_slice(&std::fs::read(output).unwrap()).unwrap();
        let nodes = document["graph"]["nodes"].as_array().unwrap();
        let edges = document["graph"]["edges"].as_array().unwrap();
        assert_eq!((nodes.len(), edges.len()), (2, 1));
        let user = nodes
            .iter()
            .find(|node| node["properties"]["id"] == "user-1")
            .unwrap();
        let vm = nodes
            .iter()
            .find(|node| node["properties"]["id"] == "vm-1")
            .unwrap();
        assert_eq!(user["properties"]["name"], "Alice");
        assert_eq!(user["properties"]["enabled"], true);
        assert_eq!(user["properties"]["values"], serde_json::json!([1, 2]));
        assert_eq!(user["properties"]["raw"], "[1,2]");
        assert_eq!(vm["properties"]["name"], "VM");
        assert_eq!(edges[0]["kind"], "CAN_ACCESS");
        assert_eq!(edges[0]["start"]["value"], user["id"]);
        assert_eq!(edges[0]["end"]["value"], vm["id"]);
        assert_eq!(edges[0]["properties"]["enabled"], false);
        assert_eq!(edges[0]["properties"]["raw"], "true");
        let grass = std::fs::read_to_string(directory.0.join("graph.grass")).unwrap();
        for label in ["GraphUser", "GraphObject", "ArmVm", "ArmResource"] {
            assert!(grass.contains(&format!("node.{label} {{")));
        }
        assert!(grass.contains("caption: '{displayName}'"));
        assert!(grass.contains("caption: '{name}'"));
    }
}
