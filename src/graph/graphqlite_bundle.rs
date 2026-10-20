//! Keep the bundled native extension in a private directory for the connection's lifetime.
use crate::errors::CirroError;
use std::{
    fs,
    path::{Path, PathBuf},
};

const LIBRARY: &[u8] = include_bytes!(env!("CIRRO_GRAPHQLITE_LIBRARY"));

pub struct BundledExtension {
    path: PathBuf,
    _directory: tempfile::TempDir,
}

impl BundledExtension {
    pub fn extract() -> Result<Self, CirroError> {
        let directory = tempfile::Builder::new()
            .prefix("cirro-graphqlite-")
            .tempdir()?;
        let name = if cfg!(target_os = "macos") {
            "graphqlite.dylib"
        } else if cfg!(target_os = "windows") {
            "graphqlite.dll"
        } else {
            "graphqlite.so"
        };
        let path = directory.path().join(name);
        fs::write(&path, LIBRARY)?;
        fs::write(
            directory.path().join("LICENSE"),
            include_bytes!("../../third_party/graphqlite-LICENSE"),
        )?;
        Ok(Self {
            path,
            _directory: directory,
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}
