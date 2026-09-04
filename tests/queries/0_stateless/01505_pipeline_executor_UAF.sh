#!/usr/bin/env bash
# Tags: long

CURDIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../shell_config.sh
. "$CURDIR"/../shell_config.sh

# Avoid failures due to "Number of CPUs is not deterministic" error from jemalloc.
export MALLOC_CONF=abort_conf:false,abort:false,percpu_arena:disabled

# Respect a restricted cpuset (for example, sanitizer test containers).
allowed_cpu=$(awk '/^Cpus_allowed_list:/ { split($2, cpus, /[,-]/); print cpus[1] }' /proc/self/status)

# Regression for UAF in ThreadPool.
# (Triggered under TSAN)
for _ in {1..10}; do
    ${CLICKHOUSE_LOCAL} -q 'select * from numbers_mt(100000000) settings max_threads=100 FORMAT Null'
    # Binding to specific CPU is not required, but this makes the test more reliable.
    taskset --cpu-list "${allowed_cpu}" ${CLICKHOUSE_LOCAL} -q 'select * from numbers_mt(100000000) settings max_threads=100 FORMAT Null' 2>&1 | {
        # build with sanitiziers does not have jemalloc
        # and for jemalloc we have separate test
        # 01502_jemalloc_percpu_arena
        grep -v '<jemalloc>: Number of CPUs detected is not deterministic. Per-CPU arena disabled.'
    }
done
exit 0
