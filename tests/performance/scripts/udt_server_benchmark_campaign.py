#!/usr/bin/env python3
"""Release server benchmark campaign for Atomic user-defined types (UDTs).

The controller owns a private ClickHouse server and a fresh data root.  It
executes the deliberately reduced 36-scenario UDT server pilot serially, keeps
raw query artifacts, and never stops the campaign because one independent
scenario failed.  Large points which cannot be executed safely, and product
limits which intentionally reject a requested scale, are explicit results.

This is not a general-purpose performance-test runner.  Its matrix and safety
rules are part of the UDT benchmark contract; use ``--plan-only`` to inspect
that contract without starting a server.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from typing import Any, Callable, Iterable, Sequence


PLAN_VERSION = 1
WARMUP_RUNS = 1
MEASURED_RUNS = 3
CATALOG_DEFINITION_LIMIT = 10_000
PHYSICALIZATION_OBJECT_LIMIT = 10_000
PHYSICALIZATION_SCOPE_LIMIT_BYTES = 4 << 20
PHYSICALIZATION_MANIFEST_LIMIT_BYTES = 64 << 20
DEFAULT_MAX_ESTIMATED_TABLE_BYTES = 768 << 30
DEFAULT_MIN_FREE_BYTES = 20 << 30
DEFAULT_MIN_FREE_INODES = 100_000
UINT64_BYTES = 8
DEFAULT_BENCHMARK_CPU = 30
DEFAULT_CONTROLLER_CPU = 29


@dataclasses.dataclass(frozen=True)
class Scenario:
    scenario_id: str
    family: str
    parameters: dict[str, Any]
    operations: tuple[str, ...]
    expected_outcome: str = "success"

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class QueryRun:
    ok: bool
    returncode: int | None
    elapsed_seconds: float
    stdout: str
    stderr: str
    timed_out: bool
    query_id: str | None
    artifact_prefix: str
    server_metrics: dict[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "elapsed_seconds": self.elapsed_seconds,
            "timed_out": self.timed_out,
            "query_id": self.query_id,
            "artifact_prefix": self.artifact_prefix,
            "stdout_bytes": len(self.stdout.encode("utf-8", errors="replace")),
            "stderr_bytes": len(self.stderr.encode("utf-8", errors="replace")),
            "server_metrics": self.server_metrics,
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.")
    return normalized or "operation"


def sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def phase_runs(warmups: int, measured: int) -> Iterable[tuple[str, int]]:
    for index in range(warmups):
        yield "warmup", index
    for index in range(measured):
        yield "measured", index


def row_operations(rows: int) -> tuple[str, ...]:
    common = (
        "insert",
        "forced_count",
        "sum",
        "scan_all",
        "filter",
        "transform",
        "join_dimension",
        "group_by",
    )
    if rows <= (1 << 24):
        return common + ("top_n", "native_round_trip")
    if rows == (1 << 26):
        return common
    return (
        "insert",
        "forced_count",
        "sum",
        "filter",
        "bounded_group_by",
        "top_n",
    )


def build_plan() -> list[Scenario]:
    scenarios: list[Scenario] = []
    compositions = ("native", "mixed", "pure_udt")
    for rows in (1 << 20, 1 << 24, 1 << 26, 1 << 32):
        for composition in compositions:
            scenarios.append(
                Scenario(
                    scenario_id=f"row_n{rows}_{composition}",
                    family="row",
                    parameters={
                        "rows": rows,
                        "width": 16,
                        "catalog_definitions": 100,
                        "composition": composition,
                    },
                    operations=row_operations(rows),
                )
            )

    widths = (
        (16, 1 << 20),
        (128, 1 << 17),
        (1_024, 1 << 14),
        (4_096, 1 << 12),
        (10_000, 1 << 11),
    )
    for width, rows in widths:
        for composition in compositions:
            scenarios.append(
                Scenario(
                    scenario_id=f"width_w{width}_n{rows}_{composition}",
                    family="width",
                    parameters={
                        "rows": rows,
                        "width": width,
                        "catalog_definitions": 100,
                        "composition": composition,
                    },
                    operations=(
                        "forced_count",
                        "read_one_column",
                        "scan_all",
                        "expression_all_columns",
                    ),
                )
            )

    for definitions in (100, 1_000, 10_000, 100_000, 1_000_000):
        above_limit = definitions > CATALOG_DEFINITION_LIMIT
        scenarios.append(
            Scenario(
                scenario_id=f"catalog_d{definitions}",
                family="catalog",
                parameters={
                    "definitions": definitions,
                    "width": 16,
                    "rows": 1 << 20,
                    "authority_definition_limit": CATALOG_DEFINITION_LIMIT,
                },
                operations=(
                    ("definition_limit_probe",)
                    if above_limit
                    else (
                        "bulk_create",
                        "restart_load",
                        "inventory",
                        "create_table_binding",
                        "rename_and_redefine",
                        "selective_drop_unused_dry_run",
                        "selective_drop_unused_apply",
                    )
                ),
                expected_outcome="limit_rejected" if above_limit else "success",
            )
        )

    for closure in (1, 100, 1_000, 10_000):
        scenarios.append(
            Scenario(
                scenario_id=f"physical_c{closure}",
                family="physical",
                parameters={
                    "selected_tables": closure,
                    "width": 16,
                    "canary_rows": 1 << 20,
                    "selected_object_limit": PHYSICALIZATION_OBJECT_LIMIT,
                    "scope_limit_bytes": PHYSICALIZATION_SCOPE_LIMIT_BYTES,
                    "manifest_limit_bytes": PHYSICALIZATION_MANIFEST_LIMIT_BYTES,
                },
                operations=("dry_run", "apply", "post_apply_correctness"),
                expected_outcome=(
                    "success_or_limit_rejected"
                    if closure == PHYSICALIZATION_OBJECT_LIMIT
                    else "success"
                ),
            )
        )

    identifiers = [scenario.scenario_id for scenario in scenarios]
    if len(scenarios) != 36 or len(set(identifiers)) != 36:
        raise AssertionError("the UDT server pilot must contain 36 unique scenarios")
    return scenarios


def plan_document(
    scenarios: Sequence[Scenario],
    *,
    warmups: int = WARMUP_RUNS,
    measured: int = MEASURED_RUNS,
    benchmark_cpu: int = DEFAULT_BENCHMARK_CPU,
    controller_cpu: int = DEFAULT_CONTROLLER_CPU,
) -> dict[str, Any]:
    return {
        "plan_version": PLAN_VERSION,
        "scenario_count": len(scenarios),
        "execution": {
            "warmup_runs": warmups,
            "measured_runs": measured,
            "concurrency": 1,
            "max_threads": 1,
            "max_insert_threads": 1,
            "benchmark_cpu": benchmark_cpu,
            "controller_cpu": controller_cpu,
            "fail_fast": False,
            "global_preflight": (
                "a <=16-row SQL/artifact/metrics/physicalization contract must pass "
                "before independent scenarios"
            ),
            "server": "private process with a fresh data root",
            "required_build_type": "Release",
            "metrics": {
                "primary_elapsed": "controller monotonic wall time",
                "server": (
                    "system.query_log QueryFinish aggregates: duration, rows, bytes, "
                    "peak memory, and all available ProfileEvents"
                ),
                "pmu_cycles_and_instructions": "available only if exposed by ProfileEvents",
            },
        },
        "product_limits": {
            "catalog_definitions": CATALOG_DEFINITION_LIMIT,
            "physicalization_selected_objects": PHYSICALIZATION_OBJECT_LIMIT,
            "physicalization_scope_bytes": PHYSICALIZATION_SCOPE_LIMIT_BYTES,
            "physicalization_manifest_bytes": PHYSICALIZATION_MANIFEST_LIMIT_BYTES,
            "catalog_points_above_10000": (
                "execute a bounded 10001-definition rejection probe; they do not "
                "pretend to be supported catalog setups"
            ),
        },
        "data_safety": {
            "compositions_are_materialized_sequentially": True,
            "no_independent_2_pow_32_copies": True,
            "large_tables_use_zstd_and_compressible_deterministic_values": True,
            "large_points_are_resource_checked_and_explicitly_rejected": True,
            "database_cleanup_is_sync": True,
            "controller_never_recursively_deletes_an_existing_root": True,
        },
        "scenarios": [scenario.as_dict() for scenario in scenarios],
    }


class Campaign:
    def __init__(self, args: argparse.Namespace, scenarios: Sequence[Scenario]) -> None:
        self.args = args
        self.scenarios = list(scenarios)
        self.output_dir = Path(args.output_dir).resolve()
        self.data_root = (
            Path(args.data_root).resolve()
            if args.data_root
            else self.output_dir / "server-root"
        )
        self.clickhouse = Path(args.clickhouse).resolve()
        self.config_dir = self.output_dir / "server-config"
        self.scenarios_dir = self.output_dir / "scenarios"
        self.server_process: subprocess.Popen[bytes] | None = None
        self.server_log_handle: Any = None
        self.server_error_handle: Any = None
        self.port = self._free_port()
        self.query_sequence = 0
        self.taskset = shutil.which("taskset")
        self.status: dict[str, Any] = {
            "state": "initializing",
            "started_at": utc_now(),
            "finished_at": None,
            "plan_version": PLAN_VERSION,
            "total": len(self.scenarios),
            "completed": 0,
            "failed": 0,
            "resource_rejected": 0,
            "limit_rejected": 0,
            "current": None,
        }

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _write_status(self) -> None:
        atomic_json(self.output_dir / "status.json", self.status)

    def prepare(self) -> None:
        if not self.clickhouse.is_file() or not os.access(self.clickhouse, os.X_OK):
            raise RuntimeError(f"ClickHouse binary is not executable: {self.clickhouse}")
        if not self.taskset:
            raise RuntimeError("taskset is required to isolate benchmark and controller CPUs")
        available_cpus = os.sched_getaffinity(0)
        requested_cpus = {self.args.benchmark_cpu, self.args.controller_cpu}
        if not requested_cpus.issubset(available_cpus):
            raise RuntimeError(
                f"requested CPUs {sorted(requested_cpus)} are not in the allowed affinity "
                f"set {sorted(available_cpus)}"
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.data_root.exists():
            raise RuntimeError(
                f"refusing to reuse existing benchmark data root: {self.data_root}"
            )
        self.data_root.mkdir(parents=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.scenarios_dir.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.data_root / "data",
            self.data_root / "tmp",
            self.data_root / "user_files",
            self.data_root / "format_schemas",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        atomic_json(
            self.output_dir / "plan.json",
            plan_document(
                self.scenarios,
                warmups=self.args.warmups,
                measured=self.args.measured,
                benchmark_cpu=self.args.benchmark_cpu,
                controller_cpu=self.args.controller_cpu,
            ),
        )
        atomic_json(
            self.output_dir / "invocation.json",
            {
                "argv": sys.argv,
                "clickhouse": str(self.clickhouse),
                "output_dir": str(self.output_dir),
                "data_root": str(self.data_root),
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "benchmark_cpu": self.args.benchmark_cpu,
                "controller_cpu": self.args.controller_cpu,
                "started_at": self.status["started_at"],
            },
        )
        self._write_server_config()
        self._write_status()

    def _write_server_config(self) -> None:
        config = f"""<clickhouse>
    <logger>
        <level>warning</level>
        <log>{self.output_dir / 'clickhouse-server.log'}</log>
        <errorlog>{self.output_dir / 'clickhouse-server.err.log'}</errorlog>
        <size>1G</size>
        <count>3</count>
    </logger>
    <listen_host>127.0.0.1</listen_host>
    <tcp_port>{self.port}</tcp_port>
    <path>{self.data_root / 'data'}/</path>
    <tmp_path>{self.data_root / 'tmp'}/</tmp_path>
    <user_files_path>{self.data_root / 'user_files'}/</user_files_path>
    <format_schema_path>{self.data_root / 'format_schemas'}/</format_schema_path>
    <users_config>{self.config_dir / 'users.xml'}</users_config>
    <default_profile>default</default_profile>
    <default_database>default</default_database>
    <pid_file>{self.data_root / 'clickhouse.pid'}</pid_file>
    <mlock_executable>false</mlock_executable>
    <max_table_size_to_drop>0</max_table_size_to_drop>
    <query_log>
        <database>system</database>
        <table>query_log</table>
        <flush_interval_milliseconds>1000</flush_interval_milliseconds>
    </query_log>
</clickhouse>
"""
        users = """<clickhouse>
    <profiles>
        <default>
            <allow_experimental_user_defined_types>1</allow_experimental_user_defined_types>
            <max_threads>1</max_threads>
            <max_insert_threads>1</max_insert_threads>
            <max_query_size>104857600</max_query_size>
            <max_ast_elements>1000000</max_ast_elements>
            <max_expanded_ast_elements>1000000</max_expanded_ast_elements>
            <max_parser_depth>100000</max_parser_depth>
            <max_columns_to_read>0</max_columns_to_read>
            <log_queries>1</log_queries>
            <log_profile_events>1</log_profile_events>
            <log_query_threads>0</log_query_threads>
        </default>
    </profiles>
    <users>
        <default>
            <password></password>
            <networks><ip>127.0.0.1</ip><ip>::1</ip></networks>
            <profile>default</profile>
            <quota>default</quota>
            <access_management>1</access_management>
        </default>
    </users>
    <quotas><default><interval><duration>3600</duration><queries>0</queries>
        <errors>0</errors><result_rows>0</result_rows><read_rows>0</read_rows>
        <execution_time>0</execution_time></interval></default></quotas>
</clickhouse>
"""
        (self.config_dir / "config.xml").write_text(config, encoding="utf-8")
        (self.config_dir / "users.xml").write_text(users, encoding="utf-8")

    def start_server(self) -> float:
        if self.server_process is not None:
            raise RuntimeError("private ClickHouse server is already running")
        started = time.monotonic()
        self.server_log_handle = (self.output_dir / "server-process.stdout").open("ab")
        self.server_error_handle = (self.output_dir / "server-process.stderr").open("ab")
        self.server_process = subprocess.Popen(
            [
                str(self.taskset),
                "--cpu-list",
                str(self.args.benchmark_cpu),
                str(self.clickhouse),
                "server",
                f"--config-file={self.config_dir / 'config.xml'}",
            ],
            stdout=self.server_log_handle,
            stderr=self.server_error_handle,
            start_new_session=True,
        )
        deadline = time.monotonic() + self.args.startup_timeout
        last_error = "server did not answer"
        while time.monotonic() < deadline:
            if self.server_process.poll() is not None:
                raise RuntimeError(
                    f"ClickHouse server exited during startup with rc={self.server_process.returncode}"
                )
            probe = subprocess.run(
                self._client_command(),
                input="SELECT 1",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
            )
            if probe.returncode == 0 and probe.stdout.strip() == "1":
                return time.monotonic() - started
            last_error = probe.stderr.strip() or last_error
            time.sleep(0.25)
        raise RuntimeError(f"ClickHouse server startup timed out: {last_error}")

    def stop_server(self) -> None:
        process = self.server_process
        if process is not None:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=30)
            self.server_process = None
        if self.server_log_handle is not None:
            self.server_log_handle.close()
            self.server_log_handle = None
        if self.server_error_handle is not None:
            self.server_error_handle.close()
            self.server_error_handle = None

    def restart_server(self) -> float:
        started = time.monotonic()
        self.stop_server()
        self.start_server()
        return time.monotonic() - started

    def server_process_snapshot(self) -> dict[str, Any] | None:
        process = self.server_process
        if process is None or process.poll() is not None:
            return None
        try:
            stat_fields = Path(f"/proc/{process.pid}/stat").read_text(
                encoding="utf-8"
            ).split()
            status_fields: dict[str, str] = {}
            for line in Path(f"/proc/{process.pid}/status").read_text(
                encoding="utf-8"
            ).splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    status_fields[key] = value.strip()
            ticks = os.sysconf("SC_CLK_TCK")
            return {
                "pid": process.pid,
                "user_cpu_seconds": int(stat_fields[13]) / ticks,
                "system_cpu_seconds": int(stat_fields[14]) / ticks,
                "rss": status_fields.get("VmRSS"),
                "peak_rss": status_fields.get("VmHWM"),
                "voluntary_context_switches": status_fields.get(
                    "voluntary_ctxt_switches"
                ),
                "nonvoluntary_context_switches": status_fields.get(
                    "nonvoluntary_ctxt_switches"
                ),
            }
        except (OSError, ValueError, IndexError):
            return None

    def _client_command(self, *, multiquery: bool = False, query_id: str | None = None) -> list[str]:
        command = [
            str(self.taskset),
            "--cpu-list",
            str(self.args.controller_cpu),
            str(self.clickhouse),
            "client",
            "--host=127.0.0.1",
            f"--port={self.port}",
            "--max_threads=1",
            "--max_insert_threads=1",
            "--allow_experimental_user_defined_types=1",
            "--max_query_size=104857600",
            "--max_ast_elements=1000000",
            "--max_expanded_ast_elements=1000000",
            "--max_parser_depth=100000",
            f"--receive_timeout={self.args.query_timeout}",
            f"--send_timeout={self.args.query_timeout}",
        ]
        if multiquery:
            command.append("--multiquery")
        if query_id:
            command.append(f"--query_id={query_id}")
        return command

    def query(
        self,
        artifact_dir: Path,
        label: str,
        sql: str,
        *,
        multiquery: bool = False,
        discard_stdout: bool = False,
        timeout: int | None = None,
        query_id: str | None = None,
        collect_metrics: bool = True,
    ) -> QueryRun:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.query_sequence += 1
        prefix = f"{self.query_sequence:06d}_{slug(label)}"
        artifact_prefix = artifact_dir / prefix
        (artifact_prefix.with_suffix(".sql")).write_text(sql.rstrip() + "\n", encoding="utf-8")
        started_at = utc_now()
        started = time.monotonic()
        timed_out = False
        stdout = ""
        stderr = ""
        returncode: int | None = None
        effective_timeout = timeout or self.args.query_timeout
        try:
            completed = subprocess.run(
                self._client_command(multiquery=multiquery, query_id=query_id),
                input=sql,
                text=True,
                stdout=subprocess.DEVNULL if discard_stdout else subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=effective_timeout,
                check=False,
            )
            returncode = completed.returncode
            stdout = "[discarded by data-safety policy]\n" if discard_stdout else completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as error:
            timed_out = True
            stdout = (
                error.stdout.decode(errors="replace")
                if isinstance(error.stdout, bytes)
                else (error.stdout or "")
            )
            stderr = (
                error.stderr.decode(errors="replace")
                if isinstance(error.stderr, bytes)
                else (error.stderr or "")
            )
            stderr += f"\ncontroller timeout after {effective_timeout} seconds\n"
        elapsed = time.monotonic() - started
        (artifact_prefix.with_suffix(".stdout")).write_text(stdout, encoding="utf-8")
        (artifact_prefix.with_suffix(".stderr")).write_text(stderr, encoding="utf-8")
        status = {
            "label": label,
            "started_at": started_at,
            "finished_at": utc_now(),
            "elapsed_seconds": elapsed,
            "returncode": returncode,
            "timed_out": timed_out,
            "ok": returncode == 0 and not timed_out,
            "query_id": query_id,
            "multiquery": multiquery,
            "stdout_policy": "discard" if discard_stdout else "capture",
            "sql_bytes": len(sql.encode("utf-8")),
            "stdout_bytes": len(stdout.encode("utf-8", errors="replace")),
            "stderr_bytes": len(stderr.encode("utf-8", errors="replace")),
        }
        result = QueryRun(
            ok=bool(status["ok"]),
            returncode=returncode,
            elapsed_seconds=elapsed,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            query_id=query_id,
            artifact_prefix=str(artifact_prefix.relative_to(self.output_dir)),
        )
        if query_id and collect_metrics:
            result.server_metrics = self._collect_server_metrics(
                artifact_dir / f"{prefix}_metrics", query_id
            )
        status["server_metrics"] = result.server_metrics
        atomic_json(artifact_prefix.with_suffix(".status.json"), status)
        return result

    def _collect_server_metrics(
        self, artifact_dir: Path, query_id: str
    ) -> dict[str, Any]:
        """Collect the server-side record without replacing monotonic wall time.

        A multiquery client can produce several QueryFinish records under one
        query id, so scalar fields are aggregated and ProfileEvents are summed
        by name.  The raw metric queries remain beside the measured query.
        """
        collection: list[dict[str, Any]] = []
        core: dict[str, Any] | None = None
        quoted_id = sql_string(query_id)
        core_sql = (
            "SELECT count() AS query_log_records, "
            "sum(query_duration_ms) AS query_duration_ms, "
            "sum(read_rows) AS read_rows, sum(read_bytes) AS read_bytes, "
            "sum(written_rows) AS written_rows, sum(written_bytes) AS written_bytes, "
            "sum(result_rows) AS result_rows, sum(result_bytes) AS result_bytes, "
            "max(memory_usage) AS peak_memory_usage, "
            "max(peak_threads_usage) AS peak_threads_usage, "
            "max(exception_code) AS exception_code, "
            "groupUniqArray(toString(type)) AS record_types "
            "FROM system.query_log "
            f"WHERE query_id = {quoted_id} AND type != 'QueryStart' "
            "FORMAT JSONEachRow"
        )
        for attempt in range(3):
            flush = self.query(
                artifact_dir,
                f"flush_query_log_{attempt}",
                "SYSTEM FLUSH LOGS",
                collect_metrics=False,
            )
            lookup = self.query(
                artifact_dir,
                f"query_log_core_{attempt}",
                core_sql,
                collect_metrics=False,
            )
            collection.append(
                {"flush": flush.summary(), "lookup": lookup.summary()}
            )
            try:
                core = json.loads(lookup.stdout.strip()) if lookup.ok else None
            except (TypeError, ValueError):
                core = None
            if core and int(core.get("query_log_records", 0)) > 0:
                break
            time.sleep(0.2)

        profile_sql = (
            "SELECT event, sum(value) AS value FROM system.query_log "
            "ARRAY JOIN mapKeys(ProfileEvents) AS event, "
            "mapValues(ProfileEvents) AS value "
            f"WHERE query_id = {quoted_id} AND type != 'QueryStart' "
            "GROUP BY event ORDER BY event FORMAT JSONEachRow"
        )
        profile = self.query(
            artifact_dir,
            "query_log_profile_events",
            profile_sql,
            collect_metrics=False,
        )
        profile_events: dict[str, int] = {}
        if profile.ok:
            try:
                for line in profile.stdout.splitlines():
                    if line:
                        row = json.loads(line)
                        profile_events[str(row["event"])] = int(row["value"])
            except (KeyError, TypeError, ValueError):
                profile_events = {}
        result = {
            "available": bool(core and int(core.get("query_log_records", 0)) > 0),
            "external_monotonic_is_primary": True,
            "core": core,
            "profile_events": profile_events,
            "collection": collection,
            "profile_query": profile.summary(),
        }
        atomic_json(artifact_dir / "metrics.json", result)
        return result

    def measured_sql(
        self,
        scenario_dir: Path,
        name: str,
        sql: str,
        *,
        prepare: Callable[[str, int, Path], QueryRun | None] | None = None,
        discard_stdout: bool = False,
    ) -> dict[str, Any]:
        operation_dir = scenario_dir / slug(name)
        runs: list[dict[str, Any]] = []
        ok = True
        for phase, index in phase_runs(self.args.warmups, self.args.measured):
            run_dir = operation_dir / f"{phase}_{index}"
            preparation = prepare(phase, index, run_dir) if prepare else None
            if preparation is not None and not preparation.ok:
                ok = False
                runs.append(
                    {
                        "phase": phase,
                        "index": index,
                        "preparation": preparation.summary(),
                        "query": None,
                    }
                )
                continue
            query_id = f"udt_bench_{slug(name)}_{uuid.uuid4().hex}"
            result = self.query(
                run_dir,
                name,
                sql,
                discard_stdout=discard_stdout,
                query_id=query_id,
            )
            ok = ok and result.ok
            runs.append(
                {
                    "phase": phase,
                    "index": index,
                    "preparation": preparation.summary() if preparation else None,
                    "query": result.summary(),
                }
            )
        document = {"operation": name, "ok": ok, "runs": runs}
        atomic_json(operation_dir / "result.json", document)
        return document

    def _disk_preflight(
        self,
        scenario: Scenario,
        *,
        estimated_table_bytes: int = 0,
        estimated_inodes: int = 0,
    ) -> dict[str, Any]:
        usage = shutil.disk_usage(self.data_root)
        stat = os.statvfs(self.data_root)
        free_inodes = int(stat.f_favail)
        reasons: list[str] = []
        if estimated_table_bytes > self.args.max_estimated_table_bytes:
            reasons.append(
                "estimated table footprint exceeds --max-estimated-table-bytes"
            )
        if estimated_table_bytes + self.args.min_free_bytes > usage.free:
            reasons.append("insufficient free bytes while preserving the configured reserve")
        required_inodes = estimated_inodes + self.args.min_free_inodes
        if required_inodes > free_inodes:
            reasons.append("insufficient free inodes while preserving the configured reserve")
        return {
            "scenario_id": scenario.scenario_id,
            "estimated_table_bytes": estimated_table_bytes,
            "estimated_inodes": estimated_inodes,
            "free_bytes": usage.free,
            "free_inodes": free_inodes,
            "max_estimated_table_bytes": self.args.max_estimated_table_bytes,
            "min_free_bytes": self.args.min_free_bytes,
            "min_free_inodes": self.args.min_free_inodes,
            "accepted": not reasons,
            "reasons": reasons,
        }

    @staticmethod
    def _type_definitions(database: str, count: int) -> str:
        return "\n".join(
            f"CREATE TYPE {database}.BenchmarkType{index:06d} AS "
            f"{Campaign._physical_type(index)};"
            for index in range(count)
        )

    @staticmethod
    def _physical_type(index: int) -> str:
        return (
            "UInt64",
            "FixedString(16)",
            "Nullable(UInt64)",
            "LowCardinality(String)",
        )[index % 4]

    @staticmethod
    def _column_type(database: str, index: int, composition: str) -> str:
        if composition == "native":
            return Campaign._physical_type(index)
        # Alternate complete four-family groups, not individual columns, so
        # both the logical and physical halves of a mixed table contain all
        # four approved physical families.
        if composition == "mixed" and (index // 4) % 2:
            return Campaign._physical_type(index)
        return f"{database}.BenchmarkType{index % 100:06d}"

    def _table_columns(self, database: str, width: int, composition: str) -> str:
        digits = max(5, len(str(width - 1)))
        return ",\n".join(
            f"c{index:0{digits}d} {self._column_type(database, index, composition)} "
            "CODEC(ZSTD(1))"
            for index in range(width)
        )

    @staticmethod
    def _insert_select(width: int) -> str:
        expressions: list[str] = []
        for index in range(width):
            value = f"number + {index}"
            family = index % 4
            if family == 0:
                expressions.append(f"toUInt64({value})")
            elif family == 1:
                expressions.append(f"toFixedString(toString({value}), 16)")
            elif family == 2:
                expressions.append(
                    f"if(({value}) % 16 = 0, NULL, toUInt64({value}))"
                )
            else:
                expressions.append(f"toString(({value}) % 4096)")
        return ", ".join(expressions)

    @staticmethod
    def _numeric_column_expression(name: str, index: int) -> str:
        family = index % 4
        if family == 0:
            return name
        if family == 2:
            return f"ifNull({name}, toUInt64(0))"
        return f"cityHash64({name})"

    @staticmethod
    def _literal_for_family(index: int) -> str:
        family = index % 4
        if family == 0:
            return str(index + 1)
        if family == 1:
            return sql_string(f"fixed-{index:02d}")
        if family == 2:
            return "NULL" if index % 8 == 2 else str(index + 1)
        return sql_string(f"label-{index % 8}")

    @staticmethod
    def _estimated_compressed_table_bytes(rows: int, width: int) -> int:
        # Sequential integers, bounded strings, and explicit ZSTD(1) codecs are
        # deliberately compressible.  Six bytes per cell includes a 50% safety
        # margin over the measured-design estimate and still admits 2^32 x 16
        # within a filesystem budget of 1.2 TiB.
        return rows * width * 6

    @staticmethod
    def _balanced_sum(names: Sequence[str]) -> str:
        level = list(names)
        while len(level) > 1:
            next_level: list[str] = []
            for index in range(0, len(level), 2):
                if index + 1 == len(level):
                    next_level.append(level[index])
                else:
                    next_level.append(f"({level[index]} + {level[index + 1]})")
            level = next_level
        return level[0]

    def _setup_data_table(
        self,
        scenario_dir: Path,
        database: str,
        *,
        width: int,
        composition: str,
        definitions: int = 100,
    ) -> QueryRun:
        columns = self._table_columns(database, width, composition)
        sql = (
            f"CREATE DATABASE {database} ENGINE = Atomic;\n"
            + self._type_definitions(database, definitions)
            + "\n"
            + f"CREATE TABLE {database}.data (\n{columns}\n) "
            "ENGINE = MergeTree ORDER BY c00000;\n"
            + f"CREATE TABLE {database}.dimension (id UInt64) "
            "ENGINE = Memory;\n"
            + f"INSERT INTO {database}.dimension SELECT number FROM numbers(4096);"
        )
        return self.query(scenario_dir / "setup", "setup", sql, multiquery=True)

    def _check_data_schema(
        self,
        scenario_dir: Path,
        database: str,
        *,
        width: int,
        composition: str,
    ) -> tuple[bool, dict[str, Any]]:
        check = self.query(
            scenario_dir / "correctness",
            "physical_schema",
            "SELECT name, type, udt_declared_type FROM system.columns "
            f"WHERE database = {sql_string(database)} AND table = 'data' "
            "ORDER BY position FORMAT TSV",
        )
        rows = [line.split("\t") for line in check.stdout.splitlines() if line]
        physical_ok = bool(
            check.ok
            and len(rows) == width
            and all(
                len(row) == 3 and row[1] == self._physical_type(index)
                for index, row in enumerate(rows)
            )
        )
        expected_mapped = sum(
            1
            for index in range(width)
            if composition == "pure_udt"
            or (composition == "mixed" and (index // 4) % 2 == 0)
        )
        observed_mapped = sum(1 for row in rows if len(row) == 3 and row[2])
        logical_ok = observed_mapped == expected_mapped
        return physical_ok and logical_ok, {
            "query": check.summary(),
            "physical_types_match": physical_ok,
            "expected_mapped_columns": expected_mapped,
            "observed_mapped_columns": observed_mapped,
            "logical_composition_matches": logical_ok,
        }

    def _drop_database(self, scenario_dir: Path, database: str) -> QueryRun:
        return self.query(
            scenario_dir / "cleanup",
            "drop_database",
            f"DROP DATABASE IF EXISTS {database} SYNC",
        )

    def run_row(self, scenario: Scenario, scenario_dir: Path) -> dict[str, Any]:
        parameters = scenario.parameters
        rows = int(parameters["rows"])
        width = int(parameters["width"])
        composition = str(parameters["composition"])
        estimated = self._estimated_compressed_table_bytes(rows, width)
        preflight = self._disk_preflight(
            scenario,
            estimated_table_bytes=estimated,
            estimated_inodes=width * 12 + 10_000,
        )
        atomic_json(scenario_dir / "resource-preflight.json", preflight)
        if not preflight["accepted"]:
            return {
                "scenario": scenario.as_dict(),
                "status": "resource_rejected",
                "resource_preflight": preflight,
                "operations": [],
            }

        database = f"udt_bench_row_{uuid.uuid4().hex[:12]}"
        operations: list[dict[str, Any]] = []
        setup = self._setup_data_table(
            scenario_dir,
            database,
            width=width,
            composition=composition,
        )
        if not setup.ok:
            self._drop_database(scenario_dir, database)
            return {
                "scenario": scenario.as_dict(),
                "status": "failed",
                "failure": "setup",
                "setup": setup.summary(),
                "operations": operations,
            }

        try:
            schema_ok, schema_check = self._check_data_schema(
                scenario_dir,
                database,
                width=width,
                composition=composition,
            )
            insert_sql = (
                f"INSERT INTO {database}.data SELECT {self._insert_select(width)} "
                f"FROM numbers({rows}) SETTINGS max_threads = 1"
            )

            def truncate(_phase: str, _index: int, run_dir: Path) -> QueryRun:
                return self.query(
                    run_dir,
                    "truncate_before_insert",
                    f"TRUNCATE TABLE {database}.data SYNC",
                )

            operations.append(
                self.measured_sql(
                    scenario_dir,
                    "insert",
                    insert_sql,
                    prepare=truncate,
                )
            )
            count_check = self.query(
                scenario_dir / "correctness",
                "row_count",
                f"SELECT count() FROM {database}.data",
            )
            count_ok = count_check.ok and count_check.stdout.strip() == str(rows)
            checks: dict[str, Any] = {
                "schema": schema_check,
                "row_count": count_check.summary(),
                "row_count_matches": count_ok,
            }

            operation_sql = {
                "forced_count": (
                    f"SELECT count() FROM {database}.data "
                    "SETTINGS optimize_trivial_count_query = 0"
                ),
                "sum": f"SELECT sum(c00000) FROM {database}.data",
                "scan_all": f"SELECT * FROM {database}.data FORMAT Null",
                "filter": (
                    f"SELECT count() FROM {database}.data WHERE c00000 % 97 = 0"
                ),
                "transform": (
                    f"SELECT sum(bitXor(c00000, toUInt64(11400714819323198485))) "
                    f"FROM {database}.data"
                ),
                "join_dimension": (
                    f"SELECT sum(d.c00000) FROM {database}.data AS d ANY INNER JOIN "
                    f"{database}.dimension AS x ON d.c00000 % 4096 = x.id"
                ),
                "group_by": (
                    f"SELECT c00000 % 4096 AS bucket, count() FROM {database}.data "
                    "GROUP BY bucket FORMAT Null"
                ),
                "bounded_group_by": (
                    f"SELECT c00000 % 4096 AS bucket, count() FROM {database}.data "
                    "GROUP BY bucket ORDER BY bucket LIMIT 1000 FORMAT Null"
                ),
                "top_n": (
                    f"SELECT * FROM {database}.data ORDER BY c00000 DESC "
                    "LIMIT 1000 FORMAT Null"
                ),
                "native_round_trip": f"SELECT * FROM {database}.data FORMAT Native",
            }
            for name in scenario.operations:
                if name == "insert":
                    continue
                operations.append(
                    self.measured_sql(
                        scenario_dir,
                        name,
                        operation_sql[name],
                        discard_stdout=name == "native_round_trip",
                    )
                )
            success = schema_ok and count_ok and all(
                operation["ok"] for operation in operations
            )
            return {
                "scenario": scenario.as_dict(),
                "status": "passed" if success else "failed",
                "resource_preflight": preflight,
                "setup": setup.summary(),
                "checks": checks,
                "operations": operations,
            }
        finally:
            self._drop_database(scenario_dir, database)

    def run_width(self, scenario: Scenario, scenario_dir: Path) -> dict[str, Any]:
        parameters = scenario.parameters
        rows = int(parameters["rows"])
        width = int(parameters["width"])
        composition = str(parameters["composition"])
        estimated = self._estimated_compressed_table_bytes(rows, width)
        preflight = self._disk_preflight(
            scenario,
            estimated_table_bytes=estimated,
            estimated_inodes=width * 12 + 10_000,
        )
        atomic_json(scenario_dir / "resource-preflight.json", preflight)
        if not preflight["accepted"]:
            return {
                "scenario": scenario.as_dict(),
                "status": "resource_rejected",
                "resource_preflight": preflight,
                "operations": [],
            }

        database = f"udt_bench_width_{uuid.uuid4().hex[:12]}"
        operations: list[dict[str, Any]] = []
        setup = self._setup_data_table(
            scenario_dir,
            database,
            width=width,
            composition=composition,
        )
        if not setup.ok:
            self._drop_database(scenario_dir, database)
            return {
                "scenario": scenario.as_dict(),
                "status": "failed",
                "failure": "setup",
                "setup": setup.summary(),
                "operations": operations,
            }
        try:
            schema_ok, schema_check = self._check_data_schema(
                scenario_dir,
                database,
                width=width,
                composition=composition,
            )
            insert = self.query(
                scenario_dir / "setup",
                "populate",
                f"INSERT INTO {database}.data SELECT {self._insert_select(width)} "
                f"FROM numbers({rows}) SETTINGS max_threads = 1",
            )
            if not insert.ok:
                return {
                    "scenario": scenario.as_dict(),
                    "status": "failed",
                    "failure": "populate",
                    "setup": setup.summary(),
                    "populate": insert.summary(),
                    "operations": operations,
                }
            names = [f"c{index:0{max(5, len(str(width - 1)))}d}" for index in range(width)]
            numeric_names = [
                self._numeric_column_expression(name, index)
                for index, name in enumerate(names)
            ]
            expression = self._balanced_sum(numeric_names)
            sql_by_name = {
                "forced_count": (
                    f"SELECT count() FROM {database}.data "
                    "SETTINGS optimize_trivial_count_query = 0"
                ),
                "read_one_column": f"SELECT c00000 FROM {database}.data FORMAT Null",
                "scan_all": f"SELECT * FROM {database}.data FORMAT Null",
                "expression_all_columns": (
                    f"SELECT sum({expression}) FROM {database}.data"
                ),
            }
            for name in scenario.operations:
                operations.append(
                    self.measured_sql(scenario_dir, name, sql_by_name[name])
                )
            count = self.query(
                scenario_dir / "correctness",
                "row_count",
                f"SELECT count() FROM {database}.data",
            )
            count_ok = count.ok and count.stdout.strip() == str(rows)
            success = schema_ok and count_ok and all(
                operation["ok"] for operation in operations
            )
            return {
                "scenario": scenario.as_dict(),
                "status": "passed" if success else "failed",
                "resource_preflight": preflight,
                "setup": setup.summary(),
                "populate": insert.summary(),
                "checks": {
                    "schema": schema_check,
                    "row_count": count.summary(),
                    "row_count_matches": count_ok,
                },
                "operations": operations,
            }
        finally:
            self._drop_database(scenario_dir, database)

    def _run_catalog_limit_probe(
        self, scenario: Scenario, scenario_dir: Path
    ) -> dict[str, Any]:
        requested = int(scenario.parameters["definitions"])
        attempts: list[dict[str, Any]] = []
        all_expected = True
        # The authority hard limit is 10,000.  A bounded 10,001st-definition
        # probe proves the requested 100k/1m point is unsupported without
        # writing hundreds of thousands of doomed statements.
        probe_count = CATALOG_DEFINITION_LIMIT + 1
        for phase, index in phase_runs(self.args.warmups, self.args.measured):
            database = f"udt_bench_limit_{uuid.uuid4().hex[:12]}"
            run_dir = scenario_dir / "definition_limit_probe" / f"{phase}_{index}"
            create_database = self.query(
                run_dir, "create_database", f"CREATE DATABASE {database} ENGINE = Atomic"
            )
            probe = self.query(
                run_dir,
                "create_definition_limit_probe",
                self._type_definitions(database, probe_count),
                multiquery=True,
                query_id=f"udt_bench_definition_limit_{uuid.uuid4().hex}",
            )
            count = self.query(
                run_dir,
                "definition_count_after_probe",
                "SELECT count() FROM system.user_defined_types "
                f"WHERE database = {sql_string(database)}",
            )
            lowered_error = probe.stderr.lower()
            expected = (
                create_database.ok
                and not probe.ok
                and count.ok
                and count.stdout.strip() == str(CATALOG_DEFINITION_LIMIT)
                and (
                    "limit" in lowered_error
                    or "maximum" in lowered_error
                    or "10,000" in lowered_error
                    or "10000" in lowered_error
                )
            )
            all_expected = all_expected and expected
            attempts.append(
                {
                    "phase": phase,
                    "index": index,
                    "requested_definitions": requested,
                    "bounded_probe_definitions": probe_count,
                    "expected_limit_rejection": expected,
                    "create_database": create_database.summary(),
                    "probe": probe.summary(),
                    "count": count.summary(),
                    "observed_count": count.stdout.strip(),
                }
            )
            self._drop_database(run_dir, database)
        return {
            "scenario": scenario.as_dict(),
            "status": "limit_rejected" if all_expected else "failed",
            "reason": (
                f"requested {requested} definitions exceeds the fixed Atomic authority "
                f"limit of {CATALOG_DEFINITION_LIMIT}"
            ),
            "attempts": attempts,
        }

    def run_catalog(self, scenario: Scenario, scenario_dir: Path) -> dict[str, Any]:
        definitions = int(scenario.parameters["definitions"])
        if definitions > CATALOG_DEFINITION_LIMIT:
            return self._run_catalog_limit_probe(scenario, scenario_dir)

        estimated_bytes = definitions * 16_384 + (1 << 20) * 16 * UINT64_BYTES
        preflight = self._disk_preflight(
            scenario,
            estimated_table_bytes=estimated_bytes,
            estimated_inodes=definitions * 8 + 50_000,
        )
        atomic_json(scenario_dir / "resource-preflight.json", preflight)
        if not preflight["accepted"]:
            return {
                "scenario": scenario.as_dict(),
                "status": "resource_rejected",
                "resource_preflight": preflight,
                "operations": [],
            }

        database = f"udt_bench_catalog_{uuid.uuid4().hex[:12]}"
        operations: list[dict[str, Any]] = []
        bulk_runs: list[dict[str, Any]] = []
        bulk_ok = True
        try:
            for phase, index in phase_runs(self.args.warmups, self.args.measured):
                current_database = (
                    database
                    if phase == "measured" and index == self.args.measured - 1
                    else f"{database}_{phase}_{index}"
                )
                run_dir = scenario_dir / "bulk_create" / f"{phase}_{index}"
                create_database = self.query(
                    run_dir,
                    "create_database",
                    f"CREATE DATABASE {current_database} ENGINE = Atomic",
                )
                create_types = self.query(
                    run_dir,
                    "bulk_create",
                    self._type_definitions(current_database, definitions),
                    multiquery=True,
                    query_id=f"udt_bench_bulk_{uuid.uuid4().hex}",
                )
                count = self.query(
                    run_dir,
                    "definition_count",
                    "SELECT count() FROM system.user_defined_types "
                    f"WHERE database = {sql_string(current_database)}",
                )
                valid = (
                    create_database.ok
                    and create_types.ok
                    and count.ok
                    and count.stdout.strip() == str(definitions)
                )
                bulk_ok = bulk_ok and valid
                bulk_runs.append(
                    {
                        "phase": phase,
                        "index": index,
                        "ok": valid,
                        "create_database": create_database.summary(),
                        "bulk_create": create_types.summary(),
                        "count": count.summary(),
                    }
                )
                if current_database != database:
                    self._drop_database(run_dir, current_database)
            bulk_operation = {"operation": "bulk_create", "ok": bulk_ok, "runs": bulk_runs}
            atomic_json(scenario_dir / "bulk_create" / "result.json", bulk_operation)
            operations.append(bulk_operation)
            if not bulk_ok:
                return {
                    "scenario": scenario.as_dict(),
                    "status": "failed",
                    "resource_preflight": preflight,
                    "operations": operations,
                }

            restart_runs: list[dict[str, Any]] = []
            restart_ok = True
            for phase, index in phase_runs(self.args.warmups, self.args.measured):
                run_dir = scenario_dir / "restart_load" / f"{phase}_{index}"
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "restart.sql").write_text(
                    "-- stop and restart the private server; then verify the catalog\n",
                    encoding="utf-8",
                )
                started = time.monotonic()
                error = ""
                try:
                    elapsed = self.restart_server()
                    process_metrics = self.server_process_snapshot()
                    check = self.query(
                        run_dir,
                        "definition_count_after_restart",
                        "SELECT count() FROM system.user_defined_types "
                        f"WHERE database = {sql_string(database)}",
                    )
                    valid = check.ok and check.stdout.strip() == str(definitions)
                except BaseException as exception:  # recorded; outer campaign continues.
                    elapsed = time.monotonic() - started
                    error = repr(exception)
                    valid = False
                    check = None
                    process_metrics = self.server_process_snapshot()
                restart_ok = restart_ok and valid
                status = {
                    "phase": phase,
                    "index": index,
                    "ok": valid,
                    "elapsed_seconds": elapsed,
                    "error": error,
                    "server_process": process_metrics,
                    "count_check": check.summary() if check else None,
                }
                atomic_json(run_dir / "restart.status.json", status)
                (run_dir / "restart.stdout").write_text("", encoding="utf-8")
                (run_dir / "restart.stderr").write_text(error + "\n" if error else "", encoding="utf-8")
                restart_runs.append(status)
                if self.server_process is None:
                    self.start_server()
            restart_operation = {
                "operation": "restart_load",
                "ok": restart_ok,
                "runs": restart_runs,
            }
            atomic_json(scenario_dir / "restart_load" / "result.json", restart_operation)
            operations.append(restart_operation)

            operations.append(
                self.measured_sql(
                    scenario_dir,
                    "inventory",
                    "SELECT name, revision, underlying_type, create_query "
                    "FROM system.user_defined_types "
                    f"WHERE database = {sql_string(database)} ORDER BY name FORMAT Null",
                )
            )

            binding_runs: list[dict[str, Any]] = []
            binding_ok = True
            columns = ", ".join(
                f"c{index:02d} {database}.BenchmarkType{index:06d}"
                for index in range(16)
            )
            for phase, index in phase_runs(self.args.warmups, self.args.measured):
                table = (
                    "bound"
                    if phase == "measured" and index == self.args.measured - 1
                    else f"bound_{phase}_{index}"
                )
                run_dir = scenario_dir / "create_table_binding" / f"{phase}_{index}"
                create = self.query(
                    run_dir,
                    "create_table_binding",
                    f"CREATE TABLE {database}.{table} ({columns}) "
                    "ENGINE = MergeTree ORDER BY c00",
                    query_id=f"udt_bench_binding_{uuid.uuid4().hex}",
                )
                binding_ok = binding_ok and create.ok
                binding_runs.append(
                    {"phase": phase, "index": index, "create": create.summary()}
                )
                if table != "bound":
                    self.query(
                        run_dir,
                        "drop_temporary_bound_table",
                        f"DROP TABLE IF EXISTS {database}.{table} SYNC",
                    )
            binding_operation = {
                "operation": "create_table_binding",
                "ok": binding_ok,
                "runs": binding_runs,
            }
            atomic_json(
                scenario_dir / "create_table_binding" / "result.json",
                binding_operation,
            )
            operations.append(binding_operation)
            insert_bound = self.query(
                scenario_dir / "catalog_correctness",
                "insert_bound_canary",
                f"INSERT INTO {database}.bound VALUES "
                f"({', '.join(self._literal_for_family(index) for index in range(16))})",
            )

            redefine_runs: list[dict[str, Any]] = []
            redefine_ok = True
            for phase, index in phase_runs(self.args.warmups, self.args.measured):
                type_index = index if phase == "measured" else self.args.measured
                old_name = f"BenchmarkType{type_index:06d}"
                legacy_name = f"LegacyBenchmarkType{type_index:06d}"
                free_index = definitions - 1 - type_index
                run_dir = scenario_dir / "rename_and_redefine" / f"{phase}_{index}"
                sql = (
                    f"DROP TYPE {database}.BenchmarkType{free_index:06d} RESTRICT;\n"
                    f"ALTER TYPE {database}.{old_name} RENAME TO {legacy_name};\n"
                    f"CREATE TYPE {database}.{old_name} AS String;"
                )
                redefine = self.query(
                    run_dir,
                    "rename_and_redefine",
                    sql,
                    multiquery=True,
                    query_id=f"udt_bench_redefine_{uuid.uuid4().hex}",
                )
                redefine_ok = redefine_ok and redefine.ok
                redefine_runs.append(
                    {"phase": phase, "index": index, "query": redefine.summary()}
                )
            redefine_operation = {
                "operation": "rename_and_redefine",
                "ok": redefine_ok,
                "runs": redefine_runs,
            }
            atomic_json(
                scenario_dir / "rename_and_redefine" / "result.json",
                redefine_operation,
            )
            operations.append(redefine_operation)
            create_redefined = self.query(
                scenario_dir / "catalog_correctness",
                "create_redefined_canary",
                f"CREATE TABLE {database}.redefined "
                f"(value {database}.BenchmarkType000000) "
                "ENGINE = MergeTree ORDER BY value",
            )
            insert_redefined = self.query(
                scenario_dir / "catalog_correctness",
                "insert_redefined_canary",
                f"INSERT INTO {database}.redefined VALUES ('new-body')",
            )

            dry_runs: list[dict[str, Any]] = []
            apply_runs: list[dict[str, Any]] = []
            dry_ok = True
            apply_ok = True
            first_physicalization = True
            for phase, index in phase_runs(self.args.warmups, self.args.measured):
                run_dir = scenario_dir / "selective_drop_unused_dry_run" / f"{phase}_{index}"
                rebind: QueryRun | None = None
                if not first_physicalization:
                    # APPLY is destructive and its token is single-use.  Restore
                    # an equivalent mapped fixture before the next iteration so
                    # every dry run can be applied immediately; never accumulate
                    # outstanding tokens merely to manufacture repetitions.
                    restore_types = [
                        *(f"CREATE TYPE {database}.LegacyBenchmarkType{type_index:06d} AS "
                          f"{self._physical_type(type_index)};"
                          for type_index in range(4)),
                        *(f"CREATE TYPE {database}.BenchmarkType{type_index:06d} AS "
                          f"{self._physical_type(type_index)};"
                          for type_index in range(4, 16)),
                    ]
                    modifications = [
                        *(f"MODIFY COLUMN c{type_index:02d} "
                          f"{database}.LegacyBenchmarkType{type_index:06d}"
                          for type_index in range(4)),
                        *(f"MODIFY COLUMN c{type_index:02d} "
                          f"{database}.BenchmarkType{type_index:06d}"
                          for type_index in range(4, 16)),
                    ]
                    rebind = self.query(
                        run_dir,
                        "restore_equivalent_mapped_fixture",
                        "\n".join(restore_types)
                        + "\n"
                        + f"ALTER TABLE {database}.bound "
                        + ", ".join(modifications),
                        multiquery=True,
                    )
                first_physicalization = False
                dry = self.query(
                    run_dir,
                    "selective_drop_unused_dry_run",
                    f"PHYSICALIZE TYPE REFERENCES OBJECT TABLE {database}.bound "
                    "DROP UNUSED TYPES DRY RUN FORMAT JSONEachRow",
                    query_id=f"udt_bench_drop_dry_{uuid.uuid4().hex}",
                )
                parsed: dict[str, Any] | None = None
                try:
                    parsed = json.loads(dry.stdout.strip()) if dry.ok else None
                    valid = bool(
                        (rebind is None or rebind.ok)
                        and dry.ok
                        and parsed
                        and int(parsed["scope_count"]) == 1
                        and parsed.get("apply_token")
                    )
                except (ValueError, KeyError, TypeError):
                    valid = False
                dry_ok = dry_ok and valid
                apply = (
                    self.query(
                        run_dir,
                        "selective_drop_unused_apply",
                        "PHYSICALIZE TYPE REFERENCES APPLY TOKEN "
                        + sql_string(str(parsed["apply_token"])),
                        query_id=f"udt_bench_drop_apply_{uuid.uuid4().hex}",
                    )
                    if valid and parsed
                    else None
                )
                mapped_after = self.query(
                    run_dir,
                    "bound_mapping_count_after_apply",
                    "SELECT count() FROM system.columns "
                    f"WHERE database = {sql_string(database)} AND table = 'bound' "
                    "AND (udt_declared_type != '' OR notEmpty(udt_references))",
                )
                current_apply_ok = bool(
                    apply
                    and apply.ok
                    and mapped_after.ok
                    and mapped_after.stdout.strip() == "0"
                )
                apply_ok = apply_ok and current_apply_ok
                dry_runs.append(
                    {
                        "phase": phase,
                        "index": index,
                        "ok": valid,
                        "rebind": rebind.summary() if rebind else None,
                        "query": dry.summary(),
                        "scope_count": parsed.get("scope_count") if parsed else None,
                        "manifest_count": parsed.get("manifest_count") if parsed else None,
                        "manifest_bytes": (
                            len(base64.b64decode(parsed["canonical_loss_manifest_base64"], validate=True))
                            if parsed and parsed.get("canonical_loss_manifest_base64")
                            else None
                        ),
                    }
                )
                apply_runs.append(
                    {
                        "phase": phase,
                        "index": index,
                        "ok": current_apply_ok,
                        "query": apply.summary() if apply else None,
                        "mapped_columns_after": mapped_after.stdout.strip(),
                        "mapped_columns_query": mapped_after.summary(),
                    }
                )
            dry_operation = {
                "operation": "selective_drop_unused_dry_run",
                "ok": dry_ok,
                "runs": dry_runs,
            }
            atomic_json(
                scenario_dir / "selective_drop_unused_dry_run" / "result.json",
                dry_operation,
            )
            operations.append(dry_operation)

            apply_operation = {
                "operation": "selective_drop_unused_apply",
                "ok": apply_ok,
                "runs": apply_runs,
            }
            atomic_json(
                scenario_dir / "selective_drop_unused_apply" / "result.json",
                apply_operation,
            )
            operations.append(apply_operation)

            mapped_bound = self.query(
                scenario_dir / "catalog_correctness",
                "bound_mapping_count",
                "SELECT count() FROM system.columns "
                f"WHERE database = {sql_string(database)} AND table = 'bound' "
                "AND (udt_declared_type != '' OR notEmpty(udt_references))",
            )
            mapped_redefined = self.query(
                scenario_dir / "catalog_correctness",
                "redefined_mapping_count",
                "SELECT count() FROM system.columns "
                f"WHERE database = {sql_string(database)} AND table = 'redefined' "
                "AND udt_declared_type != ''",
            )
            bound_sum = self.query(
                scenario_dir / "catalog_correctness",
                "bound_data",
                f"SELECT sum(c00), count() FROM {database}.bound",
            )
            redefined_data = self.query(
                scenario_dir / "catalog_correctness",
                "redefined_data",
                f"SELECT value, count() FROM {database}.redefined GROUP BY value",
            )
            correctness = (
                insert_bound.ok
                and create_redefined.ok
                and insert_redefined.ok
                and mapped_bound.ok
                and mapped_bound.stdout.strip() == "0"
                and mapped_redefined.ok
                and mapped_redefined.stdout.strip() == "1"
                and bound_sum.ok
                and bound_sum.stdout.strip() == "1\t1"
                and redefined_data.ok
                and redefined_data.stdout.strip() == "new-body\t1"
            )
            success = correctness and all(operation["ok"] for operation in operations)
            return {
                "scenario": scenario.as_dict(),
                "status": "passed" if success else "failed",
                "resource_preflight": preflight,
                "operations": operations,
                "correctness": {
                    "ok": correctness,
                    "insert_bound": insert_bound.summary(),
                    "create_redefined": create_redefined.summary(),
                    "insert_redefined": insert_redefined.summary(),
                    "mapped_bound": mapped_bound.summary(),
                    "mapped_redefined": mapped_redefined.summary(),
                    "bound_data": bound_sum.summary(),
                    "redefined_data": redefined_data.summary(),
                },
            }
        finally:
            self._drop_database(scenario_dir, database)

    def _physical_setup_sql(self, database: str, table_count: int) -> str:
        columns = (
            f"id {database}.Base, "
            f"left_value {database}.Left DEFAULT [], "
            f"right_value {database}.Right DEFAULT tuple(0), "
            f"diamond_value {database}.Diamond DEFAULT tuple([], tuple(0)), "
            + ", ".join(f"c{index:02d} {database}.Base DEFAULT 0" for index in range(4, 16))
        )
        statements = [
            f"CREATE DATABASE {database} ENGINE = Atomic;",
            f"CREATE TYPE {database}.Base AS UInt64;",
            f"CREATE TYPE {database}.Left AS Array({database}.Base);",
            f"CREATE TYPE {database}.Right AS Tuple(value {database}.Base);",
            f"CREATE TYPE {database}.Diamond AS "
            f"Tuple(left {database}.Left, right {database}.Right);",
        ]
        statements.extend(
            f"CREATE TABLE {database}.t{index:05d} ({columns}) "
            "ENGINE = MergeTree ORDER BY id;"
            for index in range(table_count)
        )
        return "\n".join(statements)

    @staticmethod
    def _expected_physical_limit(stderr: str) -> bool:
        lowered = stderr.lower()
        return (
            "physicalization" in lowered
            and (
                "selected object" in lowered and "limit" in lowered
                or "scope" in lowered and "exceed" in lowered
                or "manifest" in lowered and "exceed" in lowered
            )
        )

    def run_physical(self, scenario: Scenario, scenario_dir: Path) -> dict[str, Any]:
        table_count = int(scenario.parameters["selected_tables"])
        estimated_bytes = self._estimated_compressed_table_bytes(1 << 20, 16)
        preflight = self._disk_preflight(
            scenario,
            estimated_table_bytes=estimated_bytes,
            estimated_inodes=table_count * 16 + 50_000,
        )
        atomic_json(scenario_dir / "resource-preflight.json", preflight)
        if not preflight["accepted"]:
            return {
                "scenario": scenario.as_dict(),
                "status": "resource_rejected",
                "resource_preflight": preflight,
                "iterations": [],
            }

        iterations: list[dict[str, Any]] = []
        success_count = 0
        expected_limit_count = 0
        failed_count = 0
        for phase, index in phase_runs(self.args.warmups, self.args.measured):
            database = f"udt_bench_physical_{uuid.uuid4().hex[:12]}"
            run_dir = scenario_dir / f"{phase}_{index}"
            setup = self.query(
                run_dir / "setup",
                "setup_physicalization_scope",
                self._physical_setup_sql(database, table_count),
                multiquery=True,
            )
            if not setup.ok:
                failed_count += 1
                iterations.append(
                    {
                        "phase": phase,
                        "index": index,
                        "status": "failed",
                        "failure": "setup",
                        "setup": setup.summary(),
                    }
                )
                self._drop_database(run_dir, database)
                continue
            try:
                populate = self.query(
                    run_dir / "setup",
                    "populate_canary",
                    f"INSERT INTO {database}.t00000 (id) "
                    f"SELECT number FROM numbers({1 << 20}) SETTINGS max_threads = 1",
                )
                parts_before = self.query(
                    run_dir / "correctness",
                    "parts_before",
                    "SELECT table, name, hash_of_all_files, hash_of_uncompressed_files, rows "
                    f"FROM system.parts WHERE database = {sql_string(database)} AND active "
                    "ORDER BY table, name FORMAT TSV",
                )
                data_before = self.query(
                    run_dir / "correctness",
                    "data_before",
                    f"SELECT count(), sum(id) FROM {database}.t00000",
                )
                dry = self.query(
                    run_dir / "dry_run",
                    "physicalization_dry_run",
                    f"PHYSICALIZE TYPE REFERENCES DATABASE {database} "
                    "DROP UNUSED TYPES DRY RUN FORMAT JSONEachRow",
                    query_id=f"udt_bench_physical_dry_{uuid.uuid4().hex}",
                )
                if not dry.ok:
                    expected_limit = (
                        table_count == PHYSICALIZATION_OBJECT_LIMIT
                        and self._expected_physical_limit(dry.stderr)
                    )
                    if expected_limit:
                        expected_limit_count += 1
                    else:
                        failed_count += 1
                    iterations.append(
                        {
                            "phase": phase,
                            "index": index,
                            "status": "limit_rejected" if expected_limit else "failed",
                            "setup": setup.summary(),
                            "populate": populate.summary(),
                            "dry_run": dry.summary(),
                            "reason": dry.stderr.strip(),
                        }
                    )
                    continue
                try:
                    plan = json.loads(dry.stdout.strip())
                    manifest = base64.b64decode(
                        plan["canonical_loss_manifest_base64"], validate=True
                    )
                    plan_ok = (
                        int(plan["scope_count"]) == table_count
                        and len(manifest) <= PHYSICALIZATION_MANIFEST_LIMIT_BYTES
                        and bool(plan["apply_token"])
                    )
                except (ValueError, KeyError, TypeError):
                    plan = {}
                    manifest = b""
                    plan_ok = False
                apply = (
                    self.query(
                        run_dir / "apply",
                        "physicalization_apply",
                        "PHYSICALIZE TYPE REFERENCES APPLY TOKEN "
                        + sql_string(str(plan["apply_token"])),
                        query_id=f"udt_bench_physical_apply_{uuid.uuid4().hex}",
                    )
                    if plan_ok
                    else None
                )
                parts_after = self.query(
                    run_dir / "correctness",
                    "parts_after",
                    "SELECT table, name, hash_of_all_files, hash_of_uncompressed_files, rows "
                    f"FROM system.parts WHERE database = {sql_string(database)} AND active "
                    "ORDER BY table, name FORMAT TSV",
                )
                data_after = self.query(
                    run_dir / "correctness",
                    "data_after",
                    f"SELECT count(), sum(id) FROM {database}.t00000",
                )
                mapped_after = self.query(
                    run_dir / "correctness",
                    "mapped_columns_after",
                    "SELECT count() FROM system.columns "
                    f"WHERE database = {sql_string(database)} "
                    "AND (udt_declared_type != '' OR notEmpty(udt_references))",
                )
                definitions_after = self.query(
                    run_dir / "correctness",
                    "definitions_after",
                    "SELECT count() FROM system.user_defined_types "
                    f"WHERE database = {sql_string(database)}",
                )
                correct = (
                    setup.ok
                    and populate.ok
                    and parts_before.ok
                    and data_before.ok
                    and plan_ok
                    and apply is not None
                    and apply.ok
                    and parts_after.ok
                    and parts_after.stdout == parts_before.stdout
                    and data_after.ok
                    and data_after.stdout == data_before.stdout
                    and mapped_after.ok
                    and mapped_after.stdout.strip() == "0"
                    and definitions_after.ok
                    and definitions_after.stdout.strip() == "0"
                )
                if correct:
                    success_count += 1
                else:
                    failed_count += 1
                iterations.append(
                    {
                        "phase": phase,
                        "index": index,
                        "status": "passed" if correct else "failed",
                        "setup": setup.summary(),
                        "populate": populate.summary(),
                        "dry_run": dry.summary(),
                        "apply": apply.summary() if apply else None,
                        "plan": {
                            "valid": plan_ok,
                            "scope_count": plan.get("scope_count"),
                            "manifest_count": plan.get("manifest_count"),
                            "manifest_bytes": len(manifest),
                        },
                        "correctness": {
                            "parts_unchanged": parts_after.stdout == parts_before.stdout,
                            "data_unchanged": data_after.stdout == data_before.stdout,
                            "mapped_columns": mapped_after.stdout.strip(),
                            "definitions": definitions_after.stdout.strip(),
                        },
                    }
                )
            finally:
                self._drop_database(run_dir, database)

        total_iterations = self.args.warmups + self.args.measured
        if success_count == total_iterations:
            status = "passed"
        elif (
            table_count == PHYSICALIZATION_OBJECT_LIMIT
            and expected_limit_count == total_iterations
            and failed_count == 0
        ):
            status = "limit_rejected"
        else:
            status = "failed"
        return {
            "scenario": scenario.as_dict(),
            "status": status,
            "resource_preflight": preflight,
            "success_iterations": success_count,
            "expected_limit_iterations": expected_limit_count,
            "failed_iterations": failed_count,
            "iterations": iterations,
        }

    def run_global_preflight(self) -> dict[str, Any]:
        """Reject a globally invalid harness before spending days on the matrix."""
        preflight_dir = self.output_dir / "preflight"
        database = f"udt_bench_preflight_{uuid.uuid4().hex[:12]}"
        query_results: dict[str, Any] = {}
        checks: dict[str, Any] = {}
        failure: str | None = None
        try:
            setup_sql = f"""CREATE DATABASE {database} ENGINE = Atomic;
CREATE TYPE {database}.PreflightUInt AS UInt64;
CREATE TYPE {database}.PreflightFixed AS FixedString(16);
CREATE TYPE {database}.PreflightNullable AS Nullable(UInt64);
CREATE TYPE {database}.PreflightLowCardinality AS LowCardinality(String);
CREATE TABLE {database}.pure (
    c0 {database}.PreflightUInt CODEC(ZSTD(1)),
    c1 {database}.PreflightFixed CODEC(ZSTD(1)),
    c2 {database}.PreflightNullable CODEC(ZSTD(1)),
    c3 {database}.PreflightLowCardinality CODEC(ZSTD(1))
) ENGINE = MergeTree ORDER BY c0;
CREATE TABLE {database}.mixed (
    c0 {database}.PreflightUInt CODEC(ZSTD(1)),
    c1 FixedString(16) CODEC(ZSTD(1)),
    c2 {database}.PreflightNullable CODEC(ZSTD(1)),
    c3 LowCardinality(String) CODEC(ZSTD(1))
) ENGINE = MergeTree ORDER BY c0;"""
            setup = self.query(
                preflight_dir / "setup", "create_preflight_fixture", setup_sql, multiquery=True
            )
            query_results["setup"] = setup.summary()

            schema = self.query(
                preflight_dir / "correctness",
                "schema_lowering",
                "SELECT table, name, type, udt_declared_type FROM system.columns "
                f"WHERE database = {sql_string(database)} AND table IN ('mixed', 'pure') "
                "ORDER BY table, position FORMAT TSV",
            )
            query_results["schema"] = schema.summary()
            observed_schema = [
                line.split("\t") for line in schema.stdout.splitlines() if line
            ]
            physical_types = (
                "UInt64",
                "FixedString(16)",
                "Nullable(UInt64)",
                "LowCardinality(String)",
            )
            expected_schema: list[list[str]] = []
            for table in ("mixed", "pure"):
                for index, physical_type in enumerate(physical_types):
                    mapped = table == "pure" or index in (0, 2)
                    expected_schema.append(
                        [
                            table,
                            f"c{index}",
                            physical_type,
                            (
                                f"{database}."
                                + (
                                    "PreflightUInt"
                                    if index == 0
                                    else "PreflightFixed"
                                    if index == 1
                                    else "PreflightNullable"
                                    if index == 2
                                    else "PreflightLowCardinality"
                                )
                                if mapped
                                else ""
                            ),
                        ]
                    )
            checks["schema_lowering"] = schema.ok and observed_schema == expected_schema

            insert_expression = (
                "toUInt64(number), toFixedString(toString(number), 16), "
                "if(number % 4 = 0, NULL, toUInt64(number)), "
                "toString(number % 4)"
            )
            populate = self.query(
                preflight_dir / "setup",
                "populate_preflight_fixture",
                f"INSERT INTO {database}.pure SELECT {insert_expression} FROM numbers(16);\n"
                f"INSERT INTO {database}.mixed SELECT {insert_expression} FROM numbers(16);",
                multiquery=True,
            )
            query_results["populate"] = populate.summary()

            mapped_before = self.query(
                preflight_dir / "correctness",
                "mapped_columns_before",
                "SELECT count() FROM system.columns "
                f"WHERE database = {sql_string(database)} "
                "AND (udt_declared_type != '' OR notEmpty(udt_references))",
            )
            query_results["mapped_before"] = mapped_before.summary()
            checks["mapped_before"] = mapped_before.ok and mapped_before.stdout.strip() == "6"

            parts_sql = (
                "SELECT table, name, hash_of_all_files, hash_of_uncompressed_files, rows "
                f"FROM system.parts WHERE database = {sql_string(database)} AND active "
                "ORDER BY table, name FORMAT TSV"
            )
            data_sql = (
                "SELECT table, count(), sum(c0), sum(cityHash64(c1)), "
                "sum(ifNull(c2, 0)), sum(cityHash64(c3)) FROM ("
                f"SELECT 'mixed' AS table, * FROM {database}.mixed UNION ALL "
                f"SELECT 'pure' AS table, * FROM {database}.pure) "
                "GROUP BY table ORDER BY table FORMAT TSV"
            )
            parts_before = self.query(
                preflight_dir / "correctness", "parts_before", parts_sql
            )
            data_before = self.query(
                preflight_dir / "correctness", "data_before", data_sql
            )
            query_results["parts_before"] = parts_before.summary()
            query_results["data_before"] = data_before.summary()

            measured = self.query(
                preflight_dir / "measured_query",
                "uniquely_identified_measured_query",
                f"SELECT count(), sum(c0) FROM {database}.pure",
                query_id=f"udt_bench_global_preflight_{uuid.uuid4().hex}",
            )
            query_results["measured_query"] = measured.summary()
            checks["measured_query_result"] = (
                measured.ok and measured.stdout.strip() == "16\t120"
            )
            checks["server_metrics_available"] = bool(
                measured.server_metrics and measured.server_metrics.get("available")
            )

            dry = self.query(
                preflight_dir / "physicalization",
                "physicalization_dry_run",
                f"PHYSICALIZE TYPE REFERENCES DATABASE {database} "
                "DRY RUN FORMAT JSONEachRow",
                query_id=f"udt_bench_global_preflight_dry_{uuid.uuid4().hex}",
            )
            query_results["dry_run"] = dry.summary()
            plan: dict[str, Any] | None = None
            manifest = b""
            try:
                plan = json.loads(dry.stdout.strip()) if dry.ok else None
                manifest = (
                    base64.b64decode(
                        plan["canonical_loss_manifest_base64"], validate=True
                    )
                    if plan
                    else b""
                )
                checks["dry_run_contract"] = bool(
                    plan
                    and int(plan["scope_count"]) == 2
                    and int(plan["manifest_count"]) >= 2
                    and plan.get("apply_token")
                    and manifest
                    and base64.b64encode(manifest).decode("ascii")
                    == plan["canonical_loss_manifest_base64"]
                    and "canonical_loss_manifest" not in plan
                )
            except (KeyError, TypeError, ValueError):
                checks["dry_run_contract"] = False

            apply = (
                self.query(
                    preflight_dir / "physicalization",
                    "physicalization_apply",
                    "PHYSICALIZE TYPE REFERENCES APPLY TOKEN "
                    + sql_string(str(plan["apply_token"])),
                    query_id=f"udt_bench_global_preflight_apply_{uuid.uuid4().hex}",
                )
                if checks["dry_run_contract"] and plan
                else None
            )
            query_results["apply"] = apply.summary() if apply else None

            parts_after = self.query(
                preflight_dir / "correctness", "parts_after", parts_sql
            )
            data_after = self.query(
                preflight_dir / "correctness", "data_after", data_sql
            )
            mapped_after = self.query(
                preflight_dir / "correctness",
                "mapped_columns_after",
                "SELECT count() FROM system.columns "
                f"WHERE database = {sql_string(database)} "
                "AND (udt_declared_type != '' OR notEmpty(udt_references))",
            )
            definitions_after = self.query(
                preflight_dir / "correctness",
                "definitions_after",
                "SELECT count() FROM system.user_defined_types "
                f"WHERE database = {sql_string(database)}",
            )
            query_results["parts_after"] = parts_after.summary()
            query_results["data_after"] = data_after.summary()
            query_results["mapped_after"] = mapped_after.summary()
            query_results["definitions_after"] = definitions_after.summary()
            checks["parts_unchanged"] = (
                parts_before.ok
                and parts_after.ok
                and parts_before.stdout == parts_after.stdout
            )
            checks["data_unchanged"] = (
                data_before.ok
                and data_after.ok
                and data_before.stdout == data_after.stdout
            )
            checks["mappings_removed"] = (
                mapped_after.ok and mapped_after.stdout.strip() == "0"
            )
            checks["definitions_retained"] = (
                definitions_after.ok and definitions_after.stdout.strip() == "4"
            )
            checks["apply_succeeded"] = bool(apply and apply.ok)
            checks["setup_and_insert_succeeded"] = setup.ok and populate.ok
        except BaseException as exception:
            failure = repr(exception)
        finally:
            cleanup = self._drop_database(preflight_dir, database)
            query_results["cleanup"] = cleanup.summary()
            checks["drop_database_sync"] = cleanup.ok

        passed = failure is None and bool(checks) and all(checks.values())
        result = {
            "status": "passed" if passed else "failed",
            "database": database,
            "row_count_per_table": 16,
            "checks": checks,
            "queries": query_results,
            "failure": failure,
            "finished_at": utc_now(),
        }
        atomic_json(preflight_dir / "result.json", result)
        return result

    def run_scenario(self, scenario: Scenario) -> dict[str, Any]:
        scenario_dir = self.scenarios_dir / scenario.scenario_id
        scenario_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(scenario_dir / "scenario.json", scenario.as_dict())
        started = time.monotonic()
        try:
            if scenario.family == "row":
                result = self.run_row(scenario, scenario_dir)
            elif scenario.family == "width":
                result = self.run_width(scenario, scenario_dir)
            elif scenario.family == "catalog":
                result = self.run_catalog(scenario, scenario_dir)
            elif scenario.family == "physical":
                result = self.run_physical(scenario, scenario_dir)
            else:
                raise AssertionError(f"unknown scenario family: {scenario.family}")
        except BaseException as exception:  # preserve failure and continue independent scenarios.
            result = {
                "scenario": scenario.as_dict(),
                "status": "failed",
                "uncaught_exception": repr(exception),
            }
            (scenario_dir / "uncaught.stderr").write_text(
                repr(exception) + "\n", encoding="utf-8"
            )
        result["elapsed_seconds"] = time.monotonic() - started
        result["finished_at"] = utc_now()
        atomic_json(scenario_dir / "result.json", result)
        return result

    def verify_release(self) -> dict[str, Any]:
        run = self.query(
            self.output_dir / "provenance",
            "build_provenance",
            "SELECT name, value FROM system.build_options "
            "WHERE name IN ('CMAKE_BUILD_TYPE', 'VERSION_FULL') ORDER BY name FORMAT TSV",
        )
        values: dict[str, str] = {}
        if run.ok:
            for line in run.stdout.splitlines():
                if "\t" in line:
                    name, value = line.split("\t", 1)
                    values[name] = value
        build_type = values.get("CMAKE_BUILD_TYPE", "")
        accepted = run.ok and (
            build_type == "Release" or self.args.allow_non_release
        )
        result = {
            "query": run.summary(),
            "build_options": values,
            "required_build_type": "Release",
            "allow_non_release": self.args.allow_non_release,
            "accepted": accepted,
        }
        atomic_json(self.output_dir / "provenance.json", result)
        if not accepted:
            raise RuntimeError(
                f"benchmark requires a Release binary; observed CMAKE_BUILD_TYPE={build_type!r}"
            )
        return result

    def execute(self) -> int:
        results: list[dict[str, Any]] = []
        try:
            self.prepare()
            os.sched_setaffinity(0, {self.args.controller_cpu})
            startup_seconds = self.start_server()
            self.status.update(
                {
                    "state": "running",
                    "server_startup_seconds": startup_seconds,
                }
            )
            self._write_status()
            provenance = self.verify_release()
            preflight = self.run_global_preflight()
            if preflight["status"] != "passed":
                raise RuntimeError(
                    "global UDT benchmark preflight failed; scenarios were not started"
                )
            for scenario in self.scenarios:
                self.status["current"] = scenario.scenario_id
                self._write_status()
                result = self.run_scenario(scenario)
                results.append(result)
                self.status["completed"] += 1
                if result["status"] == "failed":
                    self.status["failed"] += 1
                elif result["status"] == "resource_rejected":
                    self.status["resource_rejected"] += 1
                elif result["status"] == "limit_rejected":
                    self.status["limit_rejected"] += 1
                self._write_status()
            self.status["current"] = None
            self.status["finished_at"] = utc_now()
            self.status["state"] = "failed" if self.status["failed"] else "finished"
            summary = {
                "status": self.status,
                "provenance": provenance,
                "preflight": preflight,
                "results": [
                    {
                        "scenario_id": result["scenario"]["scenario_id"],
                        "family": result["scenario"]["family"],
                        "status": result["status"],
                        "elapsed_seconds": result["elapsed_seconds"],
                    }
                    for result in results
                ],
            }
            atomic_json(self.output_dir / "summary.json", summary)
            self._write_status()
            return 1 if self.status["failed"] else 0
        except BaseException as exception:
            self.status.update(
                {
                    "state": "failed",
                    "current": None,
                    "fatal_error": repr(exception),
                    "finished_at": utc_now(),
                }
            )
            atomic_json(
                self.output_dir / "fatal.json",
                {"error": repr(exception), "finished_at": self.status["finished_at"]},
            )
            self._write_status()
            return 2
        finally:
            self.stop_server()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-only", action="store_true", help="print the 36-scenario JSON plan and exit")
    parser.add_argument("--clickhouse", help="path to the Release clickhouse binary")
    parser.add_argument("--output-dir", help="fresh artifact directory for execute mode")
    parser.add_argument("--data-root", help="fresh private server root (defaults below output-dir)")
    parser.add_argument("--family", action="append", choices=("row", "width", "catalog", "physical"))
    parser.add_argument("--scenario", action="append", help="exact scenario id; repeat to select several")
    parser.add_argument("--warmups", type=int, default=WARMUP_RUNS)
    parser.add_argument("--measured", type=int, default=MEASURED_RUNS)
    parser.add_argument("--query-timeout", type=int, default=21_600)
    parser.add_argument("--startup-timeout", type=int, default=120)
    parser.add_argument("--benchmark-cpu", type=int, default=DEFAULT_BENCHMARK_CPU)
    parser.add_argument("--controller-cpu", type=int, default=DEFAULT_CONTROLLER_CPU)
    parser.add_argument(
        "--max-estimated-table-bytes",
        type=int,
        default=DEFAULT_MAX_ESTIMATED_TABLE_BYTES,
        help="safety ceiling; a larger point becomes an explicit resource rejection",
    )
    parser.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    parser.add_argument("--min-free-inodes", type=int, default=DEFAULT_MIN_FREE_INODES)
    parser.add_argument(
        "--allow-non-release",
        action="store_true",
        help="infrastructure debugging only; results are not performance evidence",
    )
    args = parser.parse_args(argv)
    if args.warmups < 0 or args.measured <= 0:
        parser.error("--warmups must be non-negative and --measured must be positive")
    if args.query_timeout <= 0 or args.startup_timeout <= 0:
        parser.error("timeouts must be positive")
    if args.benchmark_cpu < 0 or args.controller_cpu < 0:
        parser.error("CPU identifiers must be non-negative")
    if args.benchmark_cpu == args.controller_cpu:
        parser.error("--benchmark-cpu and --controller-cpu must be different")
    if min(
        args.max_estimated_table_bytes,
        args.min_free_bytes,
        args.min_free_inodes,
    ) < 0:
        parser.error("resource safety values must be non-negative")
    if not args.plan_only and (not args.clickhouse or not args.output_dir):
        parser.error("execute mode requires --clickhouse and --output-dir")
    return args


def select_scenarios(args: argparse.Namespace, scenarios: Sequence[Scenario]) -> list[Scenario]:
    selected = list(scenarios)
    if args.family:
        families = set(args.family)
        selected = [scenario for scenario in selected if scenario.family in families]
    if args.scenario:
        identifiers = set(args.scenario)
        known = {scenario.scenario_id for scenario in scenarios}
        unknown = sorted(identifiers - known)
        if unknown:
            raise ValueError(f"unknown scenario ids: {', '.join(unknown)}")
        selected = [scenario for scenario in selected if scenario.scenario_id in identifiers]
    if not selected:
        raise ValueError("scenario selection is empty")
    return selected


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    all_scenarios = build_plan()
    try:
        scenarios = select_scenarios(args, all_scenarios)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.plan_only:
        print(
            json.dumps(
                plan_document(
                    scenarios,
                    warmups=args.warmups,
                    measured=args.measured,
                    benchmark_cpu=args.benchmark_cpu,
                    controller_cpu=args.controller_cpu,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    return Campaign(args, scenarios).execute()


if __name__ == "__main__":
    raise SystemExit(main())
