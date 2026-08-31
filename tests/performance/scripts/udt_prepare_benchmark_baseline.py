#!/usr/bin/env python3

"""Install control-only UDT benchmark fixtures into a clean upstream source tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy(source: Path, destination: Path, manifest: dict[str, str]) -> None:
    if not source.is_file():
        raise SystemExit(f"missing baseline benchmark input: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    manifest[str(destination)] = sha256(destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-source", required=True, type=Path)
    parser.add_argument("--candidate-source", required=True, type=Path)
    parser.add_argument("--overlay", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--baseline-revision", required=True)
    parser.add_argument("--baseline-tree", required=True)
    parser.add_argument("--baseline-archive-sha256", required=True)
    args = parser.parse_args()

    if not re.fullmatch(r"[0-9a-f]{40}", args.baseline_revision):
        parser.error("--baseline-revision must be a full lowercase Git object ID")
    if not re.fullmatch(r"[0-9a-f]{40}", args.baseline_tree):
        parser.error("--baseline-tree must be a full lowercase Git tree object ID")
    if not re.fullmatch(r"[0-9a-f]{64}", args.baseline_archive_sha256):
        parser.error("--baseline-archive-sha256 must be a lowercase SHA-256 digest")

    baseline = args.baseline_source.resolve()
    candidate = args.candidate_source.resolve()
    overlay = args.overlay.resolve()
    if not (baseline / "CMakeLists.txt").is_file():
        raise SystemExit(f"not a ClickHouse baseline source: {baseline}")
    if (baseline / "src/Parsers/ASTCastTarget.h").exists():
        raise SystemExit("baseline unexpectedly contains the candidate UDT parser surface")
    if not (candidate / "src/Parsers/ASTCastTarget.h").is_file():
        raise SystemExit(f"candidate UDT source is incomplete: {candidate}")

    installed_files: dict[str, str] = {}
    mappings = (
        (overlay / "DataTypes.cmake", baseline / "src/DataTypes/CMakeLists.txt"),
        (overlay / "DataTypesBenchmarks.cmake", baseline / "src/DataTypes/benchmarks/CMakeLists.txt"),
        (overlay / "CommonBenchmarks.cmake", baseline / "src/Common/benchmarks/CMakeLists.txt"),
        (
            candidate / "src/DataTypes/benchmarks/udt_parser.cpp",
            baseline / "src/DataTypes/benchmarks/udt_parser.cpp",
        ),
        (
            candidate / "src/DataTypes/benchmarks/udt_analysis.cpp",
            baseline / "src/DataTypes/benchmarks/udt_analysis.cpp",
        ),
        (
            candidate / "src/Common/benchmarks/udt_physicalization_token_masking.cpp",
            baseline / "src/Common/benchmarks/udt_physicalization_token_masking.cpp",
        ),
        (
            candidate / "src/Common/benchmarks/JemallocBenchmarkMemoryManager.h",
            baseline / "src/Common/benchmarks/JemallocBenchmarkMemoryManager.h",
        ),
    )
    for source, destination in mappings:
        copy(source, destination, installed_files)

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "baseline": {
            "revision": args.baseline_revision,
            "tree": args.baseline_tree,
            "archive_sha256": args.baseline_archive_sha256,
        },
        "installed_files": {
            str(Path(path).relative_to(baseline)): digest
            for path, digest in installed_files.items()
        },
    }
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
