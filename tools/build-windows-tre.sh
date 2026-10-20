#!/usr/bin/env bash
# Build the same BSD-licensed TRE release/Windows patches as MSYS2, without NLS.
# This keeps GraphQLite's Windows regex support without embedding gettext/iconv.
set -euo pipefail
export LC_ALL=C

[[ $# == 1 ]] || { echo "Usage: $0 PREFIX" >&2; exit 1; }
PREFIX=$1
TRE_REV=d0e0c997336b3210f05b3e1daa7bb5cb9900d274
PATCH_REV=8851937e888f240e8d71976c27e548e456c20ede
TRE_SHA=31d5776d8c56186704ec272b97fadf52a0a7ae9caca19487a0c3b17ef1626340
PATCH1_SHA=0fca8835867ccd669aa572d3fe25db8e45f33d93c17ee9c00e50cfab23e78dd1
PATCH2_SHA=72c87b956eade17ed5444c3c6eae7be09b93d145573f20017ec590d9c2516c52
COMPILER=${CC:-gcc}
for tool in "$COMPILER" curl tar patch autoreconf make; do
    command -v "$tool" >/dev/null || { echo "Missing TRE build tool: $tool" >&2; exit 1; }
done
TARGET=$("$COMPILER" -dumpmachine)
[[ "$TARGET" == x86_64-w64-mingw32 ]] ||
    { echo "TRE build requires the 64-bit MinGW compiler" >&2; exit 1; }
mkdir -p "$PREFIX"
PREFIX=$(cd "$PREFIX" && pwd)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/cirro-tre.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

check_hash() {
    local actual
    if command -v sha256sum >/dev/null; then
        actual=$(sha256sum "$1" | awk '{print $1}')
    else
        actual=$(shasum -a 256 "$1" | awk '{print $1}')
    fi
    [[ "$actual" == "$2" ]] || { echo "Source checksum mismatch: $1" >&2; exit 1; }
}

curl --fail --location --silent --show-error --retry 3 \
    "https://codeload.github.com/laurikari/tre/tar.gz/$TRE_REV" -o "$WORK/tre.tar.gz"
curl --fail --location --silent --show-error --retry 3 \
    "https://raw.githubusercontent.com/msys2/MINGW-packages/$PATCH_REV/mingw-w64-libtre/001-autotools.patch" \
    -o "$WORK/001-autotools.patch"
curl --fail --location --silent --show-error --retry 3 \
    "https://raw.githubusercontent.com/msys2/MINGW-packages/$PATCH_REV/mingw-w64-libtre/002-pointer-cast.patch" \
    -o "$WORK/002-pointer-cast.patch"
check_hash "$WORK/tre.tar.gz" "$TRE_SHA"
check_hash "$WORK/001-autotools.patch" "$PATCH1_SHA"
check_hash "$WORK/002-pointer-cast.patch" "$PATCH2_SHA"
mkdir "$WORK/source"
tar -xzf "$WORK/tre.tar.gz" --strip-components=1 -C "$WORK/source"
(
    cd "$WORK/source"
    patch -p1 < "$WORK/001-autotools.patch"
    patch -p1 < "$WORK/002-pointer-cast.patch"
    [[ ! -f ChangeLog.old ]] || mv ChangeLog.old ChangeLog
    autoreconf -fiv
    ./configure --host="$TARGET" --prefix="$PREFIX" \
        --disable-nls --disable-shared --enable-static --disable-agrep
    make
    make install
)
[[ -s "$PREFIX/lib/libtre.a" ]] || { echo "TRE static archive is missing" >&2; exit 1; }
NM=${NM:-nm}
UNDEFINED=$("$NM" -u "$PREFIX/lib/libtre.a")
if grep -Ei 'libintl_|libiconv|gettext|dgettext' <<< "$UNDEFINED"; then
    echo "TRE still references a localization runtime" >&2
    exit 1
fi
mkdir -p "$PREFIX/share/licenses/libtre"
cp "$WORK/source/LICENSE" "$PREFIX/share/licenses/libtre/LICENSE"
cat > "$PREFIX/BUILD-SOURCE.txt" <<EOF
TRE 0.9.0, commit $TRE_REV
Source archive SHA256: $TRE_SHA
MSYS2 patches commit: $PATCH_REV
001-autotools.patch SHA256: $PATCH1_SHA
002-pointer-cast.patch SHA256: $PATCH2_SHA
Configuration: --disable-nls --disable-shared --enable-static --disable-agrep
EOF
echo "Built Windows TRE without gettext/iconv: $PREFIX/lib/libtre.a"
