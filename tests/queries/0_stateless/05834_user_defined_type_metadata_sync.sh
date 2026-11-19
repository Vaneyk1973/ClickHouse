#!/usr/bin/env bash
# Tags: no-fasttest
# no-fasttest: needs a failpoint.

CUR_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../shell_config.sh
. "$CUR_DIR"/../shell_config.sh

set -e

errors=$(mktemp "${CLICKHOUSE_TMP}/udt-metadata-sync.XXXXXX")
trap 'rm -f -- "$errors"' EXIT

${CLICKHOUSE_LOCAL} --ignore-error --multiquery 2>"$errors" <<'SQL'
    SET fsync_metadata = 1;

    -- The first durable create syncs the parent before publishing the definition.
    SYSTEM ENABLE FAILPOINT local_directory_sync_fail;
    CREATE TYPE LiteSyncBefore AS UInt64;
    CREATE TABLE before_publish (value LiteSyncBefore) ENGINE = Memory;
    CREATE TYPE LiteSyncBefore AS UInt64;

    SYSTEM ENABLE FAILPOINT local_directory_sync_fail;
    CREATE TYPE LiteSync AS Int64;
    CREATE TABLE after_create (value LiteSync) ENGINE = Memory;
    SELECT 'after create', type FROM system.columns WHERE database = currentDatabase() AND table = 'after_create';

    SYSTEM ENABLE FAILPOINT local_directory_sync_fail;
    CREATE OR REPLACE TYPE LiteSync AS String;
    CREATE TABLE after_replace (value LiteSync) ENGINE = Memory;
    SELECT 'after replace', type FROM system.columns WHERE database = currentDatabase() AND table = 'after_replace';

    SYSTEM ENABLE FAILPOINT local_directory_sync_fail;
    DROP TYPE LiteSync;
    CREATE TABLE after_drop (value LiteSync) ENGINE = Memory;

    SYSTEM ENABLE FAILPOINT local_directory_sync_fail;
    DROP TYPE IF EXISTS LiteSync;
    DROP TYPE IF EXISTS LiteSync;
SQL

# Keep the error code and any catalog-state diagnostic printed by the client.
sed -nE '
    s/^Code: [0-9]+\. .* (User-defined type .*)\. \((CANNOT_FSYNC)\).*$/\2 \1/p
    t
    s/^Code: [0-9]+\. .* \(([A-Z_]+)\).*$/\1/p
' "$errors"
