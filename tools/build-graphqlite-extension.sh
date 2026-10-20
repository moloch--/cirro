#!/usr/bin/env bash
# Build only the pinned GraphQLite extension for a matching native environment.
set -euo pipefail
export LC_ALL=C

usage() {
    cat <<'EOF'
Usage: tools/build-graphqlite-extension.sh --target TRIPLE --output PATH [--env-file PATH]

Supported native targets:
  aarch64-apple-darwin
  x86_64-apple-darwin
  aarch64-unknown-linux-gnu
  x86_64-unknown-linux-gnu
  x86_64-unknown-linux-musl
  x86_64-pc-windows-msvc (build the C DLL in MSYS2 MINGW64/UCRT64)

Requires git, gcc, make, Bison, Flex, Python and angreal 2.9.2. If angreal
is absent, a private Python virtual environment is created. Linux also needs
binutils; Windows needs the MinGW SQLite, libsystre/TRE, gettext and iconv
static libraries. macOS needs the current Homebrew Bison.
Windows also needs autotools, gettext build tools, curl and patch to rebuild
TRE without localization; gettext/iconv are then excluded from the DLL.

--env-file appends CIRRO_GRAPHQLITE_EXTENSION for a GitHub Actions environment
file. Build logs go to stdout; use the explicit output path, not stdout parsing.
CIRRO_GRAPHQLITE_BUILD_DIR selects the private checkout/build directory.
The musl extension requires a dynamically linked musl executable; a fully
static musl executable cannot load it. The musl loader must exist at runtime.
EOF
}

fail() { echo "GraphQLite build: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || fail "Required tool is missing: $1"; }

TARGET=
OUTPUT=
ENV_FILE=
while (($#)); do
    case "$1" in
        --target|--output|--env-file)
            (($# >= 2)) || fail "$1 requires a value"
            case "$1" in
                --target) TARGET=$2 ;;
                --output) OUTPUT=$2 ;;
                --env-file) ENV_FILE=$2 ;;
            esac
            shift 2
            ;;
        --help|-h) usage; exit 0 ;;
        *) fail "Unknown argument: $1" ;;
    esac
done
[[ -n "$TARGET" && -n "$OUTPUT" ]] || { usage >&2; exit 1; }

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REV=b59a6dab054cb75a71d500a7a243f3fd399c8eec
BUILD_ROOT=${CIRRO_GRAPHQLITE_BUILD_DIR:-"$ROOT/target/graphqlite"}
HOST_OS=$(uname -s)
HOST_ARCH=$(uname -m)
case "$HOST_ARCH" in
    arm64|aarch64) HOST_ARCH=aarch64 ;;
    x86_64|amd64) HOST_ARCH=x86_64 ;;
    *) fail "Unsupported host architecture: $HOST_ARCH" ;;
esac

case "$TARGET" in
    aarch64-apple-darwin|x86_64-apple-darwin)
        [[ "$HOST_OS" == Darwin ]] || fail "$TARGET requires a macOS host"
        PLATFORM=macos
        EXT=dylib
        ;;
    aarch64-unknown-linux-gnu|x86_64-unknown-linux-gnu|x86_64-unknown-linux-musl)
        [[ "$HOST_OS" == Linux ]] || fail "$TARGET requires a Linux host"
        PLATFORM=linux
        EXT=so
        if getconf GNU_LIBC_VERSION >/dev/null 2>&1; then
            LIBC=gnu
        elif (ldd --version 2>&1 || true) | grep -qi musl; then
            LIBC=musl
        else
            fail "Cannot identify the host C library"
        fi
        [[ "$TARGET" == *-"$LIBC" ]] || fail "$TARGET does not match the host C library ($LIBC)"
        ;;
    x86_64-pc-windows-msvc)
        [[ "$HOST_OS" == MINGW* || "$HOST_OS" == MSYS* ]] ||
            fail "$TARGET requires an MSYS2 MINGW64 or UCRT64 shell"
        [[ "${MSYSTEM:-}" == MINGW64 || "${MSYSTEM:-}" == UCRT64 ]] ||
            fail "Select MSYS2 MINGW64 or UCRT64, not the MSYS C compiler"
        PLATFORM=windows
        EXT=dll
        ;;
    *) fail "Unsupported target: $TARGET" ;;
esac
[[ "$TARGET" == "$HOST_ARCH"-* ]] ||
    fail "Cross-compilation is unsupported: host $HOST_ARCH, target $TARGET"

if [[ "$PLATFORM" == macos ]] && command -v brew >/dev/null; then
    BISON_PREFIX=$(brew --prefix bison)
    FLEX_PREFIX=$(brew --prefix flex)
    export PATH="$BISON_PREFIX/bin:$FLEX_PREFIX/bin:$PATH"
fi
for tool in git gcc make bison flex; do need "$tool"; done
case "$PLATFORM" in
    macos) need otool; need lipo ;;
    linux) need readelf ;;
    windows)
        need objdump
        need cygpath
        [[ "$(gcc -dumpmachine)" == x86_64-w64-mingw32 ]] ||
            fail "The selected gcc must target native 64-bit Windows"
        OUTPUT=$(cygpath -u "$OUTPUT")
        BUILD_ROOT=$(cygpath -u "$BUILD_ROOT")
        [[ -z "$ENV_FILE" ]] || ENV_FILE=$(cygpath -u "$ENV_FILE")
        ;;
esac

mkdir -p "$BUILD_ROOT"
BUILD_ROOT=$(cd "$BUILD_ROOT" && pwd)
if [[ "$PLATFORM" == windows ]]; then
    need nm
    "$ROOT/tools/build-windows-tre.sh" "$BUILD_ROOT/windows-tre"
    # MinGW's native GCC uses semicolons to separate Windows library paths.
    TRE_LIBRARY_PATH=$(cygpath -m "$BUILD_ROOT/windows-tre/lib")
    export LIBRARY_PATH="$TRE_LIBRARY_PATH${LIBRARY_PATH:+;$LIBRARY_PATH}"
    RESOLVED_TRE=$(gcc -print-file-name=libtre.a)
    [[ "$(cygpath -u "$RESOLVED_TRE")" -ef "$BUILD_ROOT/windows-tre/lib/libtre.a" ]] ||
        fail "MinGW did not select the private no-NLS TRE archive: $RESOLVED_TRE"
fi
SOURCE="$BUILD_ROOT/source"
if [[ ! -d "$SOURCE/.git" ]]; then
    [[ ! -e "$SOURCE" ]] || fail "Source path exists without a Git checkout: $SOURCE"
    git clone --no-checkout https://github.com/moloch--/graphqlite.git "$SOURCE"
elif [[ -n "$(git -C "$SOURCE" status --porcelain)" ]]; then
    fail "Refusing to overwrite changes in $SOURCE"
fi
git -C "$SOURCE" fetch --depth 1 origin "$REV"
git -C "$SOURCE" checkout --detach "$REV"
[[ "$(git -C "$SOURCE" rev-parse HEAD)" == "$REV" ]] || fail "Pinned source verification failed"

if ! command -v angreal >/dev/null; then
    if [[ ! -x "$BUILD_ROOT/venv/bin/angreal" && ! -f "$BUILD_ROOT/venv/Scripts/angreal.exe" ]]; then
        "${PYTHON:-python3}" -m venv "$BUILD_ROOT/venv"
        if [[ -x "$BUILD_ROOT/venv/bin/python" ]]; then
            "$BUILD_ROOT/venv/bin/python" -m pip install angreal==2.9.2
        else
            "$BUILD_ROOT/venv/Scripts/python.exe" -m pip install angreal==2.9.2
        fi
    fi
    export PATH="$BUILD_ROOT/venv/bin:$BUILD_ROOT/venv/Scripts:$PATH"
fi
need angreal
(cd "$SOURCE" && angreal dev clean && angreal build extension --release)
BUILT="$SOURCE/build/graphqlite.$EXT"
[[ -s "$BUILT" ]] || fail "Build did not produce $BUILT"

# Fail instead of shipping a library that needs the build machine's packages.
case "$PLATFORM" in
    macos)
        EXPECTED_ARCH=$HOST_ARCH
        [[ "$EXPECTED_ARCH" != aarch64 ]] || EXPECTED_ARCH=arm64
        [[ "$(lipo -archs "$BUILT")" == "$EXPECTED_ARCH" ]] || fail "Wrong Mach-O architecture"
        # otool lists the dylib's own install name first; inspect dependencies only.
        while IFS= read -r dependency; do
            case "$dependency" in
                /usr/lib/*|/System/Library/*) ;;
                *) fail "Non-system macOS dependency: $dependency" ;;
            esac
        done < <(otool -L "$BUILT" | awk 'NR > 2 {print $1}')
        ;;
    linux)
        ELF_MACHINE=$(readelf -h "$BUILT" | awk -F: '/Machine:/ {sub(/^[ \t]+/, "", $2); print $2}')
        case "$TARGET:$ELF_MACHINE" in
            aarch64-*:AArch64|x86_64-*:"Advanced Micro Devices X86-64") ;;
            *) fail "Wrong ELF architecture: $ELF_MACHINE" ;;
        esac
        while IFS= read -r dependency; do
            case "$LIBC:$dependency" in
                gnu:libc.so.6|gnu:libm.so.6|gnu:libpthread.so.0|gnu:libdl.so.2|gnu:librt.so.1|gnu:ld-linux*.so.*) ;;
                musl:libc.musl-x86_64.so.1) ;;
                *) fail "Unexpected Linux runtime dependency: $dependency" ;;
            esac
        done < <(readelf -d "$BUILT" | sed -n 's/.*(NEEDED).*\[\(.*\)\].*/\1/p')
        if readelf -d "$BUILT" | grep -Eq '\((RPATH|RUNPATH)\)'; then
            fail "Extension contains an unexpected runtime search path"
        fi
        ;;
    windows)
        objdump -f "$BUILT" | grep 'file format pei-x86-64' >/dev/null || fail "Wrong PE architecture"
        objdump -p "$BUILT" | grep 'sqlite3_graphqlite_init' >/dev/null || fail "Missing SQLite DLL entry point"
        while IFS= read -r dependency; do
            case "$dependency" in
                kernel32.dll|msvcrt.dll|ucrtbase.dll|ntdll.dll|advapi32.dll|bcrypt.dll|crypt32.dll|user32.dll|ws2_32.dll|api-ms-win-*.dll) ;;
                *) fail "Non-system Windows DLL dependency: $dependency" ;;
            esac
        done < <(objdump -p "$BUILT" | awk '/DLL Name:/ {print tolower($3)}')
        # The original Makefile names -lintl/-liconv, but our no-NLS TRE must
        # leave those archives unused. Inspect all linked symbols, not imports.
        WINDOWS_SYMBOLS=$(nm "$BUILT")
        if grep -Ei 'libintl_|libiconv|[[:space:]]_?(gettext|dgettext|dcgettext)$' <<< "$WINDOWS_SYMBOLS"; then
            fail "Windows extension contains an unexpected gettext/iconv implementation"
        fi
        ;;
esac

mkdir -p "$(dirname "$OUTPUT")"
OUTPUT="$(cd "$(dirname "$OUTPUT")" && pwd)/$(basename "$OUTPUT")"
if [[ ! "$OUTPUT" -ef "$BUILT" ]]; then
    cp "$BUILT" "$OUTPUT"
fi
if [[ -n "$ENV_FILE" ]]; then
    ENV_OUTPUT=$OUTPUT
    [[ "$PLATFORM" != windows ]] || ENV_OUTPUT=$(cygpath -m "$OUTPUT")
    [[ "$ENV_OUTPUT" != *$'\n'* && "$ENV_OUTPUT" != *$'\r'* ]] || fail "Output path contains a newline"
    printf 'CIRRO_GRAPHQLITE_EXTENSION=%s\n' "$ENV_OUTPUT" >> "$ENV_FILE"
fi
echo "Built GraphQLite $REV for $TARGET: $OUTPUT"
