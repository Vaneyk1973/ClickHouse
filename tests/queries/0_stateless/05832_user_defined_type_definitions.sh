#!/usr/bin/env bash

CUR_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../shell_config.sh
. "$CUR_DIR"/../shell_config.sh

set -e

type_name="udt_lite_${CLICKHOUSE_DATABASE}"
invalid_type="${type_name}_invalid"
other_db="udt_db_${CLICKHOUSE_DATABASE}"
type_user="${type_name}_user"
local_dir=$(mktemp -d "${CLICKHOUSE_TMP}/udt-lite.XXXXXX")

function cleanup()
{
    rm -rf -- "${local_dir:?}"
    ${CLICKHOUSE_CLIENT} --multiquery --query "
        SET ast_fuzzer_any_query = 0, ignore_drop_queries_probability = 0;
        DROP TYPE IF EXISTS ${type_name};
        DROP TYPE IF EXISTS ${invalid_type};
        DROP USER IF EXISTS ${type_user};
        DROP DATABASE IF EXISTS ${other_db} SYNC;
    "
}
trap cleanup EXIT

${CLICKHOUSE_CLIENT} --multiquery --query "
    SET async_insert = 0;
    CREATE TYPE ${type_name} AS Int64;
    CREATE TYPE ${invalid_type} AS ${type_name}; -- { serverError BAD_ARGUMENTS }
    CREATE TYPE Int64 AS String; -- { serverError BAD_ARGUMENTS }
    CREATE TYPE Integer AS String; -- { serverError BAD_ARGUMENTS }
    CREATE TYPE FixedString AS String; -- { serverError BAD_ARGUMENTS }

    CREATE DATABASE ${other_db} ENGINE = Atomic;
    USE ${other_db};
    CREATE TYPE ${type_name} AS UInt64; -- { serverError BAD_ARGUMENTS }

    SET data_type_default_nullable = 1;
    CREATE TABLE original (value ${type_name}) ENGINE = MergeTree ORDER BY value;
    SET data_type_default_nullable = 0;
    INSERT INTO original VALUES (-8);

    CREATE TABLE defaults (id UInt64, input_value ${type_name} EPHEMERAL, stored Int64 DEFAULT input_value) ENGINE = Memory;
    INSERT INTO defaults (id) VALUES (1);
    INSERT INTO defaults (id, input_value) VALUES (2, 7);
    SELECT id, stored FROM defaults ORDER BY id;

    CREATE TABLE nested_shape (value Array(${type_name})) ENGINE = Memory; -- { serverError UNKNOWN_TYPE }
    CREATE TABLE nullable_shape (value ${type_name} NULL) ENGINE = Memory; -- { serverError ILLEGAL_SYNTAX_FOR_DATA_TYPE }
    ALTER TABLE original ADD COLUMN other ${type_name}; -- { serverError UNKNOWN_TYPE }
    SELECT CAST(1 AS ${type_name}); -- { serverError UNKNOWN_TYPE }
    SELECT * FROM format('TSV', 'value ${type_name}', '1'); -- { serverError UNKNOWN_TYPE }

    CREATE OR REPLACE TYPE ${type_name} AS String;
    CREATE OR REPLACE TYPE ${type_name} AS Int32; -- { serverError BAD_ARGUMENTS }
    CREATE TABLE replacement (value ${type_name}) ENGINE = Memory;
    INSERT INTO replacement VALUES ('replacement');

    CREATE USER ${type_user};
    GRANT TABLE ENGINE ON Memory TO ${type_user};
"

${CLICKHOUSE_CLIENT} --user "${type_user}" --multiquery --query "
    CREATE TABLE ${other_db}.restricted_use (value ${type_name} NULL) ENGINE = Memory; -- { serverError ACCESS_DENIED }
"

${CLICKHOUSE_CLIENT} --query "GRANT CREATE TABLE ON ${other_db}.* TO ${type_user}"
${CLICKHOUSE_CLIENT} --user "${type_user}" --multiquery --query "
    CREATE TABLE ${other_db}.restricted_use (value ${type_name}) ENGINE = Memory;
    SELECT * FROM ${other_db}.original; -- { serverError ACCESS_DENIED }
    CREATE TYPE ${invalid_type} AS Int64; -- { serverError ACCESS_DENIED }
    DROP TYPE ${type_name}; -- { serverError ACCESS_DENIED }
"

${CLICKHOUSE_CLIENT} --query "GRANT CREATE TYPE ON *.* TO ${type_user}"
${CLICKHOUSE_CLIENT} --user "${type_user}" --multiquery --query "
    CREATE TYPE ${invalid_type} AS Int64;
    CREATE OR REPLACE TYPE ${type_name} AS UInt64; -- { serverError ACCESS_DENIED }
"

${CLICKHOUSE_CLIENT} --query "GRANT DROP TYPE ON *.* TO ${type_user}"
${CLICKHOUSE_CLIENT} --user "${type_user}" --multiquery --query "
    SET ignore_drop_queries_probability = 0;
    DROP TYPE ${invalid_type};
"

${CLICKHOUSE_CLIENT} --multiquery --query "
    SET async_insert = 0, ignore_drop_queries_probability = 0;
    USE ${other_db};
    DROP TYPE ${type_name};
    DROP TYPE ${type_name}; -- { serverError UNKNOWN_TYPE }
    DROP TYPE IF EXISTS ${type_name};
    CREATE TABLE missing_type (value ${type_name}) ENGINE = Memory; -- { serverError UNKNOWN_TYPE }

    SELECT position(create_table_query, 'Int64') > 0 AND position(create_table_query, '${type_name}') = 0
    FROM system.tables WHERE database = currentDatabase() AND name = 'original';
    DETACH TABLE original;
    ATTACH TABLE original;
    INSERT INTO original VALUES (3);
    SELECT toTypeName(value), value FROM original ORDER BY value;
    SELECT toTypeName(value), value FROM replacement;

    CREATE TYPE ${type_name} AS UInt64;
    CREATE TABLE recreated (value ${type_name}) ENGINE = Memory;
    INSERT INTO recreated VALUES (18446744073709551615);
    SELECT toTypeName(value), value FROM recreated;
"

${CLICKHOUSE_LOCAL} --path "${local_dir}" --query "CREATE TYPE persisted_lite_type AS UInt64"
${CLICKHOUSE_LOCAL} --path "${local_dir}" --query "
    CREATE TABLE after_restart (value persisted_lite_type) ENGINE = Memory;
    SELECT type FROM system.columns WHERE database = currentDatabase() AND table = 'after_restart';
"
