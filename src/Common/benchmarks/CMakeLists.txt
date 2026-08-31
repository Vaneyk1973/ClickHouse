clickhouse_add_microbenchmark(integer_hash_tables_and_hashes
    SOURCES integer_hash_tables_and_hashes.cpp
    LIBRARIES
        clickhouse_common_io
        ch_contrib::wyhash
        ch_contrib::farmhash
        ch_contrib::xxHash)

clickhouse_add_microbenchmark(orc_string_dictionary
    SOURCES orc_string_dictionary.cpp
    LIBRARIES common)

clickhouse_add_microbenchmark(benchmark_radix_sort
    SOURCES radix_sort.cpp
    LIBRARIES dbms)

clickhouse_add_microbenchmark(wrap_in_nullable
    SOURCES wrap_in_nullable.cpp
    LIBRARIES dbms)

clickhouse_add_microbenchmark(profile_events
    SOURCES profile_events.cpp
    LIBRARIES clickhouse_common_io)

clickhouse_add_microbenchmark(benchmark_sched_getcpu
    SOURCES sched_getcpu.cpp
    LIBRARIES ${CMAKE_DL_LIBS})

clickhouse_add_microbenchmark(benchmark_udt_physicalization_token_masking
    SOURCES udt_physicalization_token_masking.cpp
    LIBRARIES dbms)

if (EXISTS "${ClickHouse_SOURCE_DIR}/src/Parsers/ASTCastTarget.h")
    target_compile_definitions(benchmark_udt_physicalization_token_masking_objects PRIVATE CLICKHOUSE_UDT_TOKEN_MASKING_BENCHMARK=1)
else()
    target_compile_definitions(benchmark_udt_physicalization_token_masking_objects PRIVATE CLICKHOUSE_UDT_TOKEN_MASKING_BENCHMARK=0)
endif()
