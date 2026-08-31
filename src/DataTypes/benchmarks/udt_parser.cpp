#include <benchmark/benchmark.h>

#include <Core/Defines.h>

#include <Common/SipHash.h>
#include <Common/benchmarks/JemallocBenchmarkMemoryManager.h>

#if CLICKHOUSE_UDT_PARSER_BENCHMARK
#include <DataTypes/BuiltInDataTypeFamilyClassifier.h>
#endif

#include <Parsers/ASTFunction.h>
#include <Parsers/ParserDataType.h>
#include <Parsers/ParserQuery.h>
#include <Parsers/TokenIterator.h>
#include <Parsers/parseQuery.h>

#include <algorithm>
#include <cstddef>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace DB
{
namespace
{

enum class ParserKind : UInt8
{
    Query,
    DataType,
    ClassifiedDataType,
};

struct Dimensions
{
    size_t type_depth = 0;
    size_t width = 0;
    size_t expected_functions = 0;
    size_t expected_qualified_references = 0;
};

struct ASTMetrics
{
    size_t nodes = 0;
    size_t depth = 0;
    size_t functions = 0;
    size_t qualified_references = 0;
};

struct BenchmarkCase
{
    String name;
    String text;
    ParserKind parser_kind = ParserKind::Query;
    Dimensions dimensions;
    ASTPtr prepared_ast;
    ASTMetrics ast_metrics;
    IASTHash ast_hash{};
    IASTHash formatted_hash{};
    size_t formatted_bytes = 0;
    UInt32 parser_backtracks = 0;
    bool prepared = false;
};

String nestedType(String leaf, size_t depth)
{
    if (depth == 0)
        throw std::invalid_argument("type depth must be positive");

    for (size_t level = 1; level < depth; ++level)
        leaf = "Array(" + leaf + ')';
    return leaf;
}

String makeCreateTable(String name, const String & type)
{
    return "CREATE TABLE bench." + std::move(name) + " (value " + type + ") ENGINE = Memory";
}

#if CLICKHOUSE_UDT_PARSER_BENCHMARK
String makeCreateType(String name, const String & type)
{
    return "CREATE TYPE bench." + std::move(name) + " AS " + type;
}
#endif

String makeWideCreateTable(size_t width)
{
    String query = "CREATE TABLE bench.WideTable" + std::to_string(width) + " (";
    query.reserve(query.size() + width * 16);
    for (size_t index = 0; index < width; ++index)
    {
        if (index != 0)
            query += ", ";
        query += "c" + std::to_string(index) + " UInt64";
    }
    query += ") ENGINE = Memory";
    return query;
}

#if CLICKHOUSE_UDT_PARSER_BENCHMARK
String makeWideCreateType(size_t width)
{
    String query = "CREATE TYPE bench.WideType" + std::to_string(width) + " AS Tuple(";
    query.reserve(query.size() + width * 16);
    for (size_t index = 0; index < width; ++index)
    {
        if (index != 0)
            query += ", ";
        query += "c" + std::to_string(index) + " UInt64";
    }
    query += ')';
    return query;
}
#endif

String makeFunctionDenseSelect(size_t expression_count)
{
    String query = "SELECT ";
    query.reserve(query.size() + expression_count * 96);
    for (size_t index = 0; index < expression_count; ++index)
    {
        if (index != 0)
            query += ", ";
        const String value = std::to_string(index + 1);
        query += "plus(abs(toInt64(" + value + ")), multiply(toInt64(" + value + "), 2)) AS c" + std::to_string(index);
    }
    return query;
}

String makeCastDenseSelect(size_t expression_count, bool qualified)
{
    String query = "SELECT ";
    query.reserve(query.size() + expression_count * 48);
    for (size_t index = 0; index < expression_count; ++index)
    {
        if (index != 0)
            query += ", ";
        const String type = qualified ? "app.Value" + std::to_string(index) : "UInt64";
        query += "CAST(" + std::to_string(index + 1) + " AS " + type + ") AS c" + std::to_string(index);
    }
    return query;
}

#if CLICKHOUSE_UDT_PARSER_BENCHMARK
DataTypeFamilyClassification
classifyDataTypeFamily(const void *, std::string_view family_name, DataTypeFamilySyntaxKind syntax_kind) noexcept
{
    BuiltInDataTypeFamilyClassification classification;
    switch (syntax_kind)
    {
        case DataTypeFamilySyntaxKind::Generic: classification = BuiltInDataTypeFamilyClassifier::classifyGeneric(family_name); break;
        case DataTypeFamilySyntaxKind::SpecializedEnum:
            classification = BuiltInDataTypeFamilyClassifier::classifySpecializedEnum(family_name);
            break;
        case DataTypeFamilySyntaxKind::SpecializedTuple:
            classification = BuiltInDataTypeFamilyClassifier::classifySpecializedTuple(family_name);
            break;
        case DataTypeFamilySyntaxKind::QualifiedReference:
            classification = BuiltInDataTypeFamilyClassifier::classifyQualifiedReference();
            break;
    }

    return {
        .is_built_in = static_cast<bool>(classification),
        .is_qualified_reference = classification.admission == BuiltInDataTypeAdmissionPath::QualifiedUserType,
    };
}

DataTypeFamilyClassifier makeDataTypeFamilyClassifier()
{
    return {.context = nullptr, .callback = classifyDataTypeFamily};
}
#endif

ASTPtr parseBenchmarkText(const BenchmarkCase & benchmark_case)
{
    static const String data_type_description = "UDT data type parser benchmark";
    static const String query_description = "UDT query parser benchmark";

    if (benchmark_case.parser_kind == ParserKind::DataType)
    {
        ParserDataType parser;
        return parseQuery(
            parser, benchmark_case.text, data_type_description, 0, DBMS_DEFAULT_MAX_PARSER_DEPTH, DBMS_DEFAULT_MAX_PARSER_BACKTRACKS);
    }

    if (benchmark_case.parser_kind == ParserKind::ClassifiedDataType)
    {
#if CLICKHOUSE_UDT_PARSER_BENCHMARK
        DataTypeFamilyClassificationSummary summary;
        ParserDataTypeWithFamilyClassification parser(makeDataTypeFamilyClassifier(), summary);
        return parseQuery(
            parser, benchmark_case.text, data_type_description, 0, DBMS_DEFAULT_MAX_PARSER_DEPTH, DBMS_DEFAULT_MAX_PARSER_BACKTRACKS);
#else
        throw std::logic_error("classified UDT parser benchmark is unavailable in this build");
#endif
    }

    ParserQuery parser(benchmark_case.text.data() + benchmark_case.text.size());
    return parseQuery(parser, benchmark_case.text, query_description, 0, DBMS_DEFAULT_MAX_PARSER_DEPTH, DBMS_DEFAULT_MAX_PARSER_BACKTRACKS);
}

template <typename Parser>
UInt32 measureParserBacktracks(Parser & parser, const String & text)
{
    /// parseQuery does not expose this counter. Probe the same parser once
    /// outside the timed loop while retaining parseQuery for end-to-end CPU.
    Tokens tokens(text.data(), text.data() + text.size(), 0, true);
    IParser::Pos position(tokens, DBMS_DEFAULT_MAX_PARSER_DEPTH, DBMS_DEFAULT_MAX_PARSER_BACKTRACKS);
    Expected expected;
    ASTPtr ast;
    if (!parser.parse(position, ast, expected) || !position->isEnd())
        throw std::logic_error("benchmark backtrack probe did not consume its valid input");
    return position.backtracks;
}

UInt32 measureParserBacktracks(const BenchmarkCase & benchmark_case)
{
    if (benchmark_case.parser_kind == ParserKind::DataType)
    {
        ParserDataType parser;
        return measureParserBacktracks(parser, benchmark_case.text);
    }

    if (benchmark_case.parser_kind == ParserKind::ClassifiedDataType)
    {
#if CLICKHOUSE_UDT_PARSER_BENCHMARK
        DataTypeFamilyClassificationSummary summary;
        ParserDataTypeWithFamilyClassification parser(makeDataTypeFamilyClassifier(), summary);
        return measureParserBacktracks(parser, benchmark_case.text);
#else
        throw std::logic_error("classified UDT parser benchmark is unavailable in this build");
#endif
    }

    ParserQuery parser(benchmark_case.text.data() + benchmark_case.text.size());
    return measureParserBacktracks(parser, benchmark_case.text);
}

ASTMetrics collectASTMetrics(const ASTPtr & ast)
{
    ASTMetrics metrics;
    std::vector<std::pair<const IAST *, size_t>> pending;
    pending.emplace_back(ast.get(), 1);

    while (!pending.empty())
    {
        const auto [node, depth] = pending.back();
        pending.pop_back();
        if (!node)
            continue;

        ++metrics.nodes;
        metrics.depth = std::max(metrics.depth, depth);
        metrics.functions += node->as<ASTFunction>() != nullptr;
        metrics.qualified_references += node->getID('_').starts_with("UDTReference");
        for (const auto & child : node->children)
            pending.emplace_back(child.get(), depth + 1);
    }

    return metrics;
}

void prepareCase(BenchmarkCase & benchmark_case)
{
    if (benchmark_case.prepared)
        return;

    /// Preparation stays lazy so this exact source can be overlaid on a
    /// baseline and run with a Control-only benchmark filter.
    benchmark_case.prepared_ast = parseBenchmarkText(benchmark_case);
    benchmark_case.parser_backtracks = measureParserBacktracks(benchmark_case);
    benchmark_case.ast_metrics = collectASTMetrics(benchmark_case.prepared_ast);
    benchmark_case.ast_hash = benchmark_case.prepared_ast->getTreeHash(false);
    const String formatted = benchmark_case.prepared_ast->formatWithSecretsOneLine();
    benchmark_case.formatted_bytes = formatted.size();
    SipHash formatted_hash;
    formatted_hash.update(formatted.data(), formatted.size());
    benchmark_case.formatted_hash = getSipHash128AsPair(formatted_hash);

    if (benchmark_case.ast_metrics.functions < benchmark_case.dimensions.expected_functions)
        throw std::logic_error("benchmark fixture contains fewer function nodes than requested");
    if (benchmark_case.ast_metrics.qualified_references < benchmark_case.dimensions.expected_qualified_references)
        throw std::logic_error("benchmark fixture contains fewer qualified references than requested");
    benchmark_case.prepared = true;
}

void exportCounters(benchmark::State & state, const BenchmarkCase & benchmark_case)
{
    const auto export_hash = [&](const char * prefix, const IASTHash & hash)
    {
        const UInt64 low = CityHash_v1_0_2::Uint128Low64(hash);
        const UInt64 high = CityHash_v1_0_2::Uint128High64(hash);
        state.counters[String(prefix) + "_high64_high32"] = static_cast<UInt32>(high >> 32);
        state.counters[String(prefix) + "_high64_low32"] = static_cast<UInt32>(high);
        state.counters[String(prefix) + "_low64_high32"] = static_cast<UInt32>(low >> 32);
        state.counters[String(prefix) + "_low64_low32"] = static_cast<UInt32>(low);
    };

    export_hash("ast_hash", benchmark_case.ast_hash);
    export_hash("formatted_hash", benchmark_case.formatted_hash);
    state.counters["ast_depth"] = static_cast<double>(benchmark_case.ast_metrics.depth);
    state.counters["ast_nodes"] = static_cast<double>(benchmark_case.ast_metrics.nodes);
    state.counters["formatted_bytes"] = static_cast<double>(benchmark_case.formatted_bytes);
    state.counters["function_nodes"] = static_cast<double>(benchmark_case.ast_metrics.functions);
    state.counters["input_bytes"] = static_cast<double>(benchmark_case.text.size());
    state.counters["parser_backtracks"] = static_cast<double>(benchmark_case.parser_backtracks);
    state.counters["qualified_references"] = static_cast<double>(benchmark_case.ast_metrics.qualified_references);
    state.counters["type_depth"] = static_cast<double>(benchmark_case.dimensions.type_depth);
    state.counters["width"] = static_cast<double>(benchmark_case.dimensions.width);
}

void benchmarkParse(benchmark::State & state, BenchmarkCase * benchmark_case)
{
    prepareCase(*benchmark_case);
    for (auto _ : state)
    {
        static_cast<void>(_);
        auto ast = parseBenchmarkText(*benchmark_case);
        benchmark::DoNotOptimize(ast.get());
    }

    exportJemallocOperationMemory(
        state,
        [&]
        {
            auto ast = parseBenchmarkText(*benchmark_case);
            benchmark::DoNotOptimize(ast.get());
        });

    exportCounters(state, *benchmark_case);
    state.SetBytesProcessed(static_cast<int64_t>(state.iterations() * benchmark_case->text.size()));
    state.SetItemsProcessed(state.iterations());
}

void benchmarkFormat(benchmark::State & state, BenchmarkCase * benchmark_case)
{
    prepareCase(*benchmark_case);
    for (auto _ : state)
    {
        static_cast<void>(_);
        auto formatted = benchmark_case->prepared_ast->formatWithSecretsOneLine();
        benchmark::DoNotOptimize(formatted);
    }


    exportJemallocOperationMemory(
        state,
        [&]
        {
            auto formatted = benchmark_case->prepared_ast->formatWithSecretsOneLine();
            benchmark::DoNotOptimize(formatted);
        });

    exportCounters(state, *benchmark_case);
    state.SetBytesProcessed(static_cast<int64_t>(state.iterations() * benchmark_case->formatted_bytes));
    state.SetItemsProcessed(state.iterations());
}

void addCase(
    std::vector<BenchmarkCase> & cases, String name, String text, ParserKind parser_kind = ParserKind::Query, Dimensions dimensions = {})
{
    cases.push_back({
        .name = std::move(name),
        .text = std::move(text),
        .parser_kind = parser_kind,
        .dimensions = dimensions,
        .prepared_ast = {},
        .ast_metrics = {},
        .ast_hash = {},
        .formatted_hash = {},
        .formatted_bytes = 0,
        .parser_backtracks = 0,
        .prepared = false,
    });
}

std::vector<BenchmarkCase> makeCases()
{
    std::vector<BenchmarkCase> cases;
    cases.reserve(40);

    for (const size_t depth : {1, 32})
    {
        const String depth_suffix = "/Depth" + std::to_string(depth);
        const String built_in_type = nestedType("UInt64", depth);
#if CLICKHOUSE_UDT_PARSER_BENCHMARK
        const String qualified_type = nestedType("app.UserId", depth);
#endif

        addCase(cases, "Control/DataType/BuiltIn" + depth_suffix, built_in_type, ParserKind::DataType, {.type_depth = depth});
#if CLICKHOUSE_UDT_PARSER_BENCHMARK
        addCase(
            cases,
            "UDT/DataType/Qualified" + depth_suffix,
            qualified_type,
            ParserKind::ClassifiedDataType,
            {.type_depth = depth, .expected_qualified_references = 1});
#endif

        addCase(
            cases,
            "Control/CreateTable/BuiltIn" + depth_suffix,
            makeCreateTable("BuiltInDepth" + std::to_string(depth), built_in_type),
            ParserKind::Query,
            {.type_depth = depth});
#if CLICKHOUSE_UDT_PARSER_BENCHMARK
        addCase(
            cases,
            "UDT/CreateType/BuiltInBody" + depth_suffix,
            makeCreateType("BuiltInDepth" + std::to_string(depth), built_in_type),
            ParserKind::Query,
            {.type_depth = depth});
        addCase(
            cases,
            "UDT/CreateType/QualifiedBody" + depth_suffix,
            makeCreateType("QualifiedDepth" + std::to_string(depth), qualified_type),
            ParserKind::Query,
            {.type_depth = depth, .expected_qualified_references = 1});
#endif

        addCase(
            cases,
            "Control/CastString/BuiltIn" + depth_suffix,
            "SELECT CAST(1, '" + built_in_type + "')",
            ParserKind::Query,
            {.type_depth = depth, .expected_functions = 1});
        addCase(
            cases,
            "Control/CastAs/BuiltIn" + depth_suffix,
            "SELECT CAST(1 AS " + built_in_type + ')',
            ParserKind::Query,
            {.type_depth = depth, .expected_functions = 1});
#if CLICKHOUSE_UDT_PARSER_BENCHMARK
        addCase(
            cases,
            "UDT/CastAs/Qualified" + depth_suffix,
            "SELECT CAST(1 AS " + qualified_type + ')',
            ParserKind::Query,
            {.type_depth = depth, .expected_functions = 1, .expected_qualified_references = 1});
#endif
        addCase(
            cases,
            "Control/DoubleColon/BuiltIn" + depth_suffix,
            "SELECT 1::" + built_in_type,
            ParserKind::Query,
            {.type_depth = depth, .expected_functions = 1});
#if CLICKHOUSE_UDT_PARSER_BENCHMARK
        addCase(
            cases,
            "UDT/DoubleColon/Qualified" + depth_suffix,
            "SELECT 1::" + qualified_type,
            ParserKind::Query,
            {.type_depth = depth, .expected_functions = 1, .expected_qualified_references = 1});
#endif
    }

#if CLICKHOUSE_UDT_PARSER_BENCHMARK
    addCase(cases, "UDT/CheckedTemplate/Shallow", "CREATE TYPE app.Identity(T TYPE) AS T");
    addCase(
        cases,
        "UDT/CheckedTemplate/Depth32",
        "CREATE TYPE app.Deep(T TYPE) AS " + nestedType("T", 32),
        ParserKind::Query,
        {.type_depth = 32});
    addCase(
        cases,
        "UDT/CheckedTemplate/Recursive",
        "CREATE TYPE app.Nested(T TYPE, N UInt16) DECREASES N "
        "AS TYPE_IF(N = 0, T, Tuple(head T, tail app.Nested(T, N - 1)))",
        ParserKind::Query,
        {.expected_qualified_references = 1});
    addCase(
        cases,
        "UDT/TemplateApplication/Shallow",
        "SELECT CAST(1 AS app.Identity(UInt64))",
        ParserKind::Query,
        {.type_depth = 1, .expected_functions = 1, .expected_qualified_references = 1});
    addCase(
        cases,
        "UDT/TemplateApplication/Depth32",
        "SELECT CAST(1 AS app.Deep(" + nestedType("UInt64", 32) + "))",
        ParserKind::Query,
        {.type_depth = 32, .expected_functions = 1, .expected_qualified_references = 1});
#endif

    addCase(cases, "Control/ShowList/Output", "SHOW TABLES FROM bench LIKE 'Type%' FORMAT JSONEachRow");
#if CLICKHOUSE_UDT_PARSER_BENCHMARK
    addCase(cases, "UDT/ShowList/Output", "SHOW TYPES FROM bench LIKE 'Type%' FORMAT JSONEachRow");
#endif
    addCase(cases, "Control/ShowCreate/Output", "SHOW CREATE TABLE bench.table FORMAT TSV");
#if CLICKHOUSE_UDT_PARSER_BENCHMARK
    addCase(cases, "UDT/ShowCreate/Output", "SHOW CREATE TYPE bench.Type FORMAT TSV");
#endif
    addCase(cases, "Control/Describe/Output", "DESCRIBE TABLE bench.table FORMAT JSONEachRow");
#if CLICKHOUSE_UDT_PARSER_BENCHMARK
    addCase(cases, "UDT/Describe/Output", "DESCRIBE TYPE bench.Type FORMAT JSONEachRow");
#endif

    for (const size_t width : {1, 100, 10'000})
    {
        const String width_suffix = "/Width" + std::to_string(width);
        addCase(cases, "Control/CreateTable/Wide" + width_suffix, makeWideCreateTable(width), ParserKind::Query, {.width = width});
#if CLICKHOUSE_UDT_PARSER_BENCHMARK
        addCase(cases, "UDT/CreateType/Wide" + width_suffix, makeWideCreateType(width), ParserKind::Query, {.width = width});
#endif
    }

    constexpr size_t function_expression_count = 64;
    constexpr size_t cast_expression_count = 128;
    addCase(
        cases,
        "Control/FunctionDense/BuiltIn",
        makeFunctionDenseSelect(function_expression_count),
        ParserKind::Query,
        {.width = function_expression_count, .expected_functions = function_expression_count * 5});
    addCase(
        cases,
        "Control/FunctionDense/CastAsBuiltIn",
        makeCastDenseSelect(cast_expression_count, false),
        ParserKind::Query,
        {.width = cast_expression_count, .expected_functions = cast_expression_count});
#if CLICKHOUSE_UDT_PARSER_BENCHMARK
    addCase(
        cases,
        "UDT/FunctionDense/CastAsQualified",
        makeCastDenseSelect(cast_expression_count, true),
        ParserKind::Query,
        {
            .width = cast_expression_count,
            .expected_functions = cast_expression_count,
            .expected_qualified_references = cast_expression_count,
        });
#endif

    return cases;
}

}
}

int main(int argc, char ** argv)
{
    try
    {
        auto cases = DB::makeCases();
        for (auto & benchmark_case : cases)
        {
            benchmark::RegisterBenchmark(("UDTParser/Parse/" + benchmark_case.name).c_str(), DB::benchmarkParse, &benchmark_case);
            benchmark::RegisterBenchmark(("UDTParser/Format/" + benchmark_case.name).c_str(), DB::benchmarkFormat, &benchmark_case);
        }

        benchmark::AddCustomContext("suite", "user-defined type parser and formatter");
        benchmark::AddCustomContext(
            "comparison_contract", "compare identical Control names across baseline and candidate binaries; UDT names are scale coverage");
#if USE_JEMALLOC
        benchmark::AddCustomContext("memory_metrics", "one exact operation measured separately in an isolated allocator arena");
        benchmark::AddCustomContext(
            "memory_peak_note", "allocator thread peak is approximate; small-case peak values are directional only");
#else
        benchmark::AddCustomContext("memory_metrics", "unavailable: build without jemalloc");
#endif
        benchmark::Initialize(&argc, argv);
        if (benchmark::ReportUnrecognizedArguments(argc, argv))
            return 1;
        benchmark::RunSpecifiedBenchmarks();
        benchmark::Shutdown();
        return 0;
    }
    catch (const std::exception & exception)
    {
        std::cerr << "Cannot prepare UDT parser benchmark fixtures: " << exception.what() << '\n';
        return 1;
    }
}
