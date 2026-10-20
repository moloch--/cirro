#!/usr/bin/env bash
# Build the pinned native fork, then embed it in Cirro's optional backend.
set -euo pipefail
for argument in "$@"; do
    case "$argument" in
        --target|--target=*)
            echo 'This helper builds for the host. For cross-compilation, build a target extension and set CIRRO_GRAPHQLITE_EXTENSION when invoking Cargo directly.' >&2
            exit 1
            ;;
    esac
done
if [[ -n "${CARGO_BUILD_TARGET:-}" ]]; then
    echo 'Unset CARGO_BUILD_TARGET for this native build helper.' >&2
    exit 1
fi
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BUILD_ROOT=${CIRRO_GRAPHQLITE_BUILD_DIR:-"$ROOT/target/graphqlite"}
TARGET=$(rustc -vV | sed -n 's/^host: //p')
case "$(uname -s)" in
    Darwin) EXT=dylib ;;
    Linux) EXT=so ;;
    MINGW*|MSYS*) EXT=dll ;;
    *) echo 'Unsupported native build host.' >&2; exit 1 ;;
esac
export CIRRO_GRAPHQLITE_EXTENSION="$BUILD_ROOT/source/build/graphqlite.$EXT"
"$ROOT/tools/build-graphqlite-extension.sh" \
    --target "$TARGET" --output "$CIRRO_GRAPHQLITE_EXTENSION"
if [[ "$TARGET" == *-windows-msvc ]]; then
    CIRRO_GRAPHQLITE_EXTENSION=$(cygpath -m "$CIRRO_GRAPHQLITE_EXTENSION")
fi
if [[ "$TARGET" == *-linux-musl ]]; then
    # SQLite needs the dynamic loader to open the bundled extension.
    export RUSTFLAGS="${RUSTFLAGS:-} -C target-feature=-crt-static"
fi
cargo build --manifest-path "$ROOT/Cargo.toml" --release --features graphqlite "$@"
