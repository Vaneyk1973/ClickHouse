#!/usr/bin/env bash

CUR_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../shell_config.sh
. "$CUR_DIR"/../shell_config.sh

set -e

work_dir=$(mktemp -d "${CLICKHOUSE_TMP}/udt-checks.XXXXXX")
trap 'rm -rf -- "${work_dir:?}"' EXIT
printf '3\t4\t5\timported\t281474976710655\t-8\n' > "${work_dir}/valid.tsv"
printf '8\t4\t5\timported\t281474976710656\t-8\n' > "${work_dir}/invalid.tsv"

${CLICKHOUSE_LOCAL} --path "${work_dir}/source" --multiquery <<SQL
    SET async_insert = 0;
    CREATE DATABASE checked ENGINE = Atomic;
    USE checked;
    CREATE TYPE LitePositiveInt AS Int64
        CHECK VALUE > 0
        CHECK if(vAlUe % 2 = 0, VALUE <= 40, VALUE <= 39);
    CREATE TYPE LiteNonEmptyString AS String CHECK notEmpty(VALUE);
    CREATE TYPE LiteMac48 AS UInt64 CHECK VALUE < 281474976710656;
    CREATE TYPE LiteComputedEscape AS String CHECK like(VALUE, 'a|_%', concat('|', ''));
    CREATE TABLE checked_escape (x LiteComputedEscape) ENGINE = Memory;
    INSERT INTO checked_escape VALUES ('a_b');
    INSERT INTO checked_escape VALUES ('axb'); -- { serverError VIOLATED_CONSTRAINT }

    CREATE FUNCTION lite_checked_udf AS x -> x > 0;
    CREATE TYPE LiteInvalid AS Int64 CHECK other_column > 0; -- { serverError BAD_ARGUMENTS }
    CREATE TYPE LiteInvalid AS Int64 CHECK VALUE IN (SELECT number FROM numbers(1)); -- { serverError BAD_ARGUMENTS }
    CREATE TYPE LiteInvalid AS Int64 CHECK lite_checked_udf(VALUE); -- { serverError BAD_ARGUMENTS }
    CREATE TYPE LiteInvalid AS Int64 CHECK dictHas('unused_dictionary', VALUE); -- { serverError BAD_ARGUMENTS }
    CREATE TYPE LiteInvalid AS Int64 CHECK now() > toDateTime(0); -- { serverError BAD_ARGUMENTS }
    CREATE TYPE LiteInvalid AS Int64 CHECK rand() > 0; -- { serverError BAD_ARGUMENTS }
    CREATE TYPE LiteInvalid AS Int64 CHECK sum(VALUE) > 0; -- { serverError BAD_ARGUMENTS }
    SET enable_named_columns_in_function_tuple = 1;
    CREATE TYPE LiteInvalid AS Int64 CHECK tupleElement(tuple(VALUE), 'VALUE') > 0; -- { serverError BAD_ARGUMENTS }
    CREATE TYPE LiteInvalid AS Int64 CHECK VALUE; -- { serverError BAD_ARGUMENTS }
    SET param_lite_bound = 4;
    CREATE TYPE LiteInvalid AS Int64 CHECK VALUE > {lite_bound:Int64}; -- { serverError BAD_ARGUMENTS }
    CREATE OR REPLACE TYPE LitePositiveInt AS Int64 CHECK VALUE > 10 CHECK other_column > 0; -- { serverError BAD_ARGUMENTS }

    CREATE TABLE alias_column (id Int64, x LitePositiveInt ALIAS id) ENGINE = Memory; -- { serverError BAD_ARGUMENTS }
    CREATE TABLE ephemeral_column (x LitePositiveInt EPHEMERAL, id Int64 DEFAULT x) ENGINE = Memory; -- { serverError BAD_ARGUMENTS }
    CREATE TABLE unsupported_engine (x LitePositiveInt) ENGINE = Distributed('unused', currentDatabase(), 'unused'); -- { serverError NOT_IMPLEMENTED }

    CREATE TABLE materialized_column (x Int64, positive LitePositiveInt MATERIALIZED x + 1) ENGINE = Memory;
    INSERT INTO materialized_column (x) VALUES (0);
    INSERT INTO materialized_column (x) VALUES (-1); -- { serverError VIOLATED_CONSTRAINT }
    SELECT x, positive FROM materialized_column;

    CREATE TABLE checked_events (
        id UInt64,
        positive LitePositiveInt,
        \`right.score\` LitePositiveInt,
        note LiteNonEmptyString DEFAULT 'seed',
        mac LiteMac48,
        ordinary Int64,
        CONSTRAINT udt_positive_check_1 CHECK id > 0
    ) ENGINE = MergeTree ORDER BY id;

    INSERT INTO checked_events VALUES (1, 2, 3, 'abc', 1, -7);
    INSERT INTO checked_events SELECT 2, toUInt16(3), toUInt8(4), 'xy', 2, -9;
    INSERT INTO checked_events SELECT 8, 0, 4, 'xy', 2, -9; -- { serverError VIOLATED_CONSTRAINT }
    INSERT INTO checked_events VALUES (8, 41, 3, 'abc', 1, -7); -- { serverError VIOLATED_CONSTRAINT }
    INSERT INTO checked_events VALUES (8, 2, 3, '', 1, -7); -- { serverError VIOLATED_CONSTRAINT }
    INSERT INTO checked_events (id, positive, note, mac, ordinary) VALUES (8, 2, 'abc', 1, -7); -- { serverError VIOLATED_CONSTRAINT }
    INSERT INTO checked_events (id, positive, \`right.score\`, mac, ordinary) VALUES (4, 4, 9, 1, -11);
    INSERT INTO checked_events FROM INFILE '${work_dir}/valid.tsv' FORMAT TSV;
    INSERT INTO checked_events FROM INFILE '${work_dir}/invalid.tsv' FORMAT TSV; -- { serverError VIOLATED_CONSTRAINT }
    SELECT toTypeName(positive), toTypeName(note), toTypeName(mac), positive - \`right.score\`,
           empty(substring(note, 5)), toString(mac) FROM checked_events WHERE id = 1;

    CREATE OR REPLACE TYPE LitePositiveInt AS Int64 CHECK VALUE > 10;
    CREATE TABLE fresh_events (positive LitePositiveInt) ENGINE = MergeTree ORDER BY positive;
    INSERT INTO fresh_events VALUES (10); -- { serverError VIOLATED_CONSTRAINT }
    INSERT INTO fresh_events VALUES (11);
    INSERT INTO checked_events VALUES (5, 1, 2, 'old', 1, -1);
    DROP TYPE LitePositiveInt;
    SELECT arraySort(groupArray(id)), anyIf(note, id = 4) FROM checked_events;
SQL

${CLICKHOUSE_LOCAL} --path "${work_dir}/source" --multiquery <<'SQL'
    SET async_insert = 0;
    USE checked;
    CREATE TABLE unresolved (x LitePositiveInt) ENGINE = Memory; -- { serverError UNKNOWN_TYPE }
    CREATE TABLE persisted_type (x LiteNonEmptyString) ENGINE = Memory;
    INSERT INTO persisted_type VALUES (''); -- { serverError VIOLATED_CONSTRAINT }
    INSERT INTO checked_events VALUES (6, 0, 4, 'restarted', 1, -1); -- { serverError VIOLATED_CONSTRAINT }
    INSERT INTO checked_events VALUES (7, 2, 10, 'restarted', 1, -1);
    INSERT INTO fresh_events VALUES (9); -- { serverError VIOLATED_CONSTRAINT }
    SELECT (SELECT arraySort(groupArray(id)) FROM checked_events), (SELECT arraySort(groupArray(positive)) FROM fresh_events);
SQL

ddl=$(${CLICKHOUSE_LOCAL} --path "${work_dir}/source" --database checked --query 'SHOW CREATE TABLE checked_events FORMAT TSVRaw')

# Execute the exported SQL in a separate catalog with none of the definitions.
${CLICKHOUSE_LOCAL} --path "${work_dir}/target" --multiquery <<SQL
    SET async_insert = 0;
    CREATE DATABASE checked ENGINE = Atomic;
    USE checked;
    CREATE TABLE unresolved (x LiteNonEmptyString) ENGINE = Memory; -- { serverError UNKNOWN_TYPE }
    ${ddl};
    INSERT INTO checked_events VALUES (1, 2, 3, 'replayed', 281474976710655, -7);
    INSERT INTO checked_events VALUES (2, 41, 3, 'replayed', 1, -7); -- { serverError VIOLATED_CONSTRAINT }
    INSERT INTO checked_events VALUES (0, 2, 3, 'replayed', 1, -7); -- { serverError VIOLATED_CONSTRAINT }
    INSERT INTO checked_events VALUES (2, 2, 0, 'replayed', 1, -7); -- { serverError VIOLATED_CONSTRAINT }
    SELECT positive, \`right.score\`, note, mac, ordinary FROM checked_events;
    SELECT count() FROM system.errors WHERE name = 'VIOLATED_CONSTRAINT' AND NOT remote
        AND position(last_error_message, 'Expression: (\`right.score\` > 0)') > 0;
SQL
