#!/usr/bin/env python3
"""Build and run the complete Release UDT benchmark campaign.

This is the in-container orchestrator.  It owns the two reproducible Release
builds and delegates measurements to the microbenchmark and server benchmark
controllers.  Host-side source staging, container startup, and polling are
intentionally outside its scope.

The orchestrator is deliberately non-fail-fast: a broken baseline build does
not prevent the independent candidate build, and a broken microbenchmark phase
does not prevent server benchmarks when the candidate server is available.
Every command and every failed attempt remains in the artifact tree.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


SCHEMA_VERSION = 1
RESUMABLE_TERMINAL_PHASE_STATES = {
    "succeeded",
    "performance_failure",
    "inconclusive",
}

PHASES = (
    "configure_baseline",
    "configure_candidate",
    "build_baseline",
    "build_candidate",
    "microbenchmarks",
    "server_benchmarks",
)

COMMON_CMAKE_DEFINITIONS = (
    ("CMAKE_BUILD_TYPE", "Release"),
    ("ENABLE_THINLTO", "OFF"),
    ("BUILD_STRIPPED_BINARY", "OFF"),
    ("ENABLE_BENCHMARKS", "ON"),
    ("ENABLE_TESTS", "OFF"),
    ("ENABLE_UTILS", "OFF"),
    ("ENABLE_LIBURING", "OFF"),
    ("ENABLE_BUILD_PROFILING", "OFF"),
    ("ENABLE_CHECK_HEAVY_BUILDS", "OFF"),
    ("SANITIZE", "OFF"),
    ("CMAKE_C_COMPILER", "clang-21"),
    ("CMAKE_CXX_COMPILER", "clang++-21"),
    ("COMPILER_CACHE", "sccache"),
)

BASELINE_TARGETS = (
    "programs/clickhouse",
    "benchmark_udt_parser",
    "benchmark_udt_analysis",
    "benchmark_udt_physicalization_token_masking",
)

CANDIDATE_TARGETS = (
    "programs/clickhouse",
    "benchmark_udt_catalog_resolver",
    "benchmark_udt_parser",
    "benchmark_udt_analysis",
    "benchmark_udt_physicalization_token_masking",
)

BASELINE_OUTPUTS = (
    Path("programs/clickhouse"),
    Path("src/DataTypes/benchmarks/benchmark_udt_parser"),
    Path("src/DataTypes/benchmarks/benchmark_udt_analysis"),
    Path("src/Common/benchmarks/benchmark_udt_physicalization_token_masking"),
)

CANDIDATE_OUTPUTS = (
    Path("programs/clickhouse"),
    Path("src/DataTypes/benchmarks/benchmark_udt_catalog_resolver"),
    Path("src/DataTypes/benchmarks/benchmark_udt_parser"),
    Path("src/DataTypes/benchmarks/benchmark_udt_analysis"),
    Path("src/Common/benchmarks/benchmark_udt_physicalization_token_masking"),
)

SOURCE_PROVENANCE_FILES = (
    Path("udt_benchmark_baseline_manifest.json"),
    Path("udt_benchmark_candidate_manifest.json"),
    Path("CMakeLists.txt"),
    Path("cmake/linux/toolchain-x86_64.cmake"),
    Path("src/DataTypes/CMakeLists.txt"),
    Path("src/DataTypes/benchmarks/CMakeLists.txt"),
    Path("src/DataTypes/benchmarks/udt_parser.cpp"),
    Path("src/DataTypes/benchmarks/udt_analysis.cpp"),
    Path("src/Common/benchmarks/CMakeLists.txt"),
    Path("src/Common/benchmarks/udt_physicalization_token_masking.cpp"),
    Path("src/Common/benchmarks/JemallocBenchmarkMemoryManager.h"),
    Path("tests/performance/scripts/udt_prepare_benchmark_baseline.py"),
    Path("tests/performance/scripts/udt_release_benchmark_campaign.py"),
    Path("tests/performance/scripts/udt_microbenchmark_campaign.py"),
    Path("tests/performance/scripts/udt_server_benchmark_campaign.py"),
)


class CampaignError(RuntimeError):
    """The campaign's inputs or persistent state are invalid."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    started_at: str
    finished_at: str
    elapsed_seconds: float
    command_record: Path
    stdout: Path
    stderr: Path
    exception: str | None = None


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def log(message: str) -> None:
    print(f"[{timestamp()}] {message}", flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exception:
        raise CampaignError(f"cannot read valid JSON from {path}: {exception}") from exception


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def ensure_empty_or_missing(path: Path, role: str) -> None:
    if not path.exists():
        return
    if not path.is_dir():
        raise CampaignError(f"{role} is not a directory: {path}")
    try:
        next(path.iterdir())
    except StopIteration:
        return
    raise CampaignError(f"fresh {role} must be empty: {path}")


def file_metadata(path: Path, *, with_hash: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path)}
    if not path.exists():
        result["exists"] = False
        return result
    result["exists"] = True
    stat = path.stat()
    result.update(
        {
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "mode": oct(stat.st_mode & 0o7777),
        }
    )
    if with_hash and path.is_file():
        result["sha256"] = sha256_file(path)
    return result


def source_tree_digest(source: Path) -> dict[str, Any]:
    """Hash the frozen source snapshot without traversing Git or contrib mounts."""

    excluded_top_level = {"contrib"}
    excluded_directory_names = {".git", ".mypy_cache", ".pytest_cache", "__pycache__"}
    digest = hashlib.sha256()
    file_count = 0
    symlink_count = 0
    byte_count = 0
    for directory, directory_names, file_names in os.walk(source, followlinks=False):
        directory_path = Path(directory)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in excluded_directory_names
            and not (directory_path == source and name in excluded_top_level)
        )
        for file_name in sorted(file_names):
            path = directory_path / file_name
            relative = path.relative_to(source).as_posix().encode("utf-8", errors="surrogateescape")
            if path.is_symlink():
                target = os.readlink(path).encode("utf-8", errors="surrogateescape")
                digest.update(b"L\0" + relative + b"\0" + target + b"\0")
                symlink_count += 1
                continue
            if not path.is_file():
                continue
            digest.update(b"F\0" + relative + b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    digest.update(chunk)
                    byte_count += len(chunk)
            digest.update(b"\0")
            file_count += 1
    return {
        "sha256": digest.hexdigest(),
        "regular_files": file_count,
        "symlinks": symlink_count,
        "content_bytes": byte_count,
        "excluded_top_level": sorted(excluded_top_level),
        "excluded_directory_names": sorted(excluded_directory_names),
    }


def git_provenance(source: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        top_level = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exception:
        return {"available": False, "error": str(exception)}
    if top_level.returncode != 0:
        return {"available": False, "error": top_level.stderr.strip()}
    try:
        discovered_top_level = Path(top_level.stdout.strip()).resolve()
    except OSError as exception:
        return {"available": False, "error": str(exception)}
    if discovered_top_level != source.resolve():
        return {
            "available": False,
            "error": "nearest Git worktree does not match the frozen source root",
            "discovered_top_level": str(discovered_top_level),
        }
    result["available"] = True
    for key, arguments in (
        ("head", ("rev-parse", "HEAD")),
        ("tree", ("rev-parse", "HEAD^{tree}")),
    ):
        try:
            completed = subprocess.run(
                ["git", "-C", str(source), *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exception:
            result[f"{key}_error"] = str(exception)
            continue
        if completed.returncode == 0:
            result[key] = completed.stdout.strip()
        else:
            result[f"{key}_error"] = completed.stderr.strip()
    return result


def tool_version(command: Sequence[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"command": list(command)}
    try:
        completed = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        result.update(
            {
                "returncode": completed.returncode,
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
            }
        )
    except (OSError, subprocess.SubprocessError) as exception:
        result["error"] = str(exception)
    return result


def normalized_path(path: Path) -> Path:
    return path.expanduser().resolve()


def parse_cmake_cache(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith(("#", "//")) or "=" not in line:
            continue
        key_and_type, value = line.split("=", 1)
        key = key_and_type.rsplit(":", 1)[0]
        result[key] = value
    return result


def cmake_truthy(value: str | None) -> bool:
    return value is not None and value.upper() not in {
        "",
        "0",
        "FALSE",
        "NO",
        "OFF",
        "IGNORE",
        "NOTFOUND",
    } and not value.upper().endswith("-NOTFOUND")


class Campaign:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.baseline_source = normalized_path(args.baseline_source)
        self.candidate_source = normalized_path(args.candidate_source)
        self.baseline_build = normalized_path(args.baseline_build)
        self.candidate_build = normalized_path(args.candidate_build)
        self.artifacts = normalized_path(args.artifacts)
        self.server_data = normalized_path(args.server_data)
        self.sccache_dir = normalized_path(args.sccache_dir) if args.sccache_dir else None
        self.orchestrator_artifacts = self.artifacts / "orchestrator"
        self.status_path = self.orchestrator_artifacts / "status.json"
        self.terminal_path = self.orchestrator_artifacts / "terminal.json"
        self.provenance_path = self.orchestrator_artifacts / "provenance.json"
        self.lock_path = self.artifacts / ".udt_release_benchmark_campaign.lock"
        self.lock_stream: Any = None
        self.lock_attempted = False
        self.status: dict[str, Any] = {}
        self.requested_rerun_phases = set(args.rerun_phase)
        self.rerun_phases = self.expand_rerun_phases(self.requested_rerun_phases)
        self.ninja = shutil.which(args.ninja)

    @staticmethod
    def expand_rerun_phases(requested: set[str]) -> set[str]:
        """Invalidate every measurement downstream of an explicitly rerun build."""

        expanded = set(requested)
        dependencies = {
            "configure_baseline": {"build_baseline"},
            "configure_candidate": {"build_candidate"},
            "build_baseline": {"microbenchmarks"},
            "build_candidate": {"microbenchmarks", "server_benchmarks"},
        }
        changed = True
        while changed:
            changed = False
            for phase in tuple(expanded):
                for dependent in dependencies.get(phase, set()):
                    if dependent not in expanded:
                        expanded.add(dependent)
                        changed = True
        return expanded

    def validate_paths(self) -> None:
        for source, role in (
            (self.baseline_source, "baseline source"),
            (self.candidate_source, "candidate source"),
        ):
            if not source.is_dir() or not (source / "CMakeLists.txt").is_file():
                raise CampaignError(f"{role} is not a ClickHouse source tree: {source}")
            toolchain = source / "cmake/linux/toolchain-x86_64.cmake"
            if not toolchain.is_file():
                raise CampaignError(f"{role} has no x86_64 toolchain: {toolchain}")

        if self.baseline_source == self.candidate_source:
            raise CampaignError("baseline and candidate source paths must be distinct")

        paths = {
            "baseline source": self.baseline_source,
            "candidate source": self.candidate_source,
            "baseline build": self.baseline_build,
            "candidate build": self.candidate_build,
            "artifacts": self.artifacts,
            "server data": self.server_data,
        }
        values = list(paths.items())
        for index, (left_name, left) in enumerate(values):
            for right_name, right in values[index + 1 :]:
                if left == right:
                    raise CampaignError(f"{left_name} and {right_name} resolve to the same path: {left}")

        for output_name, output_path in (
            ("baseline build", self.baseline_build),
            ("candidate build", self.candidate_build),
            ("artifacts", self.artifacts),
            ("server data", self.server_data),
        ):
            for source_name, source in (
                ("baseline source", self.baseline_source),
                ("candidate source", self.candidate_source),
            ):
                if path_is_relative_to(output_path, source):
                    raise CampaignError(f"{output_name} must not be inside {source_name}: {output_path}")
                if path_is_relative_to(source, output_path):
                    raise CampaignError(f"{output_name} must not contain {source_name}: {output_path}")

        mutable_paths = (
            ("baseline build", self.baseline_build),
            ("candidate build", self.candidate_build),
            ("artifacts", self.artifacts),
            ("server data", self.server_data),
        )
        for index, (left_name, left) in enumerate(mutable_paths):
            for right_name, right in mutable_paths[index + 1 :]:
                if path_is_relative_to(left, right) or path_is_relative_to(right, left):
                    raise CampaignError(f"{left_name} and {right_name} must not be nested")

        if not (self.baseline_source / "src/DataTypes/benchmarks/udt_parser.cpp").is_file():
            raise CampaignError("baseline benchmark overlay has not been prepared")
        baseline_datatypes_benchmarks = (
            self.baseline_source / "src/DataTypes/benchmarks/CMakeLists.txt"
        ).read_text(encoding="utf-8", errors="replace")
        baseline_common_benchmarks = (
            self.baseline_source / "src/Common/benchmarks/CMakeLists.txt"
        ).read_text(encoding="utf-8", errors="replace")
        baseline_sentinels = (
            ("CLICKHOUSE_UDT_PARSER_BENCHMARK=0", baseline_datatypes_benchmarks),
            ("CLICKHOUSE_UDT_ANALYSIS_BENCHMARK=0", baseline_datatypes_benchmarks),
            ("CLICKHOUSE_UDT_TOKEN_MASKING_BENCHMARK=0", baseline_common_benchmarks),
        )
        for sentinel, contents in baseline_sentinels:
            if sentinel not in contents:
                raise CampaignError(f"baseline benchmark overlay is missing sentinel {sentinel}")
        self.validate_baseline_manifest()
        shared_benchmark_sources = (
            Path("src/DataTypes/benchmarks/udt_parser.cpp"),
            Path("src/DataTypes/benchmarks/udt_analysis.cpp"),
            Path("src/Common/benchmarks/udt_physicalization_token_masking.cpp"),
            Path("src/Common/benchmarks/JemallocBenchmarkMemoryManager.h"),
        )
        for relative in shared_benchmark_sources:
            baseline_path = self.baseline_source / relative
            candidate_path = self.candidate_source / relative
            if not candidate_path.is_file() or sha256_file(baseline_path) != sha256_file(candidate_path):
                raise CampaignError(
                    f"baseline and candidate control workload sources differ: {relative}"
                )
        if (self.baseline_source / "src/Parsers/ASTCastTarget.h").exists():
            raise CampaignError("baseline unexpectedly contains the candidate UDT parser surface")
        if not (self.candidate_source / "src/Parsers/ASTCastTarget.h").is_file():
            raise CampaignError("candidate source is missing the UDT parser surface")

        for tool in (
            self.args.cmake,
            self.args.ninja,
            self.args.python,
            "clang-21",
            "clang++-21",
            "sccache",
        ):
            if shutil.which(tool) is None:
                raise CampaignError(f"required executable is not in PATH: {tool}")

        if self.args.jobs <= 0:
            raise CampaignError("--jobs must be positive")

    def validate_baseline_manifest(self) -> None:
        manifest_path = self.baseline_source / "udt_benchmark_baseline_manifest.json"
        manifest = read_json(manifest_path)
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise CampaignError(f"invalid baseline preparation manifest: {manifest_path}")
        baseline = manifest.get("baseline")
        if not isinstance(baseline, dict):
            raise CampaignError("baseline preparation manifest has no baseline identity")
        expected_digest_lengths = {"revision": 40, "tree": 40, "archive_sha256": 64}
        for key, length in expected_digest_lengths.items():
            value = baseline.get(key)
            if (
                not isinstance(value, str)
                or len(value) != length
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise CampaignError(f"baseline preparation manifest has invalid {key}")
        installed_files = manifest.get("installed_files")
        if not isinstance(installed_files, dict) or not installed_files:
            raise CampaignError("baseline preparation manifest has no installed file hashes")
        for relative_text, expected_hash in installed_files.items():
            relative = Path(relative_text)
            if relative.is_absolute() or ".." in relative.parts:
                raise CampaignError(f"unsafe path in baseline preparation manifest: {relative_text}")
            path = self.baseline_source / relative
            if not path.is_file():
                raise CampaignError(f"prepared baseline file is missing: {relative_text}")
            actual_hash = sha256_file(path)
            if actual_hash != expected_hash:
                raise CampaignError(
                    f"prepared baseline file hash mismatch for {relative_text}: "
                    f"expected {expected_hash}, got {actual_hash}"
                )

        candidate_manifest_path = self.candidate_source / "udt_benchmark_candidate_manifest.json"
        if candidate_manifest_path.exists():
            candidate_manifest = read_json(candidate_manifest_path)
            candidate = candidate_manifest.get("candidate") if isinstance(candidate_manifest, dict) else None
            if not isinstance(candidate_manifest, dict) or candidate_manifest.get("schema_version") != 1:
                raise CampaignError(f"invalid candidate source manifest: {candidate_manifest_path}")
            if not isinstance(candidate, dict):
                raise CampaignError("candidate source manifest has no candidate identity")
            expected_candidate_digests = {
                "revision": 40,
                "tree": 40,
                "worktree_status_sha256": 64,
                "worktree_diff_sha256": 64,
            }
            for key, length in expected_candidate_digests.items():
                value = candidate.get(key)
                if (
                    not isinstance(value, str)
                    or len(value) != length
                    or any(character not in "0123456789abcdef" for character in value)
                ):
                    raise CampaignError(f"candidate source manifest has invalid {key}")

    def acquire_lock(self) -> None:
        self.lock_attempted = True
        self.artifacts.mkdir(parents=True, exist_ok=True)
        stream = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exception:
            stream.close()
            raise CampaignError(f"another orchestrator owns {self.lock_path}: {exception}") from exception
        stream.seek(0)
        stream.truncate()
        stream.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "acquired_at": timestamp(),
                },
                sort_keys=True,
            )
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())
        self.lock_stream = stream

    def paths_record(self) -> dict[str, str | None]:
        return {
            "baseline_source": str(self.baseline_source),
            "candidate_source": str(self.candidate_source),
            "baseline_build": str(self.baseline_build),
            "candidate_build": str(self.candidate_build),
            "artifacts": str(self.artifacts),
            "server_data": str(self.server_data),
            "sccache_dir": str(self.sccache_dir) if self.sccache_dir else None,
        }

    def initialize(self) -> None:
        existing = self.status_path.exists()
        if existing and not (self.args.resume or self.rerun_phases):
            raise CampaignError(
                f"campaign status already exists at {self.status_path}; use --resume or --rerun-phase"
            )
        if (self.args.resume or self.rerun_phases) and not existing:
            raise CampaignError(f"cannot resume: campaign status does not exist at {self.status_path}")

        if existing:
            status = read_json(self.status_path)
            if not isinstance(status, dict) or status.get("schema_version") != SCHEMA_VERSION:
                raise CampaignError(f"unsupported campaign status schema in {self.status_path}")
            if status.get("paths") != self.paths_record():
                raise CampaignError("resume paths do not match the persisted campaign paths")
            if status.get("configuration_digest") != self.configuration_digest():
                raise CampaignError("resume configuration does not match the persisted campaign configuration")
            self.status = status
            self.status.setdefault("failures", [])
            self.status.setdefault("phases", {})
            self.status.setdefault("runs", []).append(
                {
                    "started_at": timestamp(),
                    "mode": "rerun" if self.rerun_phases else "resume",
                    "requested_rerun_phases": sorted(self.requested_rerun_phases),
                    "rerun_phases": sorted(self.rerun_phases),
                }
            )
        else:
            ensure_empty_or_missing(self.baseline_build, "baseline build directory")
            ensure_empty_or_missing(self.candidate_build, "candidate build directory")
            ensure_empty_or_missing(self.orchestrator_artifacts, "orchestrator artifact directory")
            ensure_empty_or_missing(self.artifacts / "controllers", "controller artifact directory")
            ensure_empty_or_missing(self.server_data, "server data directory")
            if self.sccache_dir is not None and self.sccache_dir.exists() and not self.sccache_dir.is_dir():
                raise CampaignError(f"sccache path is not a directory: {self.sccache_dir}")
            campaign_id = self.args.campaign_id or (
                f"udt-release-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
            )
            self.status = {
                "schema_version": SCHEMA_VERSION,
                "campaign_id": campaign_id,
                "state": "running",
                "started_at": timestamp(),
                "updated_at": timestamp(),
                "finished_at": None,
                "current_phase": None,
                "paths": self.paths_record(),
                "configuration_digest": self.configuration_digest(),
                "phases": {},
                "failures": [],
                "unresolved_failures": [],
                "runs": [
                    {
                        "started_at": timestamp(),
                        "mode": "fresh",
                        "requested_rerun_phases": [],
                        "rerun_phases": [],
                    }
                ],
            }

        for path in (
            self.baseline_build,
            self.candidate_build,
            self.artifacts,
            self.server_data,
            self.orchestrator_artifacts,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if self.sccache_dir is not None:
            self.sccache_dir.mkdir(parents=True, exist_ok=True)

        self.status.update(
            {
                "state": "running",
                "updated_at": timestamp(),
                "finished_at": None,
                "current_phase": None,
                "configuration_digest": self.configuration_digest(),
            }
        )
        self.write_status()
        atomic_write_json(
            self.terminal_path,
            {
                "schema_version": SCHEMA_VERSION,
                "campaign_id": self.status["campaign_id"],
                "state": "running",
                "started_at": self.status["started_at"],
                "updated_at": timestamp(),
                "status": str(self.status_path),
            },
        )
        provenance = self.collect_provenance()
        run_number = len(self.status["runs"])
        run_provenance_path = self.orchestrator_artifacts / "provenance" / f"run-{run_number:03d}.json"
        atomic_write_json(run_provenance_path, provenance)
        atomic_write_json(self.provenance_path, provenance)
        self.status["runs"][-1]["provenance"] = file_metadata(run_provenance_path)
        self.write_status()

    def write_status(self) -> None:
        self.status["updated_at"] = timestamp()
        atomic_write_json(self.status_path, self.status)

    def configuration_digest(self) -> str:
        normalized = {
            "generator": "Ninja",
            "make_program": self.ninja,
            "definitions": dict(COMMON_CMAKE_DEFINITIONS),
            "toolchain_relative_path": "cmake/linux/toolchain-x86_64.cmake",
            "baseline_targets": BASELINE_TARGETS,
            "candidate_targets": CANDIDATE_TARGETS,
            "jobs": self.args.jobs,
            "cmake": self.args.cmake,
            "ninja": self.args.ninja,
            "python": self.args.python,
            "micro_cpu": self.args.micro_cpu,
            "micro_controller_cpu": self.args.micro_controller_cpu,
            "micro_quiet_attempts": self.args.micro_quiet_attempts,
            "server_benchmark_cpu": self.args.server_benchmark_cpu,
            "server_controller_cpu": self.args.server_controller_cpu,
        }
        return sha256_json(normalized)

    def source_provenance(self, source: Path) -> dict[str, Any]:
        files = {
            str(relative): file_metadata(source / relative)
            for relative in SOURCE_PROVENANCE_FILES
        }
        return {
            "path": str(source),
            "git": git_provenance(source),
            "tree": source_tree_digest(source),
            "files": files,
            "manifest_digest": sha256_json(files),
        }

    def collect_provenance(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "campaign_id": self.status["campaign_id"],
            "collected_at": timestamp(),
            "host": {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "uname": list(platform.uname()),
                "python": sys.version,
                "cpu_count": os.cpu_count(),
                "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            },
            "paths": self.paths_record(),
            "configuration_digest": self.configuration_digest(),
            "cmake": {
                "generator": "Ninja",
                "make_program": self.ninja,
                "definitions": dict(COMMON_CMAKE_DEFINITIONS),
                "toolchain_relative_path": "cmake/linux/toolchain-x86_64.cmake",
            },
            "tools": {
                "cmake": tool_version((self.args.cmake, "--version")),
                "ninja": tool_version((self.args.ninja, "--version")),
                "clang": tool_version(("clang-21", "--version")),
                "clangxx": tool_version(("clang++-21", "--version")),
                "sccache": tool_version(("sccache", "--version")),
            },
            "sources": {
                "baseline": self.source_provenance(self.baseline_source),
                "candidate": self.source_provenance(self.candidate_source),
            },
        }

    def phase(self, name: str) -> dict[str, Any]:
        phase = self.status["phases"].setdefault(
            name,
            {
                "status": "pending",
                "attempts": [],
                "latest_attempt": None,
            },
        )
        return phase

    def should_run(self, name: str) -> bool:
        phase = self.phase(name)
        if name in self.rerun_phases:
            return True
        return phase.get("status") not in RESUMABLE_TERMINAL_PHASE_STATES

    def begin_attempt(self, name: str, command: Sequence[str] | None) -> tuple[dict[str, Any], Path]:
        phase = self.phase(name)
        attempt_number = len(phase["attempts"]) + 1
        attempt_dir = self.orchestrator_artifacts / "phases" / name / f"attempt-{attempt_number:03d}"
        attempt_dir.mkdir(parents=True, exist_ok=False)
        attempt: dict[str, Any] = {
            "number": attempt_number,
            "status": "running",
            "started_at": timestamp(),
            "finished_at": None,
            "command": list(command) if command is not None else None,
            "attempt_artifacts": str(attempt_dir),
        }
        phase["status"] = "running"
        phase["latest_attempt"] = attempt_number
        phase["attempts"].append(attempt)
        self.status["current_phase"] = name
        self.write_status()
        log(f"phase {name} attempt {attempt_number} started")
        return attempt, attempt_dir

    def finish_attempt(
        self,
        name: str,
        attempt: dict[str, Any],
        status: str,
        *,
        command_result: CommandResult | None = None,
        details: dict[str, Any] | None = None,
        failure_kind: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        attempt["status"] = status
        attempt["finished_at"] = timestamp()
        if command_result is not None:
            attempt["returncode"] = command_result.returncode
            attempt["elapsed_seconds"] = command_result.elapsed_seconds
            attempt["command_record"] = str(command_result.command_record)
            attempt["stdout"] = str(command_result.stdout)
            attempt["stderr"] = str(command_result.stderr)
            if command_result.exception:
                attempt["exception"] = command_result.exception
        if details:
            attempt["details"] = details
        phase = self.phase(name)
        phase["status"] = status
        phase["finished_at"] = attempt["finished_at"]
        if details:
            phase["latest_details"] = details
        if failure_kind or failure_message:
            failure = {
                "phase": name,
                "attempt": attempt["number"],
                "kind": failure_kind or "infra_or_code",
                "message": failure_message or f"phase {name} failed",
                "recorded_at": timestamp(),
            }
            self.status["failures"].append(failure)
            attempt["failure"] = failure
        self.status["current_phase"] = None
        self.write_status()
        log(f"phase {name} attempt {attempt['number']} finished: {status}")

    def skip_phase(self, name: str, reason: str, prerequisites: Iterable[str] = ()) -> None:
        if not self.should_run(name):
            return
        attempt, _ = self.begin_attempt(name, None)
        self.finish_attempt(
            name,
            attempt,
            "skipped",
            details={"reason": reason, "prerequisites": list(prerequisites)},
        )

    def fail_phase_without_command(self, name: str, message: str) -> None:
        attempt, _ = self.begin_attempt(name, None)
        self.finish_attempt(
            name,
            attempt,
            "failed",
            failure_kind="infra_or_code",
            failure_message=message,
        )

    def run_phase_safely(self, name: str, operation: Callable[[], None]) -> None:
        """Record an unexpected phase exception and continue independent phases."""

        try:
            operation()
        except Exception as exception:
            exception_details = {
                "exception": f"{type(exception).__name__}: {exception}",
                "traceback": traceback.format_exc(),
            }
            phase = self.phase(name)
            if phase.get("status") == "running" and phase.get("attempts"):
                attempt = phase["attempts"][-1]
                attempt_dir = Path(attempt["attempt_artifacts"])
                traceback_path = attempt_dir / "orchestrator-exception.txt"
                traceback_path.write_text(exception_details["traceback"], encoding="utf-8")
                exception_details["traceback_path"] = str(traceback_path)
                self.finish_attempt(
                    name,
                    attempt,
                    "failed",
                    details=exception_details,
                    failure_kind="infra_or_code",
                    failure_message=exception_details["exception"],
                )
            else:
                try:
                    self.fail_phase_without_command(name, exception_details["exception"])
                    latest = self.phase(name)["attempts"][-1]
                    latest.setdefault("details", {}).update(exception_details)
                    self.write_status()
                except Exception as recording_exception:
                    failure = {
                        "phase": name,
                        "kind": "infra_or_code",
                        "message": exception_details["exception"],
                        "recording_error": f"{type(recording_exception).__name__}: {recording_exception}",
                        "recorded_at": timestamp(),
                    }
                    phase["status"] = "failed"
                    self.status["failures"].append(failure)
                    self.status["current_phase"] = None
                    self.write_status()
            log(f"phase {name} raised an unexpected exception; continuing independent phases")

    def command_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update({"CC": "clang-21", "CXX": "clang++-21"})
        if self.sccache_dir is not None:
            environment["SCCACHE_DIR"] = str(self.sccache_dir)
        return environment

    def run_command(
        self,
        name: str,
        command: Sequence[str],
        attempt_dir: Path,
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> CommandResult:
        stdout_path = attempt_dir / "stdout.log"
        stderr_path = attempt_dir / "stderr.log"
        command_record_path = attempt_dir / "command.json"
        started_at = timestamp()
        monotonic_started = time.monotonic()
        selected_environment = {
            key: value
            for key, value in (environment or os.environ).items()
            if key in {"PATH", "CC", "CXX", "SCCACHE_DIR", "SCCACHE_CACHE_SIZE"}
        }
        record: dict[str, Any] = {
            "phase": name,
            "started_at": started_at,
            "finished_at": None,
            "argv": list(command),
            "shell_escaped": shlex.join(command),
            "cwd": str(cwd) if cwd is not None else os.getcwd(),
            "environment": selected_environment,
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "returncode": None,
        }
        atomic_write_json(command_record_path, record)
        returncode: int | None = None
        exception_text: str | None = None
        try:
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                process = subprocess.Popen(
                    list(command),
                    cwd=str(cwd) if cwd is not None else None,
                    env=environment,
                    stdout=stdout,
                    stderr=stderr,
                )
                record["pid"] = process.pid
                atomic_write_json(command_record_path, record)
                returncode = process.wait()
        except BaseException as exception:
            exception_text = f"{type(exception).__name__}: {exception}"
            with stderr_path.open("ab") as stderr:
                stderr.write((exception_text + "\n").encode("utf-8", errors="replace"))
            if isinstance(exception, (KeyboardInterrupt, SystemExit)):
                raise
        finished_at = timestamp()
        elapsed_seconds = time.monotonic() - monotonic_started
        record.update(
            {
                "finished_at": finished_at,
                "elapsed_seconds": elapsed_seconds,
                "returncode": returncode,
                "exception": exception_text,
            }
        )
        atomic_write_json(command_record_path, record)
        return CommandResult(
            returncode=returncode,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds,
            command_record=command_record_path,
            stdout=stdout_path,
            stderr=stderr_path,
            exception=exception_text,
        )

    def cmake_command(self, source: Path, build: Path) -> list[str]:
        command = [
            self.args.cmake,
            "-S",
            str(source),
            "-B",
            str(build),
            "-G",
            "Ninja",
        ]
        command.extend(f"-D{key}={value}" for key, value in COMMON_CMAKE_DEFINITIONS)
        command.append(f"-DCMAKE_MAKE_PROGRAM={self.ninja}")
        command.append(f"-DCMAKE_TOOLCHAIN_FILE={source / 'cmake/linux/toolchain-x86_64.cmake'}")
        return command

    def validate_cmake_cache(self, cache_path: Path, source: Path) -> dict[str, Any]:
        errors: list[str] = []
        try:
            cache = parse_cmake_cache(cache_path)
        except OSError as exception:
            return {"valid": False, "errors": [str(exception)], "selected_values": {}}

        for key, expected in COMMON_CMAKE_DEFINITIONS:
            actual = cache.get(key)
            if expected == "ON":
                matches = cmake_truthy(actual)
            elif expected == "OFF":
                matches = actual is not None and not cmake_truthy(actual)
            elif key in {"CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER"}:
                matches = actual is not None and Path(actual).name == expected
            else:
                matches = actual == expected
            if not matches:
                errors.append(f"{key}: expected {expected!r}, got {actual!r}")

        scalar_expectations = {
            "CMAKE_GENERATOR": "Ninja",
            "CMAKE_HOME_DIRECTORY": str(source),
            "CMAKE_TOOLCHAIN_FILE": str(source / "cmake/linux/toolchain-x86_64.cmake"),
        }
        for key, expected in scalar_expectations.items():
            actual = cache.get(key)
            if actual != expected:
                errors.append(f"{key}: expected {expected!r}, got {actual!r}")

        make_program = cache.get("CMAKE_MAKE_PROGRAM")
        if make_program is None or Path(make_program).resolve() != Path(self.ninja or "").resolve():
            errors.append(f"CMAKE_MAKE_PROGRAM: expected {self.ninja!r}, got {make_program!r}")

        selected_keys = [key for key, _ in COMMON_CMAKE_DEFINITIONS] + [
            "CMAKE_GENERATOR",
            "CMAKE_HOME_DIRECTORY",
            "CMAKE_TOOLCHAIN_FILE",
            "CMAKE_MAKE_PROGRAM",
        ]
        return {
            "valid": not errors,
            "errors": errors,
            "selected_values": {key: cache.get(key) for key in selected_keys},
        }

    def configure(self, role: str, source: Path, build: Path) -> None:
        name = f"configure_{role}"
        if not self.should_run(name):
            log(f"phase {name} already has a resumable terminal result; skipping")
            return
        command = self.cmake_command(source, build)
        attempt, attempt_dir = self.begin_attempt(name, command)
        result = self.run_command(name, command, attempt_dir, environment=self.command_environment())
        details: dict[str, Any] = {
            "source": str(source),
            "build": str(build),
            "configuration_digest": self.configuration_digest(),
        }
        cache = build / "CMakeCache.txt"
        cache_validation: dict[str, Any] = {
            "valid": False,
            "errors": ["CMakeCache.txt was not written"],
            "selected_values": {},
        }
        if cache.is_file():
            copied_cache = attempt_dir / "CMakeCache.txt"
            shutil.copy2(cache, copied_cache)
            details["cmake_cache"] = file_metadata(copied_cache)
            cache_validation = self.validate_cmake_cache(cache, source)
        details["cache_validation"] = cache_validation
        if result.returncode == 0 and cache.is_file() and cache_validation["valid"]:
            status = "succeeded"
            failure_kind = None
            failure_message = None
        else:
            status = "failed"
            failure_kind = "infra_or_code"
            failure_message = (
                f"{name} returned {result.returncode}; cache errors: {cache_validation['errors']}"
                if result.exception is None
                else f"{name} could not run: {result.exception}"
            )
        self.finish_attempt(
            name,
            attempt,
            status,
            command_result=result,
            details=details,
            failure_kind=failure_kind,
            failure_message=failure_message,
        )

    def phase_succeeded(self, name: str) -> bool:
        return self.phase(name).get("status") == "succeeded"

    def build(self, role: str, build: Path, targets: Sequence[str], outputs: Sequence[Path]) -> None:
        name = f"build_{role}"
        configure_name = f"configure_{role}"
        if not self.should_run(name):
            log(f"phase {name} already has a resumable terminal result; skipping")
            return
        if not self.phase_succeeded(configure_name):
            self.skip_phase(name, f"{configure_name} is not successful", (configure_name,))
            return
        command = [
            self.args.cmake,
            "--build",
            str(build),
            "--parallel",
            str(self.args.jobs),
            "--target",
            *targets,
        ]
        attempt, attempt_dir = self.begin_attempt(name, command)
        result = self.run_command(name, command, attempt_dir, environment=self.command_environment())
        missing_outputs = [str(build / output) for output in outputs if not (build / output).is_file()]
        output_metadata: dict[str, Any] = {}
        if result.returncode == 0 and not missing_outputs:
            for output in outputs:
                absolute = build / output
                output_metadata[str(output)] = file_metadata(absolute)
        details: dict[str, Any] = {
            "build": str(build),
            "targets": list(targets),
            "expected_outputs": [str(path) for path in outputs],
            "missing_outputs": missing_outputs,
            "outputs": output_metadata,
        }
        cache = build / "CMakeCache.txt"
        if cache.is_file():
            copied_cache = attempt_dir / "CMakeCache.txt"
            shutil.copy2(cache, copied_cache)
            details["cmake_cache"] = file_metadata(copied_cache)
        if result.returncode == 0 and not missing_outputs:
            status = "succeeded"
            failure_kind = None
            failure_message = None
        else:
            status = "failed"
            failure_kind = "infra_or_code"
            failure_message = f"{name} returned {result.returncode}; missing outputs: {missing_outputs}"
        self.finish_attempt(
            name,
            attempt,
            status,
            command_result=result,
            details=details,
            failure_kind=failure_kind,
            failure_message=failure_message,
        )

    def controller_artifacts(self, controller: str, attempt_number: int) -> Path:
        return self.artifacts / "controllers" / controller / f"attempt-{attempt_number:03d}"

    def run_microbenchmarks(self) -> None:
        name = "microbenchmarks"
        if not self.should_run(name):
            log(f"phase {name} already has a resumable terminal result; skipping")
            return
        prerequisites = ("build_baseline", "build_candidate")
        if not all(self.phase_succeeded(prerequisite) for prerequisite in prerequisites):
            self.skip_phase(name, "both Release builds are required", prerequisites)
            return
        script = self.candidate_source / "tests/performance/scripts/udt_microbenchmark_campaign.py"
        if not script.is_file():
            self.fail_phase_without_command(name, f"microbenchmark controller is missing: {script}")
            return
        next_attempt = len(self.phase(name)["attempts"]) + 1
        controller_artifacts: Path | None = None
        for previous_attempt in reversed(self.phase(name)["attempts"]):
            previous_path = previous_attempt.get("details", {}).get("controller_artifacts")
            if previous_path:
                candidate_path = Path(previous_path)
                if (candidate_path / "plan.json").is_file():
                    controller_artifacts = candidate_path
                    break
        resume_controller = controller_artifacts is not None
        if controller_artifacts is None:
            controller_artifacts = self.controller_artifacts("microbenchmarks", next_attempt)
            ensure_empty_or_missing(controller_artifacts, "microbenchmark controller artifact directory")
        command = [
            self.args.python,
            str(script),
            "--baseline-build",
            str(self.baseline_build),
            "--candidate-build",
            str(self.candidate_build),
            "--artifacts",
            str(controller_artifacts),
            "--cpu",
            str(self.args.micro_cpu),
            "--controller-cpu",
            str(self.args.micro_controller_cpu),
            "--quiet-attempts",
            str(self.args.micro_quiet_attempts),
        ]
        if resume_controller:
            command.append("--resume")
        attempt, attempt_dir = self.begin_attempt(name, command)
        result = self.run_command(name, command, attempt_dir, environment=self.command_environment())
        initial_summary_path = controller_artifacts / "campaign-summary.json"
        latest_summary_path = controller_artifacts / "latest-summary.json"
        summary_path = initial_summary_path
        latest_summary_pointer: dict[str, Any] | None = None
        summary_pointer_error: str | None = None
        if latest_summary_path.is_file():
            try:
                pointer_value = read_json(latest_summary_path)
                if not isinstance(pointer_value, dict) or not isinstance(pointer_value.get("summary"), str):
                    raise CampaignError("latest-summary.json has no summary path")
                pointed_path = Path(pointer_value["summary"])
                if not pointed_path.is_absolute():
                    pointed_path = controller_artifacts / pointed_path
                pointed_path = pointed_path.resolve()
                if not path_is_relative_to(pointed_path, controller_artifacts.resolve()):
                    raise CampaignError("latest-summary.json points outside controller artifacts")
                latest_summary_pointer = pointer_value
                summary_path = pointed_path
            except CampaignError as exception:
                summary_pointer_error = str(exception)
        outcome_path = controller_artifacts / "controller.outcome"
        details: dict[str, Any] = {
            "controller_artifacts": str(controller_artifacts),
            "resumed_controller": resume_controller,
            "initial_summary": file_metadata(initial_summary_path),
            "latest_summary_pointer": file_metadata(latest_summary_path),
            "resolved_summary": file_metadata(summary_path),
            "controller_outcome": file_metadata(outcome_path),
        }
        if latest_summary_pointer is not None:
            details["latest_summary_pointer_value"] = latest_summary_pointer
        if summary_pointer_error is not None:
            details["summary_pointer_error"] = summary_pointer_error
        outcome: str | None = None
        if outcome_path.is_file():
            outcome = outcome_path.read_text(encoding="utf-8", errors="replace").strip()
            details["outcome"] = outcome
        if summary_path.is_file():
            try:
                details["summary"] = read_json(summary_path)
            except CampaignError as exception:
                details["summary_error"] = str(exception)
        if result.returncode == 0:
            status = "succeeded"
        elif result.returncode == 2:
            status = "performance_failure"
        elif result.returncode == 3:
            status = "inconclusive"
        else:
            status = "failed"
        failure_kind = "infra_or_code" if status == "failed" else None
        failure_message = f"microbenchmark controller returned {result.returncode}" if status == "failed" else None
        if status != "failed" and not (
            latest_summary_path.is_file() and summary_path.is_file() and outcome_path.is_file()
        ):
            status = "failed"
            failure_kind = "infra_or_code"
            failure_message = "microbenchmark controller did not write all terminal artifacts"
        if status != "failed" and summary_pointer_error is not None:
            status = "failed"
            failure_kind = "infra_or_code"
            failure_message = summary_pointer_error
        expected_outcomes = {0: "pass", 2: "fail", 3: "inconclusive"}
        if status != "failed" and outcome != expected_outcomes.get(result.returncode):
            status = "failed"
            failure_kind = "infra_or_code"
            failure_message = (
                f"microbenchmark exit {result.returncode} disagrees with controller outcome {outcome!r}"
            )
        self.finish_attempt(
            name,
            attempt,
            status,
            command_result=result,
            details=details,
            failure_kind=failure_kind,
            failure_message=failure_message,
        )

    def server_controller_command(self, script: Path, controller_artifacts: Path) -> list[str]:
        attempt_number = len(self.phase("server_benchmarks")["attempts"]) + 1
        data_root = self.server_data / f"attempt-{attempt_number:03d}"
        ensure_empty_or_missing(data_root, "server benchmark data directory")
        return [
            self.args.python,
            str(script),
            "--clickhouse",
            str(self.candidate_build / "programs/clickhouse"),
            "--output-dir",
            str(controller_artifacts),
            "--data-root",
            str(data_root),
            "--benchmark-cpu",
            str(self.args.server_benchmark_cpu),
            "--controller-cpu",
            str(self.args.server_controller_cpu),
        ]

    def classify_server_result(self, result: CommandResult, summary: dict[str, Any] | None) -> str:
        summary_status = summary.get("status") if summary is not None else None
        if (
            result.returncode == 0
            and isinstance(summary_status, dict)
            and summary_status.get("state") == "finished"
            and summary_status.get("failed") == 0
        ):
            return "succeeded"
        return "failed"

    def run_server_benchmarks(self) -> None:
        name = "server_benchmarks"
        if not self.should_run(name):
            log(f"phase {name} already has a resumable terminal result; skipping")
            return
        prerequisite = "build_candidate"
        if not self.phase_succeeded(prerequisite):
            self.skip_phase(name, "the candidate Release server build is required", (prerequisite,))
            return
        script = self.candidate_source / "tests/performance/scripts/udt_server_benchmark_campaign.py"
        if not script.is_file():
            self.fail_phase_without_command(name, f"server benchmark controller is missing: {script}")
            return
        next_attempt = len(self.phase(name)["attempts"]) + 1
        controller_artifacts = self.controller_artifacts("server_benchmarks", next_attempt)
        ensure_empty_or_missing(controller_artifacts, "server controller artifact directory")
        try:
            command = self.server_controller_command(script, controller_artifacts)
        except CampaignError as exception:
            self.fail_phase_without_command(name, str(exception))
            return
        attempt, attempt_dir = self.begin_attempt(name, command)
        result = self.run_command(name, command, attempt_dir, environment=self.command_environment())
        summary_path = controller_artifacts / "summary.json"
        status_path = controller_artifacts / "status.json"
        fatal_path = controller_artifacts / "fatal.json"
        preflight_path = controller_artifacts / "preflight/result.json"
        parsed_summary: dict[str, Any] | None = None
        details: dict[str, Any] = {
            "controller_artifacts": str(controller_artifacts),
            "summary_artifact": file_metadata(summary_path),
            "status": file_metadata(status_path),
            "fatal": file_metadata(fatal_path),
            "preflight": file_metadata(preflight_path),
        }
        if summary_path.is_file():
            try:
                value = read_json(summary_path)
                if isinstance(value, dict):
                    parsed_summary = value
                    details["summary"] = value
                else:
                    details["summary_error"] = "campaign summary is not a JSON object"
            except CampaignError as exception:
                details["summary_error"] = str(exception)
        status = self.classify_server_result(result, parsed_summary)
        failure_kind = "infra_or_code" if status == "failed" else None
        failure_message = f"server benchmark controller returned {result.returncode}" if status == "failed" else None
        if status != "failed" and not summary_path.is_file():
            status = "failed"
            failure_kind = "infra_or_code"
            failure_message = "server benchmark controller did not write a terminal summary"
        self.finish_attempt(
            name,
            attempt,
            status,
            command_result=result,
            details=details,
            failure_kind=failure_kind,
            failure_message=failure_message,
        )

    def finalize(self) -> int:
        active_failures: list[dict[str, Any]] = []
        for name in PHASES:
            phase = self.phase(name)
            status = phase.get("status")
            if status == "failed":
                active_failures.append(
                    {
                        "phase": name,
                        "status": status,
                        "attempt": phase.get("latest_attempt"),
                        "reason": "latest attempt is an infra/code failure",
                    }
                )
            elif status in {"pending", "running"}:
                active_failures.append(
                    {
                        "phase": name,
                        "status": status,
                        "attempt": phase.get("latest_attempt"),
                        "reason": "phase has no terminal result",
                    }
                )
            elif status == "skipped":
                prerequisites = phase.get("latest_details", {}).get("prerequisites", [])
                if not prerequisites or any(not self.phase_succeeded(item) for item in prerequisites):
                    active_failures.append(
                        {
                            "phase": name,
                            "status": status,
                            "attempt": phase.get("latest_attempt"),
                            "reason": phase.get("latest_details", {}).get("reason", "required phase was skipped"),
                        }
                    )

        state = "failed" if active_failures else "completed"
        exit_code = 1 if active_failures else 0
        self.status.update(
            {
                "state": state,
                "current_phase": None,
                "finished_at": timestamp(),
                "unresolved_failures": active_failures,
                "exit_code": exit_code,
            }
        )
        if self.status.get("runs"):
            self.status["runs"][-1]["finished_at"] = self.status["finished_at"]
            self.status["runs"][-1]["state"] = state
            self.status["runs"][-1]["exit_code"] = exit_code
        self.write_status()
        terminal = {
            "schema_version": SCHEMA_VERSION,
            "campaign_id": self.status["campaign_id"],
            "state": state,
            "exit_code": exit_code,
            "started_at": self.status["started_at"],
            "finished_at": self.status["finished_at"],
            "status": str(self.status_path),
            "unresolved_failures": active_failures,
        }
        atomic_write_json(self.terminal_path, terminal)
        log(f"campaign finished: state={state} exit_code={exit_code}")
        return exit_code

    def fail_orchestrator(self, exception: BaseException) -> int:
        failure = {
            "phase": self.status.get("current_phase"),
            "kind": "orchestrator",
            "message": f"{type(exception).__name__}: {exception}",
            "recorded_at": timestamp(),
            "traceback": traceback.format_exc(),
        }
        self.status.setdefault("failures", []).append(failure)
        self.status.update(
            {
                "state": "failed",
                "finished_at": timestamp(),
                "exit_code": 1,
                "unresolved_failures": [failure],
            }
        )
        try:
            self.write_status()
            atomic_write_json(
                self.terminal_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "campaign_id": self.status.get("campaign_id"),
                    "state": "failed",
                    "exit_code": 1,
                    "finished_at": self.status["finished_at"],
                    "status": str(self.status_path),
                    "unresolved_failures": [failure],
                },
            )
        except OSError:
            pass
        log(f"orchestrator failed: {failure['message']}")
        return 1

    def record_bootstrap_failure(self, exception: BaseException) -> None:
        """Leave a watcher-visible terminal result when validation fails early."""

        if self.status_path.exists() or (self.lock_attempted and self.lock_stream is None):
            return
        unsafe_artifact_path = self.artifacts == Path("/") or any(
            path_is_relative_to(self.artifacts, source) or path_is_relative_to(source, self.artifacts)
            for source in (self.baseline_source, self.candidate_source)
        )
        if unsafe_artifact_path or (self.artifacts.exists() and not self.artifacts.is_dir()):
            return
        failure = {
            "phase": "validation",
            "kind": "infra_or_code",
            "message": f"{type(exception).__name__}: {exception}",
            "recorded_at": timestamp(),
            "traceback": traceback.format_exc(),
        }
        campaign_id = self.args.campaign_id or f"udt-release-bootstrap-{uuid.uuid4().hex[:8]}"
        status = {
            "schema_version": SCHEMA_VERSION,
            "campaign_id": campaign_id,
            "state": "failed",
            "started_at": timestamp(),
            "updated_at": timestamp(),
            "finished_at": timestamp(),
            "current_phase": None,
            "paths": self.paths_record(),
            "phases": {},
            "failures": [failure],
            "unresolved_failures": [failure],
            "exit_code": 1,
        }
        terminal = {
            "schema_version": SCHEMA_VERSION,
            "campaign_id": campaign_id,
            "state": "failed",
            "exit_code": 1,
            "started_at": status["started_at"],
            "finished_at": status["finished_at"],
            "status": str(self.status_path),
            "unresolved_failures": [failure],
        }
        try:
            atomic_write_json(self.status_path, status)
            atomic_write_json(self.terminal_path, terminal)
        except OSError:
            pass

    def run(self) -> int:
        self.validate_paths()
        self.acquire_lock()
        try:
            self.initialize()
        except BaseException as exception:
            if self.status.get("campaign_id"):
                return self.fail_orchestrator(exception)
            raise
        try:
            self.run_phase_safely(
                "configure_baseline",
                lambda: self.configure("baseline", self.baseline_source, self.baseline_build),
            )
            self.run_phase_safely(
                "configure_candidate",
                lambda: self.configure("candidate", self.candidate_source, self.candidate_build),
            )
            self.run_phase_safely(
                "build_baseline",
                lambda: self.build("baseline", self.baseline_build, BASELINE_TARGETS, BASELINE_OUTPUTS),
            )
            self.run_phase_safely(
                "build_candidate",
                lambda: self.build("candidate", self.candidate_build, CANDIDATE_TARGETS, CANDIDATE_OUTPUTS),
            )
            self.run_phase_safely("microbenchmarks", self.run_microbenchmarks)
            self.run_phase_safely("server_benchmarks", self.run_server_benchmarks)
            return self.finalize()
        except BaseException as exception:
            if isinstance(exception, KeyboardInterrupt):
                log("orchestrator interrupted")
            return self.fail_orchestrator(exception)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-source", type=Path, required=True)
    parser.add_argument("--candidate-source", type=Path, required=True)
    parser.add_argument("--baseline-build", type=Path, required=True)
    parser.add_argument("--candidate-build", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--server-data", type=Path, required=True)
    parser.add_argument("--sccache-dir", type=Path)
    parser.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--cmake", default="cmake")
    parser.add_argument("--ninja", default="ninja")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--campaign-id")
    parser.add_argument("--micro-cpu", type=int, default=30)
    parser.add_argument("--micro-controller-cpu", type=int, default=29)
    parser.add_argument("--micro-quiet-attempts", type=int, default=180)
    parser.add_argument("--server-benchmark-cpu", type=int, default=30)
    parser.add_argument("--server-controller-cpu", type=int, default=29)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--rerun-phase",
        action="append",
        default=[],
        choices=PHASES,
        help="rerun this phase even when its latest result is resumable; may be repeated",
    )
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args(argv)
    if args.micro_cpu < 0 or args.micro_controller_cpu < 0 or args.micro_cpu == args.micro_controller_cpu:
        parser.error("microbenchmark CPU IDs must be distinct non-negative integers")
    if args.micro_quiet_attempts <= 0:
        parser.error("--micro-quiet-attempts must be positive")
    if (
        args.server_benchmark_cpu < 0
        or args.server_controller_cpu < 0
        or args.server_benchmark_cpu == args.server_controller_cpu
    ):
        parser.error("server benchmark CPU IDs must be distinct non-negative integers")
    return args


def static_plan(args: argparse.Namespace) -> dict[str, Any]:
    baseline_source = normalized_path(args.baseline_source)
    candidate_source = normalized_path(args.candidate_source)
    baseline_build = normalized_path(args.baseline_build)
    candidate_build = normalized_path(args.candidate_build)

    def configure(source: Path, build: Path) -> list[str]:
        command = [args.cmake, "-S", str(source), "-B", str(build), "-G", "Ninja"]
        command.extend(f"-D{key}={value}" for key, value in COMMON_CMAKE_DEFINITIONS)
        command.append(f"-DCMAKE_MAKE_PROGRAM={shutil.which(args.ninja) or args.ninja}")
        command.append(f"-DCMAKE_TOOLCHAIN_FILE={source / 'cmake/linux/toolchain-x86_64.cmake'}")
        return command

    return {
        "schema_version": SCHEMA_VERSION,
        "phases": list(PHASES),
        "configure_baseline": configure(baseline_source, baseline_build),
        "configure_candidate": configure(candidate_source, candidate_build),
        "build_baseline": [
            args.cmake,
            "--build",
            str(baseline_build),
            "--parallel",
            str(args.jobs),
            "--target",
            *BASELINE_TARGETS,
        ],
        "build_candidate": [
            args.cmake,
            "--build",
            str(candidate_build),
            "--parallel",
            str(args.jobs),
            "--target",
            *CANDIDATE_TARGETS,
        ],
        "microbenchmarks": {
            "controller": str(candidate_source / "tests/performance/scripts/udt_microbenchmark_campaign.py"),
            "accepted_exit_codes": {
                "0": "succeeded",
                "2": "performance_failure",
                "3": "inconclusive",
                "1": "infra_or_code_failure",
            },
        },
        "server_benchmarks": {
            "controller": str(candidate_source / "tests/performance/scripts/udt_server_benchmark_campaign.py"),
            "clickhouse": str(candidate_build / "programs/clickhouse"),
            "output_dir": str(normalized_path(args.artifacts) / "controllers/server_benchmarks/attempt-NNN"),
            "data_root": str(normalized_path(args.server_data) / "attempt-NNN"),
            "accepted_exit_codes": {
                "0": "succeeded",
                "1": "scenario_or_code_failure",
                "2": "fatal_infra_or_provenance_failure",
            },
        },
        "terminal_status": str(normalized_path(args.artifacts) / "orchestrator/terminal.json"),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.plan_only:
        print(json.dumps(static_plan(args), indent=2, sort_keys=True))
        return 0
    campaign = Campaign(args)
    try:
        return campaign.run()
    except BaseException as exception:
        campaign.record_bootstrap_failure(exception)
        print(f"fatal campaign setup error: {type(exception).__name__}: {exception}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
