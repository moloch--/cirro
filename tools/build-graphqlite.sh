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
REV=b59a6dab054cb75a71d500a7a243f3fd399c8eec
BUILD_ROOT=${CIRRO_GRAPHQLITE_BUILD_DIR:-"$ROOT/target/graphqlite"}
SOURCE="$BUILD_ROOT/source"
mkdir -p "$BUILD_ROOT"
if [[ ! -d "$SOURCE/.git" ]]; then
    git clone --no-checkout https://github.com/moloch--/graphqlite.git "$SOURCE"
elif [[ -n "$(git -C "$SOURCE" status --porcelain)" ]]; then
    echo "Refusing to overwrite changes in $SOURCE" >&2
    exit 1
fi
git -C "$SOURCE" fetch --depth 1 origin "$REV"
git -C "$SOURCE" checkout --detach "$REV"

if [[ "$(uname -s)" == Darwin ]] && command -v brew >/dev/null; then
    export PATH="$(brew --prefix bison)/bin:$PATH"
fi
if ! command -v angreal >/dev/null; then
    if [[ ! -x "$BUILD_ROOT/venv/bin/angreal" ]]; then
        "${PYTHON:-python3}" -m venv "$BUILD_ROOT/venv"
        "$BUILD_ROOT/venv/bin/python" -m pip install angreal==2.9.2
    fi
    export PATH="$BUILD_ROOT/venv/bin:$PATH"
fi
(cd "$SOURCE" && angreal dev clean && angreal build extension --release)
case "$(uname -s)" in
    Darwin) EXT=dylib ;;
    Linux) EXT=so ;;
    *) echo 'Build the native extension for your target and set CIRRO_GRAPHQLITE_EXTENSION.' >&2; exit 1 ;;
esac
export CIRRO_GRAPHQLITE_EXTENSION="$SOURCE/build/graphqlite.$EXT"
cargo build --manifest-path "$ROOT/Cargo.toml" --release --features graphqlite "$@"
