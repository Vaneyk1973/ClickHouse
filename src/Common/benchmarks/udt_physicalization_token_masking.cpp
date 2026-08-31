#include <benchmark/benchmark.h>

#include <Core/Defines.h>
#include <Core/Types.h>

#include <Common/benchmarks/JemallocBenchmarkMemoryManager.h>

#include <Parsers/ParserQuery.h>
#include <Parsers/parseQuery.h>

#if CLICKHOUSE_UDT_TOKEN_MASKING_BENCHMARK
#include <Common/SensitiveDataMasker.h>
#endif

#include <array>
#include <cstddef>
#include <iostream>
#include <stdexcept>
#include <string_view>

namespace DB
{
namespace
{

constexpr size_t batch_size = 8;

String makeSizedInvalidQuery(size_t input_bytes)
{
    static constexpr std::string_view prefix = "SELECT 1 /*";
    static constexpr std::string_view suffix = "*/ +";
    if (input_bytes < prefix.size() + suffix.size())
        throw std::invalid_argument("invalid-query benchmark input is too small");

    String query(prefix);
    query.append(input_bytes - prefix.size() - suffix.size(), 'x');
    query += suffix;
    return query;
}

void runInvalidQuery(const String & query)
{
    const char * position = query.data();
    String error;
    ParserQuery parser(query.data() + query.size());
    ASTPtr ast = tryParseQuery(
        parser,
        position,
        query.data() + query.size(),
        error,
        false,
        "UDT token masking parser-error benchmark",
        false,
        0,
        DBMS_DEFAULT_MAX_PARSER_DEPTH,
        DBMS_DEFAULT_MAX_PARSER_BACKTRACKS,
        true);
    if (ast || error.empty())
        throw std::logic_error("invalid-query benchmark fixture did not fail parsing");
    benchmark::DoNotOptimize(ast.get());
    benchmark::DoNotOptimize(error.data());
}

void benchmarkOrdinaryParseError(benchmark::State & state)
{
    const size_t input_bytes = static_cast<size_t>(state.range(0));
    const String query = makeSizedInvalidQuery(input_bytes);
    runInvalidQuery(query);

    for (auto _ : state)
    {
        static_cast<void>(_);
        for (size_t index = 0; index < batch_size; ++index)
            runInvalidQuery(query);
    }

    exportJemallocOperationMemory(state, [&] { runInvalidQuery(query); });

    state.counters["input_bytes"] = static_cast<double>(query.size());
    state.counters["operations_per_iteration"] = static_cast<double>(batch_size);
    state.SetBytesProcessed(static_cast<int64_t>(state.iterations() * batch_size * query.size()));
    state.SetItemsProcessed(state.iterations() * batch_size);
    state.SetComplexityN(static_cast<int64_t>(input_bytes));
}

#if CLICKHOUSE_UDT_TOKEN_MASKING_BENCHMARK

String makeSizedOrdinaryQuery(size_t input_bytes)
{
    static constexpr std::string_view prefix = "SELECT 1 /*";
    static constexpr std::string_view suffix = "*/";
    if (input_bytes < prefix.size() + suffix.size())
        throw std::invalid_argument("ordinary-query benchmark input is too small");

    String query(prefix);
    query.append(input_bytes - prefix.size() - suffix.size(), 'x');
    query += suffix;
    return query;
}

String makeSizedApplyQuery(size_t input_bytes, bool terminate_value)
{
    static constexpr std::string_view prefix = "PHYSICALIZE TYPE REFERENCES APPLY TOKEN '";
    static constexpr std::string_view suffix = "' FORMAT Null";
    const size_t suffix_size = terminate_value ? suffix.size() : 0;
    if (input_bytes < prefix.size() + suffix_size)
        throw std::invalid_argument("apply-query benchmark input is too small");

    String query(prefix);
    query.append(input_bytes - prefix.size() - suffix_size, 's');
    if (terminate_value)
        query += suffix;
    return query;
}

String makeOrdinaryBatch(size_t statement_count)
{
    String query;
    query.reserve(statement_count * 24);
    for (size_t index = 0; index < statement_count; ++index)
    {
        if (index != 0)
            query += "; ";
        query += "SELECT ';' AS marker";
    }
    return query;
}

String makeApplyBatch(size_t statement_count)
{
    String query;
    query.reserve(statement_count * 52);
    for (size_t index = 0; index < statement_count; ++index)
    {
        if (index != 0)
            query += "; ";
        query += "PHYSICALIZE TYPE REFERENCES APPLY TOKEN ''";
    }
    return query;
}

size_t countHiddenValues(std::string_view text)
{
    static constexpr std::string_view hidden = "'[HIDDEN]'";
    size_t count = 0;
    size_t position = 0;
    while ((position = text.find(hidden, position)) != std::string_view::npos)
    {
        ++count;
        position += hidden.size();
    }
    return count;
}

void exportMaskCounters(benchmark::State & state, size_t input_bytes, size_t output_bytes, size_t statements, size_t expected_redactions)
{
    state.counters["expected_redactions"] = static_cast<double>(expected_redactions);
    state.counters["input_bytes"] = static_cast<double>(input_bytes);
    state.counters["operations_per_iteration"] = static_cast<double>(batch_size);
    state.counters["output_bytes"] = static_cast<double>(output_bytes);
    state.counters["statements"] = static_cast<double>(statements);
    state.SetBytesProcessed(static_cast<int64_t>(state.iterations() * batch_size * input_bytes));
    state.SetItemsProcessed(state.iterations() * batch_size);
}

void benchmarkContainsOrdinary(benchmark::State & state)
{
    const size_t input_bytes = static_cast<size_t>(state.range(0));
    const String query = makeSizedOrdinaryQuery(input_bytes);
    if (containsPhysicalizationApplyToken(query))
        throw std::logic_error("ordinary query was classified as an apply query");

    for (auto _ : state)
    {
        static_cast<void>(_);
        for (size_t index = 0; index < batch_size; ++index)
            benchmark::DoNotOptimize(containsPhysicalizationApplyToken(query));
    }

    exportJemallocOperationMemory(state, [&] { benchmark::DoNotOptimize(containsPhysicalizationApplyToken(query)); });

    exportMaskCounters(state, query.size(), query.size(), 1, 0);
    state.SetComplexityN(static_cast<int64_t>(input_bytes));
}

void benchmarkContainsApply(benchmark::State & state)
{
    const size_t input_bytes = static_cast<size_t>(state.range(0));
    const String query = makeSizedApplyQuery(input_bytes, true);
    if (!containsPhysicalizationApplyToken(query))
        throw std::logic_error("valid apply query was not classified");

    for (auto _ : state)
    {
        static_cast<void>(_);
        for (size_t index = 0; index < batch_size; ++index)
            benchmark::DoNotOptimize(containsPhysicalizationApplyToken(query));
    }

    exportJemallocOperationMemory(state, [&] { benchmark::DoNotOptimize(containsPhysicalizationApplyToken(query)); });

    exportMaskCounters(state, query.size(), query.size(), 1, 1);
    state.SetComplexityN(static_cast<int64_t>(input_bytes));
}

void benchmarkMaskNoMatch(benchmark::State & state, String query, size_t statements)
{
    const String original = query;
    maskPhysicalizationApplyTokens(query);
    if (query != original)
        throw std::logic_error("no-match masker benchmark changed its input");
    String measured = query;

    for (auto _ : state)
    {
        static_cast<void>(_);
        for (size_t index = 0; index < batch_size; ++index)
            maskPhysicalizationApplyTokens(query);
        benchmark::DoNotOptimize(query.data());
    }

    if (query != original)
        throw std::logic_error("no-match masker benchmark changed its input while timed");
    exportJemallocOperationMemory(state, [&] { maskPhysicalizationApplyTokens(measured); });
    exportMaskCounters(state, query.size(), query.size(), statements, 0);
    state.SetComplexityN(static_cast<int64_t>(statements == 1 ? query.size() : statements));
}

void benchmarkMaskOrdinarySingle(benchmark::State & state)
{
    benchmarkMaskNoMatch(state, makeSizedOrdinaryQuery(static_cast<size_t>(state.range(0))), 1);
}

void benchmarkMaskOrdinaryBatch(benchmark::State & state)
{
    benchmarkMaskNoMatch(state, makeOrdinaryBatch(static_cast<size_t>(state.range(0))), static_cast<size_t>(state.range(0)));
}

void benchmarkMaskMatch(benchmark::State & state, const String & query, size_t statements, size_t expected_redactions)
{
    String validated = query;
    maskPhysicalizationApplyTokens(validated);
    if (countHiddenValues(validated) != expected_redactions)
        throw std::logic_error("masker benchmark produced an unexpected number of replacements");
    String measured = query;

    std::array<String, batch_size> working;
    for (auto & value : working)
        value = query;

    for (auto _ : state)
    {
        static_cast<void>(_);
        state.PauseTiming();
        for (auto & value : working)
            value = query;
        state.ResumeTiming();

        for (auto & value : working)
        {
            maskPhysicalizationApplyTokens(value);
            benchmark::DoNotOptimize(value.data());
        }
    }

    exportJemallocOperationMemory(state, [&] { maskPhysicalizationApplyTokens(measured); });

    exportMaskCounters(state, query.size(), validated.size(), statements, expected_redactions);
}

void benchmarkMaskApply(benchmark::State & state)
{
    const size_t input_bytes = static_cast<size_t>(state.range(0));
    benchmarkMaskMatch(state, makeSizedApplyQuery(input_bytes, true), 1, 1);
    state.SetComplexityN(static_cast<int64_t>(input_bytes));
}

void benchmarkMaskUnterminated(benchmark::State & state)
{
    const size_t input_bytes = static_cast<size_t>(state.range(0));
    benchmarkMaskMatch(state, makeSizedApplyQuery(input_bytes, false), 1, 1);
    state.SetComplexityN(static_cast<int64_t>(input_bytes));
}

void benchmarkMaskMalformedPrefix(benchmark::State & state)
{
    const size_t payload_bytes = static_cast<size_t>(state.range(0));
    String query = "PHYSICALIZE TYPE REFERENCE APPLY TOKN '";
    query.append(payload_bytes, 's');
    query += "'";
    benchmarkMaskMatch(state, query, 1, 1);
    state.SetComplexityN(static_cast<int64_t>(query.size()));
}

void benchmarkMaskApplyBatch(benchmark::State & state)
{
    const size_t statement_count = static_cast<size_t>(state.range(0));
    benchmarkMaskMatch(state, makeApplyBatch(statement_count), statement_count, statement_count);
    state.SetComplexityN(static_cast<int64_t>(statement_count));
}

void benchmarkFailurePipeline(benchmark::State & state)
{
    const size_t input_bytes = static_cast<size_t>(state.range(0));
    const String query = makeSizedApplyQuery(input_bytes, false);
    String validated = query;
    maskPhysicalizationApplyTokens(validated);
    validated = wipeSensitiveDataAndCutToLength(std::move(validated), 0, true);
    if (countHiddenValues(validated) != 1)
        throw std::logic_error("failure-pipeline benchmark did not hide its value");
    String measured = query;

    std::array<String, batch_size> working;
    for (auto & value : working)
        value = query;

    for (auto _ : state)
    {
        static_cast<void>(_);
        state.PauseTiming();
        for (auto & value : working)
            value = query;
        state.ResumeTiming();

        for (auto & value : working)
        {
            maskPhysicalizationApplyTokens(value);
            value = wipeSensitiveDataAndCutToLength(std::move(value), 0, true);
            benchmark::DoNotOptimize(value.data());
        }
    }


    exportJemallocOperationMemory(
        state,
        [&]
        {
            maskPhysicalizationApplyTokens(measured);
            measured = wipeSensitiveDataAndCutToLength(std::move(measured), 0, true);
        });

    exportMaskCounters(state, query.size(), validated.size(), 1, 1);
    state.SetComplexityN(static_cast<int64_t>(input_bytes));
}

#endif

void addInputSizes(benchmark::internal::Benchmark * benchmark_case)
{
    for (const int64_t input_bytes : {64, 1024, 16 * 1024, 256 * 1024})
        benchmark_case->Arg(input_bytes);
}

void registerBenchmarks()
{
    auto * parse_error = benchmark::RegisterBenchmark("UDTTokenMask/ParseError/Control/Ordinary", benchmarkOrdinaryParseError);
    addInputSizes(parse_error);
    parse_error->Complexity(benchmark::oN);

#if CLICKHOUSE_UDT_TOKEN_MASKING_BENCHMARK
    auto * contains_ordinary = benchmark::RegisterBenchmark("UDTTokenMask/Contains/OrdinaryEarlyReject", benchmarkContainsOrdinary);
    addInputSizes(contains_ordinary);
    contains_ordinary->Complexity(benchmark::o1);

    auto * contains_apply = benchmark::RegisterBenchmark("UDTTokenMask/Contains/ValidQuoted", benchmarkContainsApply);
    addInputSizes(contains_apply);
    contains_apply->Complexity(benchmark::oN);

    auto * mask_ordinary = benchmark::RegisterBenchmark("UDTTokenMask/Mask/OrdinarySingle", benchmarkMaskOrdinarySingle);
    addInputSizes(mask_ordinary);
    mask_ordinary->Complexity(benchmark::oN);

    benchmark::RegisterBenchmark("UDTTokenMask/Mask/MultiOrdinary", benchmarkMaskOrdinaryBatch)
        ->Arg(1)
        ->Arg(64)
        ->Arg(2048)
        ->Complexity(benchmark::oN);

    auto * mask_apply = benchmark::RegisterBenchmark("UDTTokenMask/Mask/ValidQuoted", benchmarkMaskApply);
    addInputSizes(mask_apply);
    mask_apply->Complexity(benchmark::oN);

    auto * mask_unterminated = benchmark::RegisterBenchmark("UDTTokenMask/Mask/Unterminated", benchmarkMaskUnterminated);
    addInputSizes(mask_unterminated);
    mask_unterminated->Complexity(benchmark::oN);

    benchmark::RegisterBenchmark("UDTTokenMask/Mask/MalformedPrefix", benchmarkMaskMalformedPrefix)
        ->Arg(16 * 1024)
        ->Arg(256 * 1024)
        ->Complexity(benchmark::oN);

    benchmark::RegisterBenchmark("UDTTokenMask/Mask/MultiValid", benchmarkMaskApplyBatch)
        ->Arg(1)
        ->Arg(64)
        ->Arg(2048)
        ->Complexity(benchmark::oN);

    auto * failure_pipeline = benchmark::RegisterBenchmark("UDTTokenMask/FailurePipeline/Unterminated", benchmarkFailurePipeline);
    addInputSizes(failure_pipeline);
    failure_pipeline->Complexity(benchmark::oN);
#endif
}

}
}

int main(int argc, char ** argv)
{
    try
    {
        DB::registerBenchmarks();
        benchmark::AddCustomContext("suite", "UDT token parser-error masking");
        benchmark::AddCustomContext(
            "comparison_contract",
            "ParseError/Control names are baseline-candidate A/B; direct UDT token masking names are candidate-only scale coverage; CPU "
            "time "
            "is per fixed batch of 8");
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
        std::cerr << "Cannot prepare UDT token masking benchmark fixtures: " << exception.what() << '\n';
        return 1;
    }
}
