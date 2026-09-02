clickhouse_add_executable(benchmark_udt_parser udt_parser.cpp)
target_compile_definitions(benchmark_udt_parser PRIVATE CLICKHOUSE_UDT_PARSER_BENCHMARK=0)
target_link_libraries(benchmark_udt_parser PRIVATE
    ch_contrib::gbenchmark_all
    dbms)

clickhouse_add_executable(benchmark_udt_analysis udt_analysis.cpp)
target_compile_definitions(benchmark_udt_analysis PRIVATE CLICKHOUSE_UDT_ANALYSIS_BENCHMARK=0)
target_link_libraries(benchmark_udt_analysis PRIVATE
    ch_contrib::gbenchmark_all
    clickhouse_functions
    dbms)
