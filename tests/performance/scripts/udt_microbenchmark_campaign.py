#!/usr/bin/env python3
"""Run the reproducible Release UDT microbenchmark campaign.

The controller deliberately keeps execution, validation, and statistical
analysis in one place.  Every benchmark invocation is a fresh process and all
raw output is retained, including output from failed invocations.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


BLOCKS = 12
REPETITIONS = 9
MINIMUM_TIME_SECONDS = 0.5
MINIMUM_WARMUP_SECONDS = 0.2
CANDIDATE_PROCESSES_PER_FAMILY = 3
SCHEDULE_SEED = "udt-release-microbenchmark-campaign"
TWO_SIDED_T95_DF11 = 2.200985
ONE_SIDED_T95_DF11 = 1.795885
AA_EQUIVALENCE_LOW = 0.975
AA_EQUIVALENCE_HIGH = 1.025
AB_ONE_SIDED_UCB_LIMIT = 1.05
TIME_SCALE_NS = {"ns": 1.0, "us": 1_000.0, "ms": 1_000_000.0, "s": 1_000_000_000.0}


class CampaignError(RuntimeError):
    """The campaign or one of its artifacts is invalid."""


@dataclass(frozen=True)
class Family:
    name: str
    relative_binary: Path
    common_filter: str | None
    candidate_filter: str
    common_count: int
    candidate_count: int
    timeout_seconds: int
    operation_memory_counters: bool


FAMILIES = (
    Family(
        name="parser",
        relative_binary=Path("src/DataTypes/benchmarks/benchmark_udt_parser"),
        common_filter=r"^UDTParser/(Parse|Format)/Control/.*$",
        candidate_filter=r"^UDTParser/(Parse|Format)/UDT/.*$",
        common_count=36,
        candidate_count=44,
        timeout_seconds=30 * 60,
        operation_memory_counters=True,
    ),
    Family(
        name="analysis",
        relative_binary=Path("src/DataTypes/benchmarks/benchmark_udt_analysis"),
        common_filter=r"^UDTAnalysis/(QueryTree|ExpressionAnalyzer|FactoryRoute|TypeFactory)/Control/.*$",
        candidate_filter=r"^UDTAnalysis/(QueryTree|ExpressionAnalyzer|FactoryRoute|TypeFactory)/UDT/.*$",
        common_count=30,
        candidate_count=32,
        timeout_seconds=30 * 60,
        operation_memory_counters=True,
    ),
    Family(
        name="token_masking",
        relative_binary=Path("src/Common/benchmarks/benchmark_udt_physicalization_token_masking"),
        common_filter=r"^UDTTokenMask/ParseError/Control/Ordinary/(64|1024|16384|262144)$",
        candidate_filter=r"^UDTTokenMask/(Contains|Mask|FailurePipeline)/.*$",
        common_count=4,
        candidate_count=32,
        timeout_seconds=20 * 60,
        operation_memory_counters=True,
    ),
    Family(
        name="catalog_resolver",
        relative_binary=Path("src/DataTypes/benchmarks/benchmark_udt_catalog_resolver"),
        common_filter=None,
        candidate_filter=r"^BM_.*$",
        common_count=0,
        candidate_count=18,
        timeout_seconds=30 * 60,
        operation_memory_counters=False,
    ),
)


PARSER_COUNTERS = (
    "ast_depth",
    "ast_hash_high64_high32",
    "ast_hash_high64_low32",
    "ast_hash_low64_high32",
    "ast_hash_low64_low32",
    "ast_nodes",
    "formatted_bytes",
    "formatted_hash_high64_high32",
    "formatted_hash_high64_low32",
    "formatted_hash_low64_high32",
    "formatted_hash_low64_low32",
    "function_nodes",
    "input_bytes",
    "parser_backtracks",
    "qualified_references",
    "type_depth",
    "width",
)

ANALYSIS_COUNTERS = (
    "ast_depth",
    "ast_hash_high64_high32",
    "ast_hash_high64_low32",
    "ast_hash_low64_high32",
    "ast_hash_low64_low32",
    "ast_nodes",
    "classified_factory",
    "expected_error_code",
    "feature_enabled",
    "function_nodes",
    "input_bytes",
    "output_bytes",
    "output_hash_high64_high32",
    "output_hash_high64_low32",
    "output_hash_low64_high32",
    "output_hash_low64_low32",
    "type_depth",
    "width",
)

TOKEN_MASKING_COUNTERS = ("input_bytes", "operations_per_iteration")
OPTIONAL_TOKEN_MASKING_COUNTERS = ("expected_redactions", "output_bytes", "statements", "complexity_n")
OPERATION_MEMORY_COUNTERS = (
    "operation_allocations",
    "operation_allocated_bytes",
    "operation_peak_net_bytes",
    "operation_net_heap_growth",
)
BINDING_COUNTERS = (
    "bound_nodes",
    "definition_lookups",
    "distinct_definitions",
    "distinct_specializations",
    "logical_occurrences",
    "memo_hits",
    "physical_factory_calls",
    "specialization_requests",
    "specializer_work",
)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def log(message: str) -> None:
    print(f"[{now()}] {message}", flush=True)


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_tsv(path: Path, values: Iterable[Any]) -> None:
    with path.open("a", encoding="utf-8", newline="") as stream:
        csv.writer(stream, delimiter="\t").writerow(values)


def parse_cmake_cache(path: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith(("#", "//")) or "=" not in line or ":" not in line.split("=", 1)[0]:
            continue
        key_and_type, value = line.split("=", 1)
        key, value_type = key_and_type.rsplit(":", 1)
        result[key] = {"type": value_type, "value": value}
    return result


def cache_value(cache: dict[str, dict[str, str]], key: str) -> str | None:
    entry = cache.get(key)
    return entry["value"] if entry is not None else None


def is_enabled(value: str | None) -> bool:
    return value is not None and value.strip().upper() not in {"", "0", "FALSE", "NO", "NONE", "OFF", "NOTFOUND"}


def source_provenance(cache: dict[str, dict[str, str]]) -> dict[str, Any]:
    source_text = cache_value(cache, "ClickHouse_SOURCE_DIR") or cache_value(cache, "CMAKE_HOME_DIRECTORY")
    result: dict[str, Any] = {"path": source_text}
    if not source_text:
        return result
    source = Path(source_text)
    try:
        completed = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if completed.returncode == 0:
            result["git_head"] = completed.stdout.strip()
        else:
            result["git_error"] = completed.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exception:
        result["git_error"] = str(exception)
    return result


def validate_release_build(build: Path, role: str) -> dict[str, Any]:
    build = build.resolve()
    cache_path = build / "CMakeCache.txt"
    if not build.is_dir() or not cache_path.is_file():
        raise CampaignError(f"{role} build has no CMakeCache.txt: {build}")
    cache = parse_cmake_cache(cache_path)
    build_type = cache_value(cache, "CMAKE_BUILD_TYPE")
    if build_type != "Release":
        raise CampaignError(f"{role} build is not Release: CMAKE_BUILD_TYPE={build_type!r}")
    benchmarks = cache_value(cache, "ENABLE_BENCHMARKS")
    if not is_enabled(benchmarks):
        raise CampaignError(f"{role} build has ENABLE_BENCHMARKS={benchmarks!r}")
    jemalloc = cache_value(cache, "ENABLE_JEMALLOC")
    if not is_enabled(jemalloc):
        raise CampaignError(f"{role} build has ENABLE_JEMALLOC={jemalloc!r}; allocator counters would be absent")
    sanitizer_options = {
        key: entry["value"]
        for key, entry in cache.items()
        if (key == "SANITIZE" or key.startswith("SANITIZE_")) and is_enabled(entry["value"])
    }
    if sanitizer_options:
        raise CampaignError(f"{role} Release build enables sanitizer options: {sanitizer_options}")

    selected_keys = (
        "CMAKE_BUILD_TYPE",
        "CMAKE_C_COMPILER",
        "CMAKE_C_FLAGS_RELEASE",
        "CMAKE_CXX_COMPILER",
        "CMAKE_CXX_COMPILER_VERSION",
        "CMAKE_CXX_FLAGS_RELEASE",
        "CMAKE_EXE_LINKER_FLAGS_RELEASE",
        "CMAKE_GENERATOR",
        "ENABLE_BENCHMARKS",
        "ENABLE_JEMALLOC",
        "ENABLE_THINLTO",
        "USE_MUSL",
        "USE_JEMALLOC",
    )
    return {
        "role": role,
        "path": str(build),
        "cmake_cache": str(cache_path),
        "cmake_cache_sha256": sha256_file(cache_path),
        "cmake": {key: cache_value(cache, key) for key in selected_keys if cache_value(cache, key) is not None},
        "source": source_provenance(cache),
    }


def schedule_pattern(phase: str, family: str, block: int) -> str:
    if phase not in {"aa", "ab"} or not 1 <= block <= BLOCKS:
        raise CampaignError(f"invalid paired schedule coordinate: {phase}/{block}")
    half_block = (block - 1) % (BLOCKS // 2) + 1
    base = "ABBA" if int(stable_hash(f"{SCHEDULE_SEED}:{phase}:{family}:pattern:{half_block}"), 16) % 2 else "BAAB"
    if block <= BLOCKS // 2:
        return base
    return "BAAB" if base == "ABBA" else "ABBA"


def family_order(phase: str, block: int) -> list[Family]:
    if phase == "candidate-only":
        return sorted(FAMILIES, key=lambda family: stable_hash(f"{SCHEDULE_SEED}:{phase}:{block}:{family.name}:order"))

    eligible = sorted(
        (family for family in FAMILIES if family.common_count),
        key=lambda family: stable_hash(f"{SCHEDULE_SEED}:{phase}:{family.name}:label"),
    )
    if len(eligible) != 3:
        raise CampaignError("paired family schedule requires exactly three common families")
    williams_orders = (
        (0, 1, 2),
        (1, 2, 0),
        (2, 0, 1),
        (2, 1, 0),
        (0, 2, 1),
        (1, 0, 2),
    )
    rotation = int(stable_hash(f"{SCHEDULE_SEED}:{phase}:williams-rotation"), 16) % len(williams_orders)
    order = williams_orders[((block - 1) % len(williams_orders) + rotation) % len(williams_orders)]
    return [eligible[index] for index in order]


def make_schedule() -> list[dict[str, Any]]:
    schedule: list[dict[str, Any]] = []
    for phase in ("aa", "ab"):
        for block in range(1, BLOCKS + 1):
            for family_index, family in enumerate(family_order(phase, block), 1):
                pattern = schedule_pattern(phase, family.name, block)
                for slot, side in enumerate(pattern, 1):
                    binary_role = "baseline" if phase == "aa" or side == "A" else "candidate"
                    schedule.append(
                        {
                            "phase": phase,
                            "block": block,
                            "family_order": family_index,
                            "family": family.name,
                            "pattern": pattern,
                            "slot": slot,
                            "side": side,
                            "binary_role": binary_role,
                            "run": 1,
                        }
                    )
    for run in range(1, CANDIDATE_PROCESSES_PER_FAMILY + 1):
        for family_index, family in enumerate(family_order("candidate-only", run), 1):
            schedule.append(
                {
                    "phase": "candidate-only",
                    "block": 1,
                    "family_order": family_index,
                    "family": family.name,
                    "pattern": "C",
                    "slot": 1,
                    "side": "C",
                    "binary_role": "candidate",
                    "run": run,
                }
            )
    return schedule


def make_plan(args: argparse.Namespace) -> dict[str, Any]:
    schedule = make_schedule()
    common_cases = sum(family.common_count for family in FAMILIES)
    candidate_cases = sum(family.candidate_count for family in FAMILIES)
    paired_processes = BLOCKS * 4 * sum(1 for family in FAMILIES if family.common_count)
    paired_rows = BLOCKS * 4 * REPETITIONS * common_cases
    candidate_processes = CANDIDATE_PROCESSES_PER_FAMILY * len(FAMILIES)
    candidate_rows = CANDIDATE_PROCESSES_PER_FAMILY * REPETITIONS * candidate_cases
    plan = {
        "schedule_seed": SCHEDULE_SEED,
        "baseline_build": str(args.baseline_build),
        "candidate_build": str(args.candidate_build),
        "artifacts": str(args.artifacts),
        "cpu": args.cpu,
        "controller_cpu": args.controller_cpu,
        "blocks": BLOCKS,
        "repetitions": REPETITIONS,
        "minimum_time_seconds": MINIMUM_TIME_SECONDS,
        "minimum_warmup_seconds": MINIMUM_WARMUP_SECONDS,
        "candidate_processes_per_family": CANDIDATE_PROCESSES_PER_FAMILY,
        "common_case_count": common_cases,
        "candidate_case_count": candidate_cases,
        "expected_processes": paired_processes * 2 + candidate_processes,
        "expected_raw_iteration_rows": paired_rows * 2 + candidate_rows,
        "expected_rows_by_phase": {"aa": paired_rows, "ab": paired_rows, "candidate-only": candidate_rows},
        "acceptance": {
            "aa_equivalence_interval": [AA_EQUIVALENCE_LOW, AA_EQUIVALENCE_HIGH],
            "ab_one_sided_ucb_limit": AB_ONE_SIDED_UCB_LIMIT,
            "memory_counters_are_acceptance_gate": False,
        },
        "quiet_gate": {
            "initial_window_seconds": args.initial_quiet_window_seconds,
            "window_seconds": args.quiet_window_seconds,
            "attempts": args.quiet_attempts,
            "minimum_cpu_idle_fraction": args.minimum_cpu_idle_fraction,
            "minimum_global_idle_fraction": args.minimum_global_idle_fraction,
            "maximum_load1": args.maximum_load1,
            "minimum_available_bytes": int(args.minimum_available_gib * 1024**3),
            "require_zero_swap_io": True,
        },
        "families": [
            {
                "name": family.name,
                "binary": str(family.relative_binary),
                "common_filter": family.common_filter,
                "candidate_filter": family.candidate_filter,
                "common_count": family.common_count,
                "candidate_count": family.candidate_count,
                "timeout_seconds": family.timeout_seconds,
                "operation_memory_counters": family.operation_memory_counters,
            }
            for family in FAMILIES
        ],
        "schedule_process_count": len(schedule),
    }
    validate_plan(plan, schedule)
    return plan


def validate_plan(plan: dict[str, Any], schedule: list[dict[str, Any]]) -> None:
    if BLOCKS != 12 or REPETITIONS != 9 or CANDIDATE_PROCESSES_PER_FAMILY != 3:
        raise CampaignError("campaign constants do not match the approved design")
    if plan["common_case_count"] != 70 or plan["candidate_case_count"] != 126:
        raise CampaignError("family filters/counts do not match the approved design")
    if plan["expected_processes"] != 300 or plan["expected_raw_iteration_rows"] != 63_882:
        raise CampaignError("campaign totals do not match the approved design")
    if len(schedule) != plan["expected_processes"]:
        raise CampaignError("schedule process count is inconsistent")
    schedule_keys = {
        (
            entry["phase"],
            entry["block"],
            entry["family"],
            entry["run"],
            entry["slot"],
            entry["side"],
            entry["binary_role"],
        )
        for entry in schedule
    }
    if len(schedule_keys) != len(schedule):
        raise CampaignError("schedule contains duplicate processes")
    for phase in ("aa", "ab"):
        for family in (item for item in FAMILIES if item.common_count):
            patterns = [schedule_pattern(phase, family.name, block) for block in range(1, BLOCKS + 1)]
            if patterns.count("ABBA") != BLOCKS // 2 or patterns.count("BAAB") != BLOCKS // 2:
                raise CampaignError(f"unbalanced schedule for {phase}/{family.name}")
        position_counts: dict[tuple[str, int], int] = {}
        joint_counts: dict[tuple[str, int, str], int] = {}
        for entry in schedule:
            if entry["phase"] != phase or entry["slot"] != 1:
                continue
            key = (entry["family"], entry["family_order"])
            position_counts[key] = position_counts.get(key, 0) + 1
            joint_key = (entry["family"], entry["family_order"], entry["pattern"])
            joint_counts[joint_key] = joint_counts.get(joint_key, 0) + 1
        for family in (item for item in FAMILIES if item.common_count):
            for position in range(1, 4):
                if position_counts.get((family.name, position)) != 4:
                    raise CampaignError(f"unbalanced family position for {phase}/{family.name}/{position}")
                for pattern in ("ABBA", "BAAB"):
                    if joint_counts.get((family.name, position, pattern)) != 2:
                        raise CampaignError(
                            f"unbalanced family position/pattern for {phase}/{family.name}/{position}/{pattern}"
                        )
    for family in FAMILIES:
        candidate_processes = [
            entry
            for entry in schedule
            if entry["phase"] == "candidate-only" and entry["family"] == family.name
        ]
        if len(candidate_processes) != CANDIDATE_PROCESSES_PER_FAMILY:
            raise CampaignError(f"wrong candidate-only process count for {family.name}")


class Campaign:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.artifacts = args.artifacts.resolve()
        self.builds = {
            "baseline": args.baseline_build.resolve(),
            "candidate": args.candidate_build.resolve(),
        }
        self.schedule = make_schedule()
        self.plan = make_plan(args)
        self.inventory: dict[str, dict[str, Any]] = {}
        self.processes: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.binary_hashes: dict[tuple[str, str], str] = {}
        self.started_at = now()
        self.initialized = False
        self.run_id = (
            f"resume-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}" if args.resume else "initial"
        )
        self.rerun_process_ids = set(args.rerun_process_id)
        known_process_ids = {self.process_id(entry) for entry in self.schedule}
        unknown_process_ids = self.rerun_process_ids - known_process_ids
        if unknown_process_ids:
            raise CampaignError(f"unknown --rerun-process-id values: {sorted(unknown_process_ids)}")
        self.reusable_processes: dict[str, dict[str, Any]] = {}

    @property
    def status_path(self) -> Path:
        return self.artifacts / "campaign.status"

    @property
    def provenance_path(self) -> Path:
        if self.run_id == "initial":
            return self.artifacts / "provenance.json"
        return self.artifacts / f"provenance.{self.run_id}.json"

    @property
    def inventory_path(self) -> Path:
        if self.run_id == "initial":
            return self.artifacts / "inventory" / "inventory.json"
        return self.artifacts / "inventory" / f"inventory.{self.run_id}.json"

    def analysis_path(self, phase: str) -> Path:
        name = "summary.json" if self.run_id == "initial" else f"summary.{self.run_id}.json"
        return self.artifacts / "analysis" / phase / name

    @property
    def summary_path(self) -> Path:
        if self.run_id == "initial":
            return self.artifacts / "campaign-summary.json"
        return self.artifacts / f"campaign-summary.{self.run_id}.json"

    @property
    def decision_path(self) -> Path:
        if self.run_id == "initial":
            return self.artifacts / "decision.json"
        return self.artifacts / f"decision.{self.run_id}.json"

    def append_status(self, message: str) -> None:
        with self.status_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{now()}\t{message}\n")

    def initialize(self) -> None:
        if self.args.resume:
            self.initialize_resume()
            return
        if self.artifacts.exists() and any(self.artifacts.iterdir()):
            raise CampaignError(f"artifact directory is not empty: {self.artifacts}")
        self.artifacts.mkdir(parents=True, exist_ok=True)
        for path in (
            self.artifacts / "inventory",
            self.artifacts / "raw" / "aa",
            self.artifacts / "raw" / "ab",
            self.artifacts / "raw" / "candidate-only",
            self.artifacts / "analysis" / "aa",
            self.artifacts / "analysis" / "ab",
            self.artifacts / "analysis" / "candidate-only",
            self.artifacts / "host-metadata",
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(f"{self.started_at}\tcontroller_start\n", encoding="utf-8")
        self.initialized = True
        (self.artifacts / "process-status.tsv").write_text(
            "at\tprocess_id\tphase\tblock\tfamily\trun\tslot\tside\tbinary\tstatus\texit_code\terror\n",
            encoding="utf-8",
        )
        (self.artifacts / "quiet-windows.tsv").write_text(
            "at\tscope\tattempt\telapsed_seconds\tcpu_idle_fraction\tcpu_iowait_steal_ticks\t"
            "global_idle_fraction\tload1\tswap_in_delta\tswap_out_delta\tavailable_memory_bytes\tpass\n",
            encoding="utf-8",
        )
        write_json(self.artifacts / "plan.json", self.plan)
        self.write_schedule()
        self.capture_host_metadata()
        (self.artifacts / "controller.sha256").write_text(
            f"{sha256_file(Path(__file__).resolve())}  {Path(__file__).name}\n", encoding="utf-8"
        )

    def initialize_resume(self) -> None:
        if not self.artifacts.is_dir():
            raise CampaignError(f"resume artifact directory does not exist: {self.artifacts}")
        plan_path = self.artifacts / "plan.json"
        if not plan_path.is_file():
            raise CampaignError(f"resume artifact directory has no plan.json: {self.artifacts}")
        try:
            persisted_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except Exception as exception:
            raise CampaignError(f"cannot read persisted plan: {exception}") from exception
        if persisted_plan != self.plan:
            raise CampaignError("resume arguments/configuration do not match the persisted plan")
        for required in (self.status_path, self.artifacts / "process-status.tsv", self.artifacts / "quiet-windows.tsv"):
            if not required.is_file():
                raise CampaignError(f"resume artifact is missing: {required}")
        self.initialized = True
        self.append_status(
            f"controller_resume_start run_id={self.run_id} requested={','.join(sorted(self.rerun_process_ids)) or '-'}"
        )
        (self.artifacts / "controller.outcome").write_text("running\n", encoding="utf-8")
        (self.artifacts / f"controller.{self.run_id}.sha256").write_text(
            f"{sha256_file(Path(__file__).resolve())}  {Path(__file__).name}\n", encoding="utf-8"
        )

    def write_schedule(self) -> None:
        with (self.artifacts / "schedule.tsv").open("w", encoding="utf-8", newline="") as stream:
            fieldnames = ("phase", "block", "family_order", "family", "pattern", "run", "slot", "side", "binary_role")
            writer = csv.DictWriter(stream, fieldnames=fieldnames, delimiter="\t")
            writer.writeheader()
            writer.writerows(self.schedule)

    def capture_host_metadata(self) -> None:
        destination = self.artifacts / "host-metadata"
        for source_text in ("/proc/cpuinfo", "/proc/meminfo", "/proc/loadavg", "/proc/cmdline"):
            source = Path(source_text)
            if source.exists():
                (destination / source.name).write_bytes(source.read_bytes())
        for label, command in (
            ("uname.txt", ["uname", "-a"]),
            ("lscpu.txt", ["lscpu"]),
            ("affinity.txt", ["taskset", "-pc", str(os.getpid())]),
        ):
            try:
                completed = subprocess.run(
                    command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False
                )
                (destination / label).write_text(completed.stdout, encoding="utf-8")
            except (OSError, subprocess.SubprocessError) as exception:
                (destination / label).write_text(f"unavailable: {exception}\n", encoding="utf-8")
        for name in ("scaling_governor", "scaling_cur_freq", "scaling_min_freq", "scaling_max_freq"):
            source = Path(f"/sys/devices/system/cpu/cpu{self.args.cpu}/cpufreq/{name}")
            if source.exists():
                (destination / name).write_bytes(source.read_bytes())
        write_json(
            destination / "controller.json",
            {
                "python": sys.version,
                "platform": platform.platform(),
                "argv": sys.argv,
                "pid": os.getpid(),
                "cpu": self.args.cpu,
                "controller_cpu": self.args.controller_cpu,
            },
        )

    def validate_builds(self) -> None:
        provenance: dict[str, Any] = {
            "captured_at": now(),
            "controller_sha256": sha256_file(Path(__file__).resolve()),
            "builds": {},
            "binaries": {},
        }
        errors = []
        for role, path in self.builds.items():
            try:
                provenance["builds"][role] = validate_release_build(path, role)
            except Exception as exception:
                errors.append(str(exception))
        if errors:
            write_json(self.provenance_path, provenance | {"errors": errors})
            raise CampaignError("; ".join(errors))

        compatibility_keys = (
            "CMAKE_BUILD_TYPE",
            "CMAKE_C_COMPILER",
            "CMAKE_C_FLAGS_RELEASE",
            "CMAKE_CXX_COMPILER",
            "CMAKE_CXX_COMPILER_VERSION",
            "CMAKE_CXX_FLAGS_RELEASE",
            "CMAKE_EXE_LINKER_FLAGS_RELEASE",
            "CMAKE_GENERATOR",
            "ENABLE_BENCHMARKS",
            "ENABLE_JEMALLOC",
            "ENABLE_THINLTO",
            "USE_MUSL",
            "USE_JEMALLOC",
        )
        baseline_cmake = provenance["builds"]["baseline"]["cmake"]
        candidate_cmake = provenance["builds"]["candidate"]["cmake"]
        configuration_differences = {
            key: {"baseline": baseline_cmake.get(key), "candidate": candidate_cmake.get(key)}
            for key in compatibility_keys
            if baseline_cmake.get(key) != candidate_cmake.get(key)
        }
        provenance["configuration_differences"] = configuration_differences
        if configuration_differences:
            write_json(self.provenance_path, provenance)
            raise CampaignError(f"baseline/candidate Release configurations differ: {configuration_differences}")

        for family in FAMILIES:
            roles = ("candidate",) if family.common_count == 0 else ("baseline", "candidate")
            for role in roles:
                binary = self.builds[role] / family.relative_binary
                if not binary.is_file() or not os.access(binary, os.X_OK):
                    errors.append(f"missing executable {role}/{family.name}: {binary}")
                    continue
                binary_sha256 = sha256_file(binary)
                self.binary_hashes[(role, family.name)] = binary_sha256
                provenance["binaries"][f"{role}/{family.name}"] = {
                    "path": str(binary),
                    "sha256": binary_sha256,
                    "size": binary.stat().st_size,
                    "mtime_ns": binary.stat().st_mtime_ns,
                }
        write_json(self.provenance_path, provenance | ({"errors": errors} if errors else {}))
        if errors:
            raise CampaignError("; ".join(errors))

    def validate_runtime(self) -> None:
        allowed_cpus = os.sched_getaffinity(0)
        required_cpus = {self.args.cpu, self.args.controller_cpu}
        if not required_cpus.issubset(allowed_cpus):
            raise CampaignError(
                f"requested CPUs {sorted(required_cpus)} are outside allowed affinity {sorted(allowed_cpus)}"
            )
        if shutil.which("taskset") is None:
            raise CampaignError("taskset is required to pin benchmark processes")
        time_binary = Path("/usr/bin/time")
        if not time_binary.is_file() or not os.access(time_binary, os.X_OK):
            raise CampaignError("/usr/bin/time is required for process resource counters")

    def list_cases(self, family: Family, role: str, case_set: str, benchmark_filter: str, expected_count: int) -> list[str]:
        binary = self.builds[role] / family.relative_binary
        suffix = "" if self.run_id == "initial" else f"-{self.run_id}"
        label = f"{role}-{family.name}-{case_set}{suffix}"
        command = [
            "taskset",
            "-c",
            str(self.args.cpu),
            str(binary),
            "--benchmark_list_tests=true",
            f"--benchmark_filter={benchmark_filter}",
        ]
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
            env=self.subprocess_environment(),
        )
        inventory_dir = self.artifacts / "inventory"
        (inventory_dir / f"{label}.stdout").write_text(completed.stdout, encoding="utf-8")
        (inventory_dir / f"{label}.stderr").write_text(completed.stderr, encoding="utf-8")
        names = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if completed.returncode != 0:
            raise CampaignError(f"inventory {label} exited with {completed.returncode}")
        if len(names) != expected_count or len(set(names)) != expected_count:
            raise CampaignError(f"inventory {label} has {len(names)} unique={len(set(names))}, expected {expected_count}")
        return sorted(names)

    def collect_inventory(self) -> None:
        for family in FAMILIES:
            entry: dict[str, Any] = {
                "status": "PASS",
                "common_status": "NOT_APPLICABLE" if family.common_filter is None else "PASS",
                "candidate_status": "PASS",
                "common": [],
                "candidate": [],
            }
            errors = []
            if family.common_filter is not None:
                try:
                    baseline_names = self.list_cases(
                        family, "baseline", "common", family.common_filter, family.common_count
                    )
                    candidate_names = self.list_cases(
                        family, "candidate", "common", family.common_filter, family.common_count
                    )
                    if baseline_names != candidate_names:
                        raise CampaignError(f"common inventory differs between builds for {family.name}")
                    entry["common"] = baseline_names
                except Exception as exception:
                    entry["common_status"] = "FAILED"
                    errors.append(str(exception))
                    self.failures.append(
                        {"kind": "inventory", "family": family.name, "case_set": "common", "error": str(exception)}
                    )
            try:
                entry["candidate"] = self.list_cases(
                    family, "candidate", "candidate", family.candidate_filter, family.candidate_count
                )
            except Exception as exception:
                entry["candidate_status"] = "FAILED"
                errors.append(str(exception))
                self.failures.append(
                    {"kind": "inventory", "family": family.name, "case_set": "candidate", "error": str(exception)}
                )
            if entry["common_status"] == "FAILED" or entry["candidate_status"] == "FAILED":
                entry["status"] = "FAILED"
            if errors:
                entry["errors"] = errors
            self.inventory[family.name] = entry
        write_json(self.inventory_path, self.inventory)

    def subprocess_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment["LC_ALL"] = "C"
        environment["TZ"] = "UTC"
        return environment

    @staticmethod
    def read_cpu_snapshot() -> tuple[dict[int, tuple[int, int, int]], tuple[int, int, int]]:
        cpu_values: dict[int, tuple[int, int, int]] = {}
        global_value = (0, 0, 0)
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if not fields or not fields[0].startswith("cpu"):
                continue
            if fields[0] != "cpu" and not fields[0][3:].isdigit():
                continue
            values = [int(item) for item in fields[1:9]]
            triple = (sum(values), values[3], values[4] + values[7])
            if fields[0] == "cpu":
                global_value = triple
            else:
                cpu_values[int(fields[0][3:])] = triple
        return cpu_values, global_value

    @staticmethod
    def read_swap_snapshot() -> tuple[int, int]:
        values: dict[str, int] = {}
        for line in Path("/proc/vmstat").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] in {"pswpin", "pswpout"}:
                values[fields[0]] = int(fields[1])
        return values.get("pswpin", 0), values.get("pswpout", 0)

    @staticmethod
    def available_memory_bytes() -> int:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
        raise CampaignError("MemAvailable is missing from /proc/meminfo")

    def sample_quiet(self, window_seconds: float) -> dict[str, Any]:
        cpu_before, global_before = self.read_cpu_snapshot()
        if self.args.cpu not in cpu_before:
            raise CampaignError(f"benchmark CPU {self.args.cpu} is not online")
        swap_before = self.read_swap_snapshot()
        started = time.monotonic()
        time.sleep(window_seconds)
        cpu_after, global_after = self.read_cpu_snapshot()
        swap_after = self.read_swap_snapshot()
        elapsed = time.monotonic() - started
        before = cpu_before[self.args.cpu]
        after = cpu_after[self.args.cpu]
        cpu_total = after[0] - before[0]
        global_total = global_after[0] - global_before[0]
        result = {
            "elapsed_seconds": elapsed,
            "cpu_idle_fraction": (after[1] - before[1]) / cpu_total if cpu_total else 0.0,
            "cpu_iowait_steal_ticks": after[2] - before[2],
            "global_idle_fraction": (global_after[1] - global_before[1]) / global_total if global_total else 0.0,
            "load1": float(Path("/proc/loadavg").read_text(encoding="utf-8").split()[0]),
            "swap_in_delta": swap_after[0] - swap_before[0],
            "swap_out_delta": swap_after[1] - swap_before[1],
            "available_memory_bytes": self.available_memory_bytes(),
        }
        result["pass"] = (
            result["cpu_idle_fraction"] >= self.args.minimum_cpu_idle_fraction
            and result["cpu_iowait_steal_ticks"] == 0
            and result["global_idle_fraction"] >= self.args.minimum_global_idle_fraction
            and result["load1"] <= self.args.maximum_load1
            and result["swap_in_delta"] == 0
            and result["swap_out_delta"] == 0
            and result["available_memory_bytes"] >= int(self.args.minimum_available_gib * 1024**3)
        )
        return result

    def wait_quiet(self, scope: str, window_seconds: float | None = None) -> bool:
        window = self.args.quiet_window_seconds if window_seconds is None else window_seconds
        last_error = "quiet gate exhausted"
        for attempt in range(1, self.args.quiet_attempts + 1):
            try:
                result = self.sample_quiet(window)
            except Exception as exception:
                last_error = f"quiet-gate sample failed: {exception}"
                append_tsv(
                    self.artifacts / "quiet-windows.tsv",
                    (now(), scope, attempt, "", "", "", "", "", "", "", "", 0),
                )
                if attempt != self.args.quiet_attempts:
                    time.sleep(5)
                continue
            append_tsv(
                self.artifacts / "quiet-windows.tsv",
                (
                    now(),
                    scope,
                    attempt,
                    f"{result['elapsed_seconds']:.6f}",
                    f"{result['cpu_idle_fraction']:.9f}",
                    result["cpu_iowait_steal_ticks"],
                    f"{result['global_idle_fraction']:.9f}",
                    f"{result['load1']:.3f}",
                    result["swap_in_delta"],
                    result["swap_out_delta"],
                    result["available_memory_bytes"],
                    int(result["pass"]),
                ),
            )
            if result["pass"]:
                return True
            last_error = "quiet gate exhausted"
            if attempt != self.args.quiet_attempts:
                time.sleep(5)
        self.failures.append({"kind": "quiet_gate", "scope": scope, "error": last_error})
        self.append_status(f"quiet_gate_failed scope={scope}")
        return False

    def record_process_status(self, process: dict[str, Any]) -> None:
        append_tsv(
            self.artifacts / "process-status.tsv",
            (
                now(),
                process["process_id"],
                process["phase"],
                process["block"],
                process["family"],
                process["run"],
                process["slot"],
                process["side"],
                process["binary_role"],
                process["status"],
                process.get("exit_code", ""),
                process.get("error", ""),
            ),
        )

    def skipped_process(self, scheduled: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
        process = dict(scheduled)
        process["process_id"] = self.process_id(scheduled)
        process.update({"status": status, "error": reason, "exit_code": None})
        self.record_process_status(process)
        self.processes.append(process)
        return process

    @staticmethod
    def process_id(scheduled: dict[str, Any]) -> str:
        phase = scheduled["phase"]
        family = scheduled["family"]
        if phase == "candidate-only":
            return f"candidate-only-{family}-run-{scheduled['run']:02d}"
        return (
            f"{phase}-block-{scheduled['block']:02d}-{family}-slot-{scheduled['slot']}"
            f"-{scheduled['side']}-{scheduled['binary_role']}"
        )

    def process_directory(self, scheduled: dict[str, Any]) -> Path:
        directory = self.artifacts / "raw" / scheduled["phase"]
        if scheduled["phase"] != "candidate-only":
            directory /= f"block-{scheduled['block']:02d}"
        return directory / scheduled["family"]

    @staticmethod
    def next_process_attempt(directory: Path, process_id: str) -> int:
        attempts = []
        for path in directory.glob(f"{process_id}.attempt-*.meta.json"):
            marker = path.name.removeprefix(f"{process_id}.attempt-").removesuffix(".meta.json")
            if marker.isdigit():
                attempts.append(int(marker))
        return max(attempts, default=0) + 1

    @staticmethod
    def run_command_with_timeout(command: list[str], stdout_path: Path, stderr_path: Path, timeout: int, env: dict[str, str]) -> int:
        with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
            process = subprocess.Popen(
                command,
                stdout=stdout_stream,
                stderr=stderr_stream,
                start_new_session=True,
                env=env,
            )
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=30)
                return 124

    @staticmethod
    def parse_time_metrics(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise CampaignError(f"missing /usr/bin/time output: {path}")
        raw: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if ": " in stripped:
                key, value = stripped.split(": ", 1)
                raw[key] = value
        maximum_rss = raw.get("Maximum resident set size (kbytes)")
        exit_status = raw.get("Exit status")
        if maximum_rss is None or exit_status is None:
            raise CampaignError(f"incomplete /usr/bin/time output: {path}")
        try:
            maximum_rss_kib = int(maximum_rss)
            parsed_exit_status = int(exit_status)
        except ValueError as exception:
            raise CampaignError(f"invalid /usr/bin/time counters: {path}") from exception
        if maximum_rss_kib <= 0 or parsed_exit_status != 0:
            raise CampaignError(
                f"invalid /usr/bin/time result: rss={maximum_rss_kib}, exit={parsed_exit_status}"
            )
        return {"maximum_rss_kib": maximum_rss_kib, "exit_status": parsed_exit_status, "raw": raw}

    @staticmethod
    def integer_counter(row: dict[str, Any], key: str) -> int:
        if key not in row:
            raise CampaignError(f"missing counter {key} in {row.get('name')}")
        value = row[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CampaignError(f"non-numeric counter {key} in {row.get('name')}: {value!r}")
        number = float(value)
        if not math.isfinite(number) or not number.is_integer() or abs(number) >= 2**53:
            raise CampaignError(f"invalid integer counter {key} in {row.get('name')}: {value!r}")
        return int(number)

    @staticmethod
    def catalog_counter_keys(name: str) -> tuple[str, ...]:
        if name.startswith("BM_TemplateCheckerChargedWork/"):
            return ("accepted_bytes", "canonical_bytes", "charged_work", "checked_definitions", "scratch_peak_bytes")
        catalog = ("definitions", "root_bytes", "shards")
        if name.startswith("BM_TypeCatalogBulkBuild/"):
            return catalog + ("built_root_bytes",)
        if name.startswith("BM_TypeCatalogLookup/"):
            return catalog + ("lookups_per_iteration",)
        if name.startswith("BM_TypeCatalogOneShardMutation/"):
            return catalog + ("copied_identity_shards", "copied_name_shards", "mutated_root_bytes")
        if name.startswith("BM_TypeResolverBuiltInFastPath"):
            return BINDING_COUNTERS + ("udt_state_created",)
        if name.startswith("BM_TypeResolverActivatedBinding/"):
            return BINDING_COUNTERS + ("D", "K", "S")
        if name.startswith(("BM_OrdinaryBoundResultAccess/", "BM_BoundResultRetentionAfterAuthorityRelease/")):
            return ("authority_released", "bound_nodes", "descriptors", "retained_definitions")
        raise CampaignError(f"unknown catalog benchmark name: {name}")

    def structural_counter_keys(self, family: Family, name: str, row: dict[str, Any]) -> tuple[str, ...]:
        if family.name == "parser":
            return PARSER_COUNTERS
        if family.name == "analysis":
            return ANALYSIS_COUNTERS
        if family.name == "token_masking":
            return TOKEN_MASKING_COUNTERS + tuple(key for key in OPTIONAL_TOKEN_MASKING_COUNTERS if key in row)
        return self.catalog_counter_keys(name)

    def validate_process_json(self, path: Path, family: Family, expected_names: list[str]) -> dict[str, Any]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exception:
            raise CampaignError(f"invalid benchmark JSON {path}: {exception}") from exception
        rows = document.get("benchmarks")
        if not isinstance(rows, list):
            raise CampaignError(f"benchmark JSON has no benchmark rows: {path}")
        for row in rows:
            if not isinstance(row, dict):
                raise CampaignError(f"non-object benchmark row in {path}")
            if row.get("error_occurred", False) or row.get("skipped", False) or row.get("error_message"):
                raise CampaignError(f"failed or skipped benchmark row {row.get('name')} in {path}")
        iteration_rows = [row for row in rows if row.get("run_type", "iteration") == "iteration"]
        by_name: dict[str, list[dict[str, Any]]] = {}
        for row in iteration_rows:
            name = row.get("name")
            if not isinstance(name, str) or row.get("run_name", name) != name:
                raise CampaignError(f"non-canonical benchmark name in {path}: {name!r}")
            by_name.setdefault(name, []).append(row)
        if sorted(by_name) != expected_names:
            raise CampaignError(
                f"benchmark case set mismatch in {path}: got {len(by_name)}, expected {len(expected_names)}"
            )

        medians: dict[str, float] = {}
        samples: dict[str, list[float]] = {}
        signatures: dict[str, dict[str, int]] = {}
        memory: dict[str, list[dict[str, int]]] = {}
        for name in expected_names:
            case_rows = by_name[name]
            if len(case_rows) != REPETITIONS:
                raise CampaignError(f"{name} has {len(case_rows)} iterations, expected {REPETITIONS}")
            indices = sorted(row.get("repetition_index") for row in case_rows)
            if indices != list(range(REPETITIONS)):
                raise CampaignError(f"{name} has wrong repetition indices: {indices}")
            cpu_values: list[float] = []
            case_signatures: list[dict[str, int]] = []
            case_memory: list[dict[str, int]] = []
            for row in case_rows:
                if row.get("repetitions") != REPETITIONS or row.get("threads") != 1:
                    raise CampaignError(f"{name} has wrong repetition/thread metadata")
                iterations = row.get("iterations")
                if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations <= 0:
                    raise CampaignError(f"{name} has invalid iteration count: {iterations!r}")
                unit = row.get("time_unit")
                if unit not in TIME_SCALE_NS:
                    raise CampaignError(f"{name} has unknown time unit: {unit!r}")
                cpu_time = row.get("cpu_time")
                real_time = row.get("real_time")
                if not isinstance(cpu_time, (int, float)) or not isinstance(real_time, (int, float)):
                    raise CampaignError(f"{name} has no numeric CPU/real time")
                cpu_ns = float(cpu_time) * TIME_SCALE_NS[unit]
                real_ns = float(real_time) * TIME_SCALE_NS[unit]
                if not math.isfinite(cpu_ns) or not math.isfinite(real_ns) or cpu_ns <= 0 or real_ns <= 0:
                    raise CampaignError(f"{name} has non-positive or non-finite time")
                keys = self.structural_counter_keys(family, name, row)
                signature = {key: self.integer_counter(row, key) for key in keys}
                if family.name == "token_masking":
                    operations = signature["operations_per_iteration"]
                    if operations != 8:
                        raise CampaignError(f"{name} has unexpected operations_per_iteration={operations}")
                    cpu_ns /= operations
                cpu_values.append(cpu_ns)
                case_signatures.append(signature)
                if family.operation_memory_counters:
                    measured = {key: self.integer_counter(row, key) for key in OPERATION_MEMORY_COUNTERS}
                    if any(measured[key] < 0 for key in OPERATION_MEMORY_COUNTERS[:3]):
                        raise CampaignError(f"{name} has negative allocator counters: {measured}")
                    case_memory.append(measured)
            if any(value != case_signatures[0] for value in case_signatures[1:]):
                raise CampaignError(f"{name} structural counters changed between repetitions")
            medians[name] = statistics.median(cpu_values)
            samples[name] = cpu_values
            signatures[name] = case_signatures[0]
            memory[name] = case_memory
        return {
            "medians": medians,
            "cpu_samples": samples,
            "signatures": signatures,
            "operation_memory": memory,
            "iteration_rows": len(iteration_rows),
        }

    def run_process(self, scheduled: dict[str, Any]) -> dict[str, Any]:
        process = dict(scheduled)
        process_id = self.process_id(scheduled)
        process["process_id"] = process_id
        family = next(item for item in FAMILIES if item.name == scheduled["family"])
        role = scheduled["binary_role"]
        case_set = "candidate" if scheduled["phase"] == "candidate-only" else "common"
        inventory = self.inventory[family.name]
        expected_names = inventory[case_set]
        benchmark_filter = family.candidate_filter if case_set == "candidate" else family.common_filter
        if benchmark_filter is None:
            return self.skipped_process(scheduled, "SKIPPED_INVENTORY", "missing benchmark filter")

        directory = self.process_directory(scheduled)
        directory.mkdir(parents=True, exist_ok=True)
        attempt = self.next_process_attempt(directory, process_id)
        prefix = directory / f"{process_id}.attempt-{attempt:02d}"
        partial_json = Path(f"{prefix}.json.partial")
        final_json = Path(f"{prefix}.json")
        stdout_path = Path(f"{prefix}.stdout")
        stderr_path = Path(f"{prefix}.stderr")
        time_path = Path(f"{prefix}.time.txt")
        meta_path = Path(f"{prefix}.meta.json")
        binary = self.builds[role] / family.relative_binary
        command = [
            "/usr/bin/time",
            "-v",
            "-o",
            str(time_path),
            "taskset",
            "-c",
            str(self.args.cpu),
            str(binary),
            f"--benchmark_filter={benchmark_filter}",
            f"--benchmark_repetitions={REPETITIONS}",
            f"--benchmark_min_time={MINIMUM_TIME_SECONDS}",
            f"--benchmark_min_warmup_time={MINIMUM_WARMUP_SECONDS}",
            "--benchmark_enable_random_interleaving=true",
            "--benchmark_time_unit=ns",
            "--benchmark_color=false",
            "--benchmark_report_aggregates_only=false",
            f"--benchmark_out={partial_json}",
            "--benchmark_out_format=json",
        ]
        process.update(
            {
                "status": "RUNNING",
                "attempt": attempt,
                "binary": str(binary),
                "binary_sha256": self.binary_hashes[(role, family.name)],
                "filter": benchmark_filter,
                "expected_names_sha256": stable_hash("\n".join(expected_names) + "\n"),
                "plan_sha256": stable_hash(json.dumps(self.plan, sort_keys=True, separators=(",", ":"))),
                "command": command,
                "started_at": now(),
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "time": str(time_path),
                "json_partial": str(partial_json),
                "meta": str(meta_path),
            }
        )
        write_json(meta_path, process)
        self.record_process_status(process)
        started = time.monotonic()
        return_code = self.run_command_with_timeout(
            command, stdout_path, stderr_path, family.timeout_seconds, self.subprocess_environment()
        )
        process["exit_code"] = return_code
        process["finished_at"] = now()
        process["elapsed_seconds"] = time.monotonic() - started
        try:
            if return_code != 0:
                raise CampaignError(f"benchmark process exited with {return_code}")
            if not partial_json.is_file():
                raise CampaignError("benchmark process produced no JSON")
            validation = self.validate_process_json(partial_json, family, expected_names)
            time_metrics = self.parse_time_metrics(time_path)
            partial_json.replace(final_json)
            process.update(validation)
            process["time_metrics"] = time_metrics
            process["json"] = str(final_json)
            process["json_sha256"] = sha256_file(final_json)
            process["status"] = "PASS"
        except Exception as exception:
            process["status"] = "FAILED"
            process["error"] = str(exception)
            self.failures.append(
                {"kind": "benchmark_process", "process_id": process_id, "family": family.name, "error": str(exception)}
            )
        write_json(meta_path, self.process_metadata(process))
        self.record_process_status(process)
        self.processes.append(process)
        return process

    def safe_run_process(self, scheduled: dict[str, Any]) -> dict[str, Any]:
        """Run one independent process without aborting the remaining schedule."""
        process_id = self.process_id(scheduled)
        try:
            return self.run_process(scheduled)
        except Exception as exception:
            for recorded in self.processes:
                if recorded["process_id"] == process_id:
                    return recorded
            process = dict(scheduled)
            process.update(
                {
                    "process_id": process_id,
                    "status": "FAILED",
                    "exit_code": None,
                    "error": f"controller could not run process: {exception}",
                    "finished_at": now(),
                }
            )
            self.failures.append(
                {
                    "kind": "benchmark_process",
                    "process_id": process_id,
                    "family": scheduled["family"],
                    "error": process["error"],
                }
            )
            self.record_process_status(process)
            self.processes.append(process)
            return process

    @staticmethod
    def process_metadata(process: dict[str, Any]) -> dict[str, Any]:
        omitted = {"medians", "cpu_samples", "signatures", "operation_memory"}
        return {key: value for key, value in process.items() if key not in omitted}

    def load_reusable_processes(self) -> None:
        if not self.args.resume:
            return
        plan_sha256 = stable_hash(json.dumps(self.plan, sort_keys=True, separators=(",", ":")))
        for scheduled in self.schedule:
            process_id = self.process_id(scheduled)
            if process_id in self.rerun_process_ids:
                continue
            family = next(item for item in FAMILIES if item.name == scheduled["family"])
            case_set = "candidate" if scheduled["phase"] == "candidate-only" else "common"
            required_inventory_status = "candidate_status" if case_set == "candidate" else "common_status"
            if self.inventory[family.name][required_inventory_status] != "PASS":
                continue
            expected_names = self.inventory[family.name][case_set]
            benchmark_filter = family.candidate_filter if case_set == "candidate" else family.common_filter
            role = scheduled["binary_role"]
            expected_binary_hash = self.binary_hashes[(role, family.name)]
            expected_names_hash = stable_hash("\n".join(expected_names) + "\n")
            directory = self.process_directory(scheduled)
            def attempt_number(path: Path) -> int:
                marker = path.name.removeprefix(f"{process_id}.attempt-").removesuffix(".meta.json")
                return int(marker) if marker.isdigit() else -1

            metadata_paths = sorted(
                directory.glob(f"{process_id}.attempt-*.meta.json"), key=attempt_number, reverse=True
            )
            for metadata_path in metadata_paths:
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if metadata.get("status") != "PASS":
                        continue
                    for key in ("phase", "block", "family", "run", "slot", "side", "binary_role"):
                        if metadata.get(key) != scheduled[key]:
                            raise CampaignError(f"persisted process coordinate mismatch: {process_id}/{key}")
                    if metadata.get("process_id") != process_id:
                        raise CampaignError(f"persisted process id mismatch: {process_id}")
                    if metadata.get("binary_sha256") != expected_binary_hash:
                        continue
                    if metadata.get("filter") != benchmark_filter:
                        continue
                    if metadata.get("expected_names_sha256") != expected_names_hash:
                        continue
                    if metadata.get("plan_sha256") != plan_sha256:
                        continue
                    json_path = Path(metadata["json"])
                    time_path = Path(metadata["time"])
                    if not json_path.is_file() or sha256_file(json_path) != metadata.get("json_sha256"):
                        raise CampaignError(f"persisted JSON hash mismatch: {json_path}")
                    validation = self.validate_process_json(json_path, family, expected_names)
                    time_metrics = self.parse_time_metrics(time_path)
                    process = dict(scheduled)
                    process.update(metadata)
                    process.update(validation)
                    process["time_metrics"] = time_metrics
                    process["status"] = "PASS"
                    process["reused"] = True
                    process["reused_at"] = now()
                    self.reusable_processes[process_id] = process
                    break
                except Exception as exception:
                    self.append_status(
                        f"resume_rejected process_id={process_id} meta={metadata_path.name} reason={exception}"
                    )

    def reuse_process(self, scheduled: dict[str, Any]) -> bool:
        process_id = self.process_id(scheduled)
        process = self.reusable_processes.get(process_id)
        if process is None:
            return False
        self.processes.append(process)
        status = dict(process)
        status["status"] = "REUSED"
        self.record_process_status(status)
        return True

    def run_paired_phase(self, phase: str) -> None:
        self.append_status(f"phase={phase} start")
        for block in range(1, BLOCKS + 1):
            block_schedule = [
                item for item in self.schedule if item["phase"] == phase and item["block"] == block
            ]
            invalid_families = {
                item["family"]
                for item in block_schedule
                if self.inventory[item["family"]]["common_status"] != "PASS"
            }
            pending = [
                item
                for item in block_schedule
                if self.process_id(item) not in self.reusable_processes and item["family"] not in invalid_families
            ]
            quiet = True
            if pending:
                quiet = self.wait_quiet(f"{phase}-block-{block:02d}")
            log(f"{phase}: block {block}/{BLOCKS}")
            for item in block_schedule:
                if self.reuse_process(item):
                    continue
                if item["family"] in invalid_families:
                    self.skipped_process(item, "SKIPPED_INVENTORY", "family inventory failed")
                elif not quiet:
                    self.skipped_process(item, "SKIPPED_QUIET", "quiet gate failed")
                else:
                    self.safe_run_process(item)
            self.append_status(f"phase={phase} block={block} complete")
        self.append_status(f"phase={phase} complete")

    def run_candidate_phase(self) -> None:
        phase = "candidate-only"
        self.append_status(f"phase={phase} start")
        for item in (entry for entry in self.schedule if entry["phase"] == phase):
            if self.reuse_process(item):
                continue
            if self.inventory[item["family"]]["candidate_status"] != "PASS":
                self.skipped_process(item, "SKIPPED_INVENTORY", "family inventory failed")
                continue
            scope = f"candidate-only-{item['family']}-run-{item['run']:02d}"
            if not self.wait_quiet(scope):
                self.skipped_process(item, "SKIPPED_QUIET", "quiet gate failed")
                continue
            log(f"candidate-only: {item['family']} process {item['run']}/{CANDIDATE_PROCESSES_PER_FAMILY}")
            self.safe_run_process(item)
        self.append_status(f"phase={phase} complete")

    def determinism_errors(self) -> list[str]:
        errors: list[str] = []
        per_binary: dict[tuple[str, str, str], dict[str, int]] = {}
        cross_build: dict[tuple[str, str], dict[str, dict[str, int]]] = {}
        for process in (item for item in self.processes if item["status"] == "PASS"):
            for name, signature in process["signatures"].items():
                key = (process["binary_role"], process["family"], name)
                if key in per_binary and per_binary[key] != signature:
                    errors.append(f"structural counters changed across processes: {key}")
                per_binary[key] = signature
                if process["phase"] == "ab":
                    cross_build.setdefault((process["family"], name), {})[process["binary_role"]] = signature
        for (family, name), values in cross_build.items():
            if set(values) == {"baseline", "candidate"} and values["baseline"] != values["candidate"]:
                errors.append(f"cross-build structural counters differ: {family}/{name}")
        return sorted(set(errors))

    @staticmethod
    def type7_quantile(values: list[float], probability: float) -> float:
        ordered = sorted(values)
        if not ordered:
            raise CampaignError("quantile of an empty sample")
        position = (len(ordered) - 1) * probability
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        fraction = position - lower
        return ordered[lower] * (1 - fraction) + ordered[upper] * fraction

    def numeric_summary(self, values: list[float]) -> dict[str, float | int]:
        if not values:
            raise CampaignError("summary of an empty sample")
        return {
            "samples": len(values),
            "median": statistics.median(values),
            "p05": self.type7_quantile(values, 0.05),
            "p95": self.type7_quantile(values, 0.95),
            "minimum": min(values),
            "maximum": max(values),
        }

    @staticmethod
    def diagnostic_ratio(numerator: float, denominator: float) -> float | None:
        if denominator == 0:
            return 1.0 if numerator == 0 else None
        return numerator / denominator

    def analyze_paired_phase(self, phase: str) -> dict[str, Any]:
        passed = [item for item in self.processes if item["phase"] == phase and item["status"] == "PASS"]
        cells: dict[tuple[int, str, str], dict[str, list[float]]] = {}
        allocator_cells: dict[tuple[int, str, str], dict[str, dict[str, list[float]]]] = {}
        process_rss: dict[tuple[str, str], list[float]] = {}
        for process in passed:
            process_rss.setdefault((process["family"], process["side"]), []).append(
                float(process["time_metrics"]["maximum_rss_kib"])
            )
            for name, median in process["medians"].items():
                cell = cells.setdefault((process["block"], process["family"], name), {"A": [], "B": []})
                cell[process["side"]].append(median)
                memory_rows = process["operation_memory"].get(name, [])
                if memory_rows:
                    memory_cell = allocator_cells.setdefault(
                        (process["block"], process["family"], name), {"A": {}, "B": {}}
                    )
                    for counter in OPERATION_MEMORY_COUNTERS:
                        memory_cell[process["side"]].setdefault(counter, []).extend(
                            float(row[counter]) for row in memory_rows
                        )

        rows = []
        incomplete = []
        for family in (item for item in FAMILIES if item.common_count):
            expected_names = self.inventory.get(family.name, {}).get("common", [])
            for name in expected_names:
                log_differences = []
                ratios = []
                missing_blocks = []
                for block in range(1, BLOCKS + 1):
                    cell = cells.get((block, family.name, name), {"A": [], "B": []})
                    if len(cell["A"]) != 2 or len(cell["B"]) != 2:
                        missing_blocks.append(block)
                        continue
                    difference = (
                        sum(math.log(value) for value in cell["B"])
                        - sum(math.log(value) for value in cell["A"])
                    ) / 2
                    log_differences.append(difference)
                    ratios.append(math.exp(difference))
                if missing_blocks:
                    incomplete.append({"family": family.name, "name": name, "missing_blocks": missing_blocks})
                    continue
                mean = statistics.fmean(log_differences)
                standard_deviation = statistics.stdev(log_differences)
                standard_error = standard_deviation / math.sqrt(BLOCKS)
                row: dict[str, Any] = {
                    "family": family.name,
                    "name": name,
                    "ratio": math.exp(mean),
                    "sample_log_sd": standard_deviation,
                    "standard_error": standard_error,
                    "block_ratio_median": statistics.median(ratios),
                    "block_ratio_mad": statistics.median(abs(value - statistics.median(ratios)) for value in ratios),
                    "block_ratio_min": min(ratios),
                    "block_ratio_max": max(ratios),
                }
                if phase == "aa":
                    row["ci_lower"] = math.exp(mean - TWO_SIDED_T95_DF11 * standard_error)
                    row["ci_upper"] = math.exp(mean + TWO_SIDED_T95_DF11 * standard_error)
                    row["pass"] = (
                        row["ci_lower"] >= AA_EQUIVALENCE_LOW and row["ci_upper"] <= AA_EQUIVALENCE_HIGH
                    )
                else:
                    row["one_sided_ucb"] = math.exp(mean + ONE_SIDED_T95_DF11 * standard_error)
                    row["pass"] = row["one_sided_ucb"] < AB_ONE_SIDED_UCB_LIMIT

                allocator: dict[str, Any] = {}
                for counter in OPERATION_MEMORY_COUNTERS:
                    side_a: list[float] = []
                    side_b: list[float] = []
                    block_deltas: list[float] = []
                    block_ratios: list[float] = []
                    for block in range(1, BLOCKS + 1):
                        memory_cell = allocator_cells.get((block, family.name, name))
                        if memory_cell is None:
                            continue
                        values_a = memory_cell["A"].get(counter, [])
                        values_b = memory_cell["B"].get(counter, [])
                        if not values_a or not values_b:
                            continue
                        side_a.extend(values_a)
                        side_b.extend(values_b)
                        median_a = statistics.median(values_a)
                        median_b = statistics.median(values_b)
                        block_deltas.append(median_b - median_a)
                        ratio = self.diagnostic_ratio(median_b, median_a)
                        if ratio is not None:
                            block_ratios.append(ratio)
                    if side_a and side_b:
                        median_a = statistics.median(side_a)
                        median_b = statistics.median(side_b)
                        allocator[counter] = {
                            "side_a": self.numeric_summary(side_a),
                            "side_b": self.numeric_summary(side_b),
                            "median_delta_b_minus_a": median_b - median_a,
                            "median_ratio_b_over_a": self.diagnostic_ratio(median_b, median_a),
                            "block_delta_median": statistics.median(block_deltas),
                            "block_ratio_median": statistics.median(block_ratios) if block_ratios else None,
                            "directional": counter == "operation_peak_net_bytes",
                        }
                if allocator:
                    row["allocator"] = allocator
                rows.append(row)
        complete = not incomplete and len(rows) == 70
        summary = {
            "phase": phase,
            "complete": complete,
            "pass": complete and all(row["pass"] for row in rows),
            "case_count": len(rows),
            "failed_case_count": sum(not row["pass"] for row in rows),
            "incomplete": incomplete,
            "allocator_counters_acceptance_gate": False,
            "process_rss_kib": [
                {"family": family, "side": side, **self.numeric_summary(values)}
                for (family, side), values in sorted(process_rss.items())
            ],
            "rows": rows,
        }
        write_json(self.analysis_path(phase), summary)
        return summary

    def analyze_candidate(self) -> dict[str, Any]:
        passed = [item for item in self.processes if item["phase"] == "candidate-only" and item["status"] == "PASS"]
        samples: dict[tuple[str, str], list[float]] = {}
        memory: dict[tuple[str, str], list[dict[str, int]]] = {}
        process_counts: dict[str, int] = {}
        process_rss: dict[str, list[float]] = {}
        for process in passed:
            process_counts[process["family"]] = process_counts.get(process["family"], 0) + 1
            process_rss.setdefault(process["family"], []).append(float(process["time_metrics"]["maximum_rss_kib"]))
            for name, values in process["cpu_samples"].items():
                key = (process["family"], name)
                samples.setdefault(key, []).extend(values)
                memory.setdefault(key, []).extend(process["operation_memory"][name])
        rows = []
        expected_samples = CANDIDATE_PROCESSES_PER_FAMILY * REPETITIONS
        incomplete = []
        for family in FAMILIES:
            names = self.inventory.get(family.name, {}).get("candidate", [])
            for name in names:
                key = (family.name, name)
                values = samples.get(key, [])
                if len(values) != expected_samples:
                    incomplete.append(
                        {"family": family.name, "name": name, "sample_count": len(values), "expected": expected_samples}
                    )
                    continue
                median = statistics.median(values)
                row: dict[str, Any] = {
                    "family": family.name,
                    "name": name,
                    "sample_count": len(values),
                    "cpu_ns_per_operation_median": median,
                    "cpu_ns_per_operation_mad": statistics.median(abs(value - median) for value in values),
                    "cpu_ns_per_operation_p05": self.type7_quantile(values, 0.05),
                    "cpu_ns_per_operation_p95": self.type7_quantile(values, 0.95),
                }
                memories = memory[key]
                if memories:
                    row.update(
                        {
                            "operation_allocations_median": statistics.median(
                                item["operation_allocations"] for item in memories
                            ),
                            "operation_allocated_bytes_median": statistics.median(
                                item["operation_allocated_bytes"] for item in memories
                            ),
                            "operation_net_heap_growth_median": statistics.median(
                                item["operation_net_heap_growth"] for item in memories
                            ),
                            "operation_peak_net_bytes_median_directional": statistics.median(
                                item["operation_peak_net_bytes"] for item in memories
                            ),
                        }
                    )
                rows.append(row)
        complete = (
            not incomplete
            and len(rows) == 126
            and all(process_counts.get(family.name) == CANDIDATE_PROCESSES_PER_FAMILY for family in FAMILIES)
        )
        summary = {
            "complete": complete,
            "acceptance_gate": False,
            "processes_per_family": process_counts,
            "case_count": len(rows),
            "incomplete": incomplete,
            "process_rss_kib": [
                {"family": family, **self.numeric_summary(values)} for family, values in sorted(process_rss.items())
            ],
            "rows": rows,
        }
        write_json(self.analysis_path("candidate-only"), summary)
        return summary

    def final_summary(self) -> tuple[dict[str, Any], int]:
        determinism = self.determinism_errors()
        for error in determinism:
            self.failures.append({"kind": "determinism", "error": error})
        aa = self.analyze_paired_phase("aa")
        ab = self.analyze_paired_phase("ab")
        candidate = self.analyze_candidate()
        statuses: dict[str, int] = {}
        for process in self.processes:
            statuses[process["status"]] = statuses.get(process["status"], 0) + 1

        hard_failures = [
            failure for failure in self.failures if failure["kind"] in {"inventory", "benchmark_process", "determinism"}
        ]
        quiet_failures = [failure for failure in self.failures if failure["kind"] == "quiet_gate"]
        if hard_failures:
            verdict, exit_code = "INVALID", 1
        elif quiet_failures or not aa["complete"] or not ab["complete"] or not candidate["complete"]:
            verdict, exit_code = "INCONCLUSIVE", 3
        elif not aa["pass"]:
            verdict, exit_code = "INCONCLUSIVE", 3
        elif not ab["pass"]:
            verdict, exit_code = "FAIL", 2
        else:
            verdict, exit_code = "PASS", 0
        summary = {
            "verdict": verdict,
            "started_at": self.started_at,
            "finished_at": now(),
            "exit_code": exit_code,
            "processes": {
                "scheduled": len(self.schedule),
                "recorded": len(self.processes),
                "by_status": statuses,
            },
            "inventory": {
                family: {
                    "status": entry["status"],
                    "common_status": entry["common_status"],
                    "candidate_status": entry["candidate_status"],
                }
                for family, entry in self.inventory.items()
            },
            "aa": {key: value for key, value in aa.items() if key != "rows"},
            "ab": {key: value for key, value in ab.items() if key != "rows"},
            "candidate_only": {key: value for key, value in candidate.items() if key != "rows"},
            "determinism_errors": determinism,
            "failures": self.failures,
            "artifacts": {
                "plan": str(self.artifacts / "plan.json"),
                "provenance": str(self.provenance_path),
                "inventory": str(self.inventory_path),
                "aa": str(self.analysis_path("aa")),
                "ab": str(self.analysis_path("ab")),
                "candidate_only": str(self.analysis_path("candidate-only")),
            },
        }
        write_json(self.summary_path, summary)
        write_json(self.decision_path, summary)
        write_json(
            self.artifacts / "latest-summary.json",
            {"run_id": self.run_id, "summary": str(self.summary_path), "decision": str(self.decision_path)},
        )
        (self.artifacts / "controller.outcome").write_text(verdict.lower() + "\n", encoding="utf-8")
        self.append_status(f"controller_complete verdict={verdict} exit_code={exit_code}")
        return summary, exit_code

    def run(self) -> int:
        self.initialize()
        self.validate_runtime()
        os.sched_setaffinity(0, {self.args.controller_cpu})
        self.validate_builds()
        self.collect_inventory()
        self.load_reusable_processes()
        if len(self.reusable_processes) != len(self.schedule):
            self.wait_quiet("initial", self.args.initial_quiet_window_seconds)
        self.run_paired_phase("aa")
        self.run_paired_phase("ab")
        self.run_candidate_phase()
        summary, exit_code = self.final_summary()
        log(f"campaign finished: {summary['verdict']}")
        return exit_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-build", type=Path, required=True)
    parser.add_argument("--candidate-build", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--cpu", type=int, default=30, help="CPU used by every benchmark process")
    parser.add_argument("--controller-cpu", type=int, default=29, help="CPU used by this controller")
    parser.add_argument("--initial-quiet-window-seconds", type=float, default=30.0)
    parser.add_argument("--quiet-window-seconds", type=float, default=5.0)
    parser.add_argument(
        "--quiet-attempts",
        type=int,
        default=180,
        help="maximum settling attempts; the default tolerates post-build load-average decay",
    )
    parser.add_argument("--minimum-cpu-idle-fraction", type=float, default=0.99)
    parser.add_argument("--minimum-global-idle-fraction", type=float, default=0.95)
    parser.add_argument("--maximum-load1", type=float, default=2.0)
    parser.add_argument("--minimum-available-gib", type=float, default=32.0)
    parser.add_argument("--resume", action="store_true", help="resume from validated PASS process artifacts")
    parser.add_argument(
        "--rerun-process-id",
        action="append",
        default=[],
        help="force a scheduled process to run again; repeat for multiple process IDs",
    )
    parser.add_argument("--plan-only", action="store_true", help="validate and print the static plan without touching builds")
    args = parser.parse_args(argv)
    if args.cpu < 0 or args.controller_cpu < 0 or args.cpu == args.controller_cpu:
        parser.error("--cpu and --controller-cpu must be distinct non-negative CPU numbers")
    if args.quiet_attempts <= 0 or args.initial_quiet_window_seconds <= 0 or args.quiet_window_seconds <= 0:
        parser.error("quiet-gate attempts and windows must be positive")
    if not 0 <= args.minimum_cpu_idle_fraction <= 1 or not 0 <= args.minimum_global_idle_fraction <= 1:
        parser.error("quiet-gate idle fractions must be between zero and one")
    if args.maximum_load1 < 0 or args.minimum_available_gib < 0:
        parser.error("quiet-gate load and memory limits must be non-negative")
    if args.rerun_process_id and not args.resume:
        parser.error("--rerun-process-id requires --resume")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    plan = make_plan(args)
    if args.plan_only:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    try:
        campaign = Campaign(args)
        return campaign.run()
    except Exception as exception:
        log(f"campaign setup failed: {exception}")
        traceback.print_exc()
        if "campaign" in locals() and campaign.initialized:
            failure = {
                "verdict": "INVALID",
                "reason": str(exception),
                "started_at": campaign.started_at,
                "finished_at": now(),
                "exit_code": 1,
            }
            try:
                write_json(campaign.summary_path, failure)
                write_json(campaign.decision_path, failure)
                write_json(
                    campaign.artifacts / "latest-summary.json",
                    {
                        "run_id": campaign.run_id,
                        "summary": str(campaign.summary_path),
                        "decision": str(campaign.decision_path),
                    },
                )
                (campaign.artifacts / "controller.outcome").write_text("invalid\n", encoding="utf-8")
                if campaign.status_path.exists():
                    campaign.append_status(f"controller_failed reason={exception}")
            except OSError:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
