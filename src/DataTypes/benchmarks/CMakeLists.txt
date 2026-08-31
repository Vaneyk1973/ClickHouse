clickhouse_add_executable(benchmark_udt_catalog_resolver udt_catalog_resolver.cpp)
target_link_libraries(benchmark_udt_catalog_resolver PRIVATE
    ch_contrib::gbenchmark_all
    dbms)

clickhouse_add_executable(benchmark_udt_parser udt_parser.cpp)
target_link_libraries(benchmark_udt_parser PRIVATE
    ch_contrib::gbenchmark_all
    dbms)

if (EXISTS "${ClickHouse_SOURCE_DIR}/src/DataTypes/BuiltInDataTypeFamilyClassifier.h")
    target_compile_definitions(benchmark_udt_parser PRIVATE CLICKHOUSE_UDT_PARSER_BENCHMARK=1)
else()
    target_compile_definitions(benchmark_udt_parser PRIVATE CLICKHOUSE_UDT_PARSER_BENCHMARK=0)
endif()

clickhouse_add_executable(benchmark_udt_analysis udt_analysis.cpp)
target_link_libraries(benchmark_udt_analysis PRIVATE
    ch_contrib::gbenchmark_all
    clickhouse_functions
    dbms)

if (EXISTS "${ClickHouse_SOURCE_DIR}/src/Parsers/ASTCastTarget.h")
    target_compile_definitions(benchmark_udt_analysis PRIVATE CLICKHOUSE_UDT_ANALYSIS_BENCHMARK=1)
else()
    target_compile_definitions(benchmark_udt_analysis PRIVATE CLICKHOUSE_UDT_ANALYSIS_BENCHMARK=0)
endif()
