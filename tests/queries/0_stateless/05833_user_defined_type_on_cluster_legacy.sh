#!/usr/bin/env bash
# Tags: zookeeper, no-replicated-database
# Tag no-replicated-database: `ON CLUSTER` is not allowed for a `Replicated` database.

CUR_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../shell_config.sh
. "$CUR_DIR"/../shell_config.sh

set -e

run_id=$(${CLICKHOUSE_CLIENT} --query 'SELECT hex(generateUUIDv4()) FORMAT TabSeparated')
type_name="udt_legacy_type_${CLICKHOUSE_DATABASE}_${run_id}"
checked_type_name="udt_legacy_checked_${CLICKHOUSE_DATABASE}_${run_id}"
table_name="udt_legacy_table_${CLICKHOUSE_DATABASE}_${run_id}"

function cleanup()
{
    ${CLICKHOUSE_CLIENT} --multiquery --query "
        SET ast_fuzzer_any_query = 0, ignore_drop_queries_probability = 0;
        DROP TYPE IF EXISTS ${type_name};
        DROP TYPE IF EXISTS ${checked_type_name};
        DROP TABLE IF EXISTS ${table_name} SYNC;
    "
}
trap cleanup EXIT

${CLICKHOUSE_CLIENT} --multiquery --query "
    SET distributed_ddl_entry_format_version = 1, distributed_ddl_output_mode = 'throw',
        default_table_engine = 'Memory', async_insert = 0, ignore_drop_queries_probability = 0;
    CREATE TYPE ${type_name} AS UInt64;
    CREATE TYPE ${checked_type_name} AS Int64 CHECK VALUE > 0;
    CREATE TABLE ${CLICKHOUSE_DATABASE}.${table_name}_conflict ON CLUSTER test_shard_localhost
        (positive ${checked_type_name}, \`greater(positive, 0)\` UInt8) FORMAT Null; -- { serverError AMBIGUOUS_COLUMN_NAME }
    CREATE TABLE ${CLICKHOUSE_DATABASE}.${table_name} ON CLUSTER test_shard_localhost
        (value ${type_name}, positive ${checked_type_name}) FORMAT Null;

    SELECT count(), countIf(position(query, ' UInt64') > 0 AND position(query, ' Int64') > 0
        AND position(query, ' CHECK positive > 0') > 0 AND position(query, 'ENGINE = Memory') > 0
        AND position(query, '${type_name}') = 0 AND position(query, '${checked_type_name}') = 0)
    FROM system.distributed_ddl_queue
    WHERE entry_version = 1 AND cluster = 'test_shard_localhost'
        AND startsWith(query, 'CREATE TABLE ') AND position(query, '${table_name}') > 0;

    SELECT type FROM system.columns WHERE database = currentDatabase() AND table = '${table_name}' ORDER BY position;

    DROP TYPE ${type_name};
    DROP TYPE ${checked_type_name};
    INSERT INTO ${table_name} VALUES (1, 2);
    INSERT INTO ${table_name} VALUES (1, 0); -- { serverError VIOLATED_CONSTRAINT }
    SELECT value, positive FROM ${table_name};
"
