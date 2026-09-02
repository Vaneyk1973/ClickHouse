#!/usr/bin/env bash

CURDIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../shell_config.sh
. "$CURDIR"/../shell_config.sh

# The metric_log table has a large number of columns, so its definition is also large.
# And it is larger than the default value of `output_format_pretty_max_value_width`.
# But cutting it in the result of SHOW CREATE TABLE will be bad for a user.
# That's why we control it with the setting `output_format_pretty_max_value_width_apply_for_single_value`.

# Make sure that system.metric_log exists
${CLICKHOUSE_CLIENT} --query "SELECT 1 FORMAT Null"
metric_log_deadline=$((SECONDS + 120))
while [[ $(${CLICKHOUSE_CLIENT} --query "EXISTS TABLE system.metric_log" 2>/dev/null) != 1 ]]; do
    if ((SECONDS >= metric_log_deadline)); then
        echo "system.metric_log was not created within 120 seconds" >&2
        exit 1
    fi
    sleep 1
done


${CLICKHOUSE_CLIENT} --output-format-pretty-multiline-fields 0 --output_format_pretty_fallback_to_vertical 0 --query "SHOW CREATE TABLE system.metric_log" --format Pretty | grep -P '^COMMENT'
${CLICKHOUSE_CLIENT} --output-format-pretty-multiline-fields 0 --output_format_pretty_fallback_to_vertical 0 --query "SHOW CREATE TABLE system.metric_log" --format PrettyCompact | grep -P '^COMMENT'
${CLICKHOUSE_CLIENT} --output-format-pretty-multiline-fields 0 --output_format_pretty_fallback_to_vertical 0 --query "SHOW CREATE TABLE system.metric_log" --format PrettySpace | grep -P '^COMMENT'

# Just in case, non-Pretty format:
${CLICKHOUSE_CLIENT} --query "SHOW CREATE TABLE system.metric_log" --format TSV | grep -o -P '\\nCOMMENT.+$'
