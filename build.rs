use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo:rerun-if-env-changed=CIRRO_GRAPHQLITE_EXTENSION");
    if env::var_os("CARGO_FEATURE_GRAPHQLITE").is_none() {
        return;
    }
    let source = env::var_os("CIRRO_GRAPHQLITE_EXTENSION")
        .map(PathBuf::from)
        .expect(
            "The graphqlite feature needs its native extension. Run tools/build-graphqlite.sh, \
         or set CIRRO_GRAPHQLITE_EXTENSION to a built extension from the pinned fork.",
        );
    println!("cargo:rerun-if-changed={}", source.display());
    let name = match env::var("CARGO_CFG_TARGET_OS").as_deref() {
        Ok("macos") => "graphqlite.dylib",
        Ok("linux") => "graphqlite.so",
        Ok("windows") => "graphqlite.dll",
        other => panic!("GraphQLite extension packaging is unsupported for {other:?}"),
    };
    let output = PathBuf::from(env::var_os("OUT_DIR").expect("OUT_DIR")).join(name);
    fs::copy(&source, &output).unwrap_or_else(|e| {
        panic!(
            "Cannot embed GraphQLite extension {}: {e}",
            source.display()
        )
    });
    println!(
        "cargo:rustc-env=CIRRO_GRAPHQLITE_LIBRARY={}",
        output.display()
    );
}
