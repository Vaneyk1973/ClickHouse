#!/usr/bin/env bash
# Tags: zookeeper, no-replicated-database
# Tag no-replicated-database: `ON CLUSTER` is not allowed for a `Replicated` database.

CUR_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../shell_config.sh
. "$CUR_DIR"/../shell_config.sh

set -e

run_id=$(${CLICKHOUSE_CLIENT} --query 'SELECT hex(generateUUIDv4()) FORMAT TabSeparated')
type_name="udt_legacy_type_${CLICKHOUSE_DATABASE}_${run_id}"
table_name="udt_legacy_table_${CLICKHOUSE_DATABASE}_${run_id}"

function cleanup()
{
    ${CLICKHOUSE_CLIENT} --multiquery --query "
        SET ast_fuzzer_any_query = 0, ignore_drop_queries_probability = 0;
        DROP TYPE IF EXISTS ${type_name};
        DROP TABLE IF EXISTS ${table_name} SYNC;
    "
}
trap cleanup EXIT

${CLICKHOUSE_CLIENT} --multiquery --query "
    SET distributed_ddl_entry_format_version = 1, distributed_ddl_output_mode = 'throw';
    CREATE TYPE ${type_name} AS UInt64;
    CREATE TABLE ${CLICKHOUSE_DATABASE}.${table_name} ON CLUSTER test_shard_localhost (value ${type_name}) ENGINE = Memory FORMAT Null;

    SELECT count(), countIf(position(query, 'UInt64') > 0 AND position(query, '${type_name}') = 0)
    FROM system.distributed_ddl_queue
    WHERE entry_version = 1 AND cluster = 'test_shard_localhost'
        AND startsWith(query, 'CREATE TABLE ') AND position(query, '${table_name}') > 0;

    SELECT type FROM system.columns WHERE database = currentDatabase() AND table = '${table_name}' AND name = 'value';
"
