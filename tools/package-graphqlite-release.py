#!/usr/bin/env python3
"""Package a GraphQLite-enabled Cirro binary, its licenses and build provenance."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--extension", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--notices-dir", type=Path)
    args = parser.parse_args()
    config = tomllib.loads((ROOT / "dist-workspace.toml").read_text())
    if args.target not in config["dist"]["targets"]:
        parser.error("target is not in dist-workspace.toml")
    cargo = tomllib.loads((ROOT / "Cargo.toml").read_text())
    binary = args.binary.read_bytes()
    extension = args.extension.read_bytes()
    if not extension or extension not in binary:
        parser.error("binary does not contain the supplied native extension")
    windows = args.target.endswith("windows-msvc")
    basename = f"cirro-graphqlite-{args.target}"
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / (basename + (".zip" if windows else ".tar.xz"))
    if archive.exists():
        parser.error(f"refusing to replace {archive}")
    with tempfile.TemporaryDirectory(prefix="cirro-package-") as temporary:
        package = Path(temporary) / basename
        package.mkdir()
        executable = package / ("cirro.exe" if windows else "cirro")
        shutil.copy2(args.binary, executable)
        executable.chmod(0o755)
        shutil.copy2(ROOT / "LICENSE", package / "LICENSE")
        shutil.copy2(ROOT / "third_party/graphqlite-LICENSE", package / "graphqlite-LICENSE")
        if args.notices_dir:
            shutil.copytree(args.notices_dir, package / "third-party-licenses")
        metadata = {
            "version": cargo["package"]["version"],
            "cirro_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "graphqlite_revision": cargo["dependencies"]["graphqlite"]["rev"],
            "target": args.target,
            "features": cargo["features"]["default"] + ["graphqlite"],
            "binary_sha256": sha256(binary),
            "embedded_extension_sha256": sha256(extension),
        }
        (package / "build-info.json").write_text(json.dumps(metadata, indent=2) + "\n")
        runtime = (
            "This Alpine/musl build uses the system musl loader "
            "(/lib/ld-musl-x86_64.so.1); it is not a static executable.\n"
            if args.target.endswith("-musl") else ""
        )
        (package / "README.md").write_text(
            f"# Cirro {cargo['package']['version']} with GraphQLite\n\n"
            f"Target: `{args.target}`\n\n"
            "Copy `cirro` (or `cirro.exe`) to your PATH. The native GraphQLite\n"
            "extension is embedded in the executable and automatically extracted\n"
            "to a private temporary directory. No extension installation, Python,\n"
            "SQLite CLI, or database server is required for local graphs.\n"
            "A writable temporary directory that permits loading shared libraries is required.\n\n"
            f"{runtime}\n"
            "Azure and Tailscale collection and the default Neo4j backend remain available.\n"
            "Use `--backend graphqlite --graph-file graph.db` for local graph commands.\n\n"
            "For a quick query check, create an empty `graph.db` first (`touch graph.db`\n"
            "on Unix, or `New-Item graph.db` in PowerShell), then run:\n\n"
            "```sh\n"
            "cirro graph query --backend graphqlite --graph-file graph.db "
            "--cypher 'RETURN 1 AS ready'\n"
            "```\n\n"
            "Full usage: https://github.com/moloch--/cirro/blob/main/docs/usage/graphqlite.md\n"
        )
        if windows:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(package.rglob("*")):
                    if path.is_file():
                        bundle.write(path, f"{basename}/{path.relative_to(package).as_posix()}")
        else:
            with tarfile.open(archive, "w:xz") as bundle:
                bundle.add(package, arcname=basename)
    checksum = sha256(archive.read_bytes())
    archive.with_name(archive.name + ".sha256").write_text(f"{checksum}  {archive.name}\n")
    print(archive)


if __name__ == "__main__":
    main()
