#!/usr/bin/env bash

CUR_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../shell_config.sh
. "$CUR_DIR"/../shell_config.sh

set -e

work_dir=$(mktemp -d "${CLICKHOUSE_TMP}/udt-check-validation.XXXXXX")
trap 'rm -rf -- "${work_dir:?}"' EXIT

${CLICKHOUSE_LOCAL} --path "${work_dir}/source" --multiquery <<'SQL'
    SET async_insert = 0;
    CREATE DATABASE checked ENGINE = Atomic;
    USE checked;

    CREATE TYPE LiteCityName AS String CHECK match(VALUE, '^[A-Z][a-z]{4,}$');
    CREATE TYPE LiteInvalidPattern AS String CHECK match(VALUE, '['); -- { serverError CANNOT_COMPILE_REGEXP }
    CREATE TYPE LiteInvalidPattern AS String CHECK like(VALUE, 'a%', 'ab'); -- { serverError BAD_ARGUMENTS }
    DROP TYPE LiteInvalidPattern; -- { serverError UNKNOWN_TYPE }
    CREATE OR REPLACE TYPE LiteCityName AS String
        CHECK length(VALUE) > 10
        CHECK if(length(VALUE) > 20, match(VALUE, concat('[', '')), true); -- { serverError CANNOT_COMPILE_REGEXP }
    CREATE OR REPLACE TYPE LiteCityName AS String
        CHECK if(0, match(VALUE, concat('[', '')), true); -- { serverError CANNOT_COMPILE_REGEXP }
    CREATE OR REPLACE TYPE LiteCityName AS String
        CHECK length(VALUE) > 10
        CHECK like(VALUE, 'a%', concat('a', 'b')); -- { serverError BAD_ARGUMENTS }
    CREATE OR REPLACE TYPE LiteCityName AS String
        CHECK ilike(VALUE, concat('a|', 'q'), '|'); -- { serverError CANNOT_PARSE_ESCAPE_SEQUENCE }
    CREATE OR REPLACE TYPE LiteCityName AS String
        CHECK notLike(VALUE, concat('a', x'5C')); -- { serverError CANNOT_PARSE_ESCAPE_SEQUENCE }

    CREATE TYPE LitePattern AS String CHECK match('Paris', VALUE) CHECK like(VALUE, if(1, '%', VALUE));
    CREATE TABLE row_patterns (pattern LitePattern) ENGINE = Memory;
    INSERT INTO row_patterns VALUES ('^Par');
    INSERT INTO row_patterns VALUES ('^Rom'); -- { serverError VIOLATED_CONSTRAINT }

    CREATE TABLE old_check (city LiteCityName) ENGINE = Memory;
    INSERT INTO old_check VALUES ('Paris');
    INSERT INTO old_check VALUES ('Rome'); -- { serverError VIOLATED_CONSTRAINT }
    SELECT city FROM old_check;

    CREATE TABLE quoted_city (id UInt8, ":`city`" LiteCityName, details JSON) ENGINE = MergeTree ORDER BY id;
    INSERT INTO quoted_city VALUES (1, 'Paris', '{"city":"Rome"}');
    DROP TYPE LiteCityName;

    CREATE TYPE LitePositive AS Int64 CHECK VALUE > 0;
    CREATE TYPE LiteShifted AS Int64 CHECK VALUE + 1 > 1;
    CREATE TABLE ambiguous_result (x LitePositive, "greater(x, 0)" String) ENGINE = Memory; -- { serverError AMBIGUOUS_COLUMN_NAME }
    CREATE TABLE ambiguous_reversed ("greater(x, 0)" UInt8, x LitePositive) ENGINE = Memory; -- { serverError AMBIGUOUS_COLUMN_NAME }
    SET flatten_nested = 1;
    CREATE TABLE ambiguous_flattened ("x.a" LitePositive, "greater(x" Nested("a, 0)" Int64)) ENGINE = Memory; -- { serverError AMBIGUOUS_COLUMN_NAME }
    CREATE TABLE intermediate_name (x LiteShifted, "plus(x, 1)" Int64 DEFAULT 2) ENGINE = Memory;
    INSERT INTO intermediate_name (x) VALUES (1);
    SELECT x FROM intermediate_name;

    CREATE TABLE unambiguous (x LitePositive, "greater(y, 0)" UInt8) ENGINE = Memory;
    INSERT INTO unambiguous VALUES (1, 0);
    INSERT INTO unambiguous VALUES (0, 1); -- { serverError VIOLATED_CONSTRAINT }
    SELECT x, x > 0 FROM unambiguous;

    CREATE TABLE ordinary_columns (x Int64, "greater(x, 0)" UInt8) ENGINE = Memory;
    INSERT INTO ordinary_columns VALUES (1, 0);
    SELECT x, "greater(x, 0)" FROM ordinary_columns;
SQL

${CLICKHOUSE_LOCAL} --path "${work_dir}/source" --multiquery <<'SQL'
    SET async_insert = 0;
    USE checked;
    SELECT count() FROM system.tables WHERE database = currentDatabase() AND startsWith(name, 'ambiguous_');
    DETACH TABLE quoted_city;
    ATTACH TABLE quoted_city;
    INSERT INTO quoted_city VALUES (2, 'Rome', '{"city":"Paris"}'); -- { serverError VIOLATED_CONSTRAINT }
    SELECT ":`city`", details.city.:String FROM quoted_city;
SQL

ddl=$(${CLICKHOUSE_LOCAL} --path "${work_dir}/source" --database checked --query 'SHOW CREATE TABLE quoted_city FORMAT TSVRaw')
{
    printf 'SET async_insert = 0; CREATE DATABASE checked ENGINE = Atomic; USE checked;\n%s;\n' "$ddl"
    cat <<'SQL'
    INSERT INTO quoted_city VALUES (1, 'Lisbon', '{"city":"Oslo"}');
    INSERT INTO quoted_city VALUES (2, 'Oslo', '{"city":"Lisbon"}'); -- { serverError VIOLATED_CONSTRAINT }
    SELECT ":`city`", details.city.:String FROM quoted_city;
SQL
} | ${CLICKHOUSE_LOCAL} --path "${work_dir}/target" --multiquery
