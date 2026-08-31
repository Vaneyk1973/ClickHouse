#include <benchmark/benchmark.h>

#include <Analyzer/QueryTreeBuilder.h>
#include <Analyzer/QueryTreePassManager.h>

#include <Core/NamesAndTypes.h>
#include <Core/Settings.h>
#include <Core/Types.h>

#include <Common/Exception.h>
#include <Common/SipHash.h>
#include <Common/benchmarks/JemallocBenchmarkMemoryManager.h>

#include <DataTypes/DataTypeFactory.h>

#include <Functions/registerFunctions.h>

#include <Interpreters/Context.h>
#include <Interpreters/ExpressionAnalyzer.h>
#include <Interpreters/InterpreterFactory.h>
#include <Interpreters/TreeRewriter.h>

#if CLICKHOUSE_UDT_ANALYSIS_BENCHMARK
#include <Interpreters/UDT/UDTExecutionBoundary.h>
#endif

#include <Parsers/ASTBackupQuery.h>
#include <Parsers/ASTFunction.h>
#include <Parsers/ASTTTLElement.h>
#include <Parsers/Access/ASTCreateMaskingPolicyQuery.h>
#include <Parsers/Access/ASTCreateRowPolicyQuery.h>
#include <Parsers/ExpressionListParsers.h>
#include <Parsers/ParserQuery.h>
#include <Parsers/parseQuery.h>

#include <Poco/Util/MapConfiguration.h>

#include <algorithm>
#include <array>
#include <cstddef>
#include <iostream>
#include <stdexcept>
#include <utility>
#include <vector>

namespace DB::ErrorCodes
{
extern const int BAD_ARGUMENTS;
extern const int SUPPORT_IS_DISABLED;
extern const int TOO_BIG_AST;
}

#if CLICKHOUSE_UDT_ANALYSIS_BENCHMARK
namespace DB::Setting
{
extern const SettingsBool allow_experimental_analyzer;
extern const SettingsBool allow_experimental_user_defined_types;
}
#endif

namespace DB
{
namespace
{

enum class AnalysisOperation : UInt8
{
    QueryTree,
    ExpressionAnalyzer,
    ExecutionBoundary,
    FactoryRoute,
    TypeFactory,
};

struct ASTMetrics
{
    size_t nodes = 0;
    size_t execution_nodes = 0;
    size_t depth = 0;
    size_t execution_depth = 0;
    size_t functions = 0;
    size_t execution_functions = 0;
};

struct AnalysisCase
{
    String name;
    String text;
    AnalysisOperation operation = AnalysisOperation::QueryTree;
    size_t width = 0;
    size_t type_depth = 0;
    size_t max_ast_elements = 1'000'000;
    bool feature_enabled = false;
    int expected_error_code = 0;
    ASTPtr prepared_ast;
    ASTMetrics ast_metrics;
    IASTHash ast_hash{};
    IASTHash output_hash{};
    size_t output_bytes = 0;
    bool prepared = false;
};

class AnalysisEnvironment
{
public:
    AnalysisEnvironment()
        : shared_context(Context::createShared())
        , global_context(Context::createGlobal(shared_context.get()))
    {
        global_context->makeGlobalContext();
        global_context->setPath("/tmp/clickhouse-udt-analysis/");
        global_context->setConfig(Poco::AutoPtr<Poco::Util::MapConfiguration>(new Poco::Util::MapConfiguration));
        global_context->setApplicationType(Context::ApplicationType::LOCAL);
        registerFunctions();

        feature_off_context = Context::createCopy(global_context);
        feature_on_context = Context::createCopy(global_context);
#if CLICKHOUSE_UDT_ANALYSIS_BENCHMARK
        feature_on_context->setSetting("allow_experimental_user_defined_types", 1);
#endif

        factory.registerInterpreter(
            "InterpreterCreateFunctionQuery",
            [](const InterpreterFactory::Arguments &) -> InterpreterFactory::InterpreterPtr { return {}; });
    }

    ContextMutablePtr getContext(bool feature_enabled) const { return feature_enabled ? feature_on_context : feature_off_context; }

    InterpreterFactory & getFactory() { return factory; }

    void shutdown()
    {
        if (!global_context)
            return;

        global_context->shutdown();
        feature_off_context.reset();
        feature_on_context.reset();
        global_context.reset();
        shared_context.reset();
    }

private:
    SharedContextHolder shared_context;
    ContextMutablePtr global_context;
    ContextMutablePtr feature_off_context;
    ContextMutablePtr feature_on_context;
    InterpreterFactory factory;
};

bool environment_created = false;

AnalysisEnvironment & getEnvironment()
{
    static AnalysisEnvironment environment;
    environment_created = true;
    return environment;
}

struct AnalysisEnvironmentShutdownGuard
{
    ~AnalysisEnvironmentShutdownGuard()
    {
        if (environment_created)
            getEnvironment().shutdown();
    }
};

String nestedType(String leaf, size_t depth)
{
    if (depth == 0)
        throw std::invalid_argument("type depth must be positive");
    for (size_t level = 1; level < depth; ++level)
        leaf = "Array(" + leaf + ')';
    return leaf;
}

String makeFunctionExpression(size_t width, bool cast_dense)
{
    if (width == 0)
        throw std::invalid_argument("analysis benchmark width must be positive");

    String expression;
    if (width != 1)
        expression = "tuple(";
    expression.reserve(expression.size() + width * 72);
    for (size_t index = 0; index < width; ++index)
    {
        if (index != 0)
            expression += ", ";
        const String value = std::to_string(index + 1);
        if (cast_dense)
            expression += "CAST(" + value + " AS UInt64)";
        else
            expression += "plus(abs(toInt64(" + value + ")), multiply(toInt64(" + value + "), 2))";
    }
    if (width != 1)
        expression += ')';
    return expression;
}

#if CLICKHOUSE_UDT_ANALYSIS_BENCHMARK

String makeLateStructuredCastExpression(size_t width, const String & type = "app.MissingType")
{
    if (width == 0)
        throw std::invalid_argument("late structured-cast width must be positive");

    if (width == 1)
        return "CAST(1 AS " + type + ')';

    String expression = "tuple(";
    expression.reserve(width * 48 + type.size() + 32);
    for (size_t index = 0; index + 1 < width; ++index)
    {
        if (index != 0)
            expression += ", ";
        expression += "plus(" + std::to_string(index + 1) + ", 1)";
    }
    expression += ", CAST(1 AS " + type + "))";
    return expression;
}

String makeLateStringCastExpression(size_t width, const String & type = "app.MissingType")
{
    if (width == 0)
        throw std::invalid_argument("late string-cast width must be positive");

    if (width == 1)
        return "CAST(1, '" + type + "')";

    String expression = "tuple(";
    expression.reserve(width * 48 + type.size() + 36);
    for (size_t index = 0; index + 1 < width; ++index)
    {
        if (index != 0)
            expression += ", ";
        expression += "plus(" + std::to_string(index + 1) + ", 1)";
    }
    expression += ", CAST(1, '" + type + "'))";
    return expression;
}

#endif

String makeCreateFunction(size_t width, bool cast_dense, bool qualified_target, const String & qualified_type = "app.MissingType")
{
    String query = "CREATE FUNCTION udt_analysis_route AS x -> ";
    if (width != 1)
        query += "tuple(";
    query.reserve(query.size() + width * 48 + 64);
    for (size_t index = 0; index < width; ++index)
    {
        if (index != 0)
            query += ", ";
        if (qualified_target && index + 1 == width)
            query += cast_dense ? "CAST(x, '" + qualified_type + "')" : "CAST(x AS " + qualified_type + ')';
        else if (cast_dense)
            query += "CAST(x AS UInt64)";
        else
            query += "plus(x, " + std::to_string(index + 1) + ')';
    }
    if (width != 1)
        query += ')';
    return query;
}

String makeRepeatedCallCreateFunction(size_t width, const String & function_name)
{
    if (width == 0)
        throw std::invalid_argument("repeated-call width must be positive");

    String query = "CREATE FUNCTION udt_analysis_route AS x -> ";
    if (width != 1)
        query += "tuple(";
    query.reserve(query.size() + width * (function_name.size() + 8));
    for (size_t index = 0; index < width; ++index)
    {
        if (index != 0)
            query += ", ";
        query += function_name + "(x)";
    }
    if (width != 1)
        query += ')';
    return query;
}

String makeRepeatedCallExpression(size_t width, const String & function_name = "identity")
{
    if (width == 0)
        throw std::invalid_argument("repeated-call expression width must be positive");

    String expression;
    if (width != 1)
        expression = "tuple(";
    expression.reserve(expression.size() + width * (function_name.size() + 8));
    for (size_t index = 0; index < width; ++index)
    {
        if (index != 0)
            expression += ", ";
        expression += function_name + "(x)";
    }
    if (width != 1)
        expression += ')';
    return expression;
}

String makeDeepCreateFunction(size_t depth, const String & function_name = "identity")
{
    if (depth == 0)
        throw std::invalid_argument("function depth must be positive");

    String query = "CREATE FUNCTION udt_analysis_route AS x -> ";
    query.reserve(query.size() + depth * (function_name.size() + 2) + 1);
    for (size_t level = 0; level < depth; ++level)
        query += function_name + '(';
    query += 'x';
    query.append(depth, ')');
    return query;
}

String makeRepeatedStringCastCreateFunction(size_t width, const String & type_name)
{
    if (width == 0)
        throw std::invalid_argument("string-cast width must be positive");

    String query = "CREATE FUNCTION udt_analysis_route AS x -> ";
    if (width != 1)
        query += "tuple(";
    query.reserve(query.size() + width * (type_name.size() + 16));
    for (size_t index = 0; index < width; ++index)
    {
        if (index != 0)
            query += ", ";
        query += "CAST(x, '" + type_name + "')";
    }
    if (width != 1)
        query += ')';
    return query;
}

String makeDistinctDottedEnumStringCasts(size_t width)
{
    if (width == 0)
        throw std::invalid_argument("distinct string-cast width must be positive");

    String query = "CREATE FUNCTION udt_analysis_route AS x -> ";
    if (width != 1)
        query += "tuple(";
    query.reserve(query.size() + width * 48);
    for (size_t index = 0; index < width; ++index)
    {
        if (index != 0)
            query += ", ";
        query += "CAST(x, 'Enum8(\\'dotted.value" + std::to_string(index) + "\\' = 1)')";
    }
    if (width != 1)
        query += ')';
    return query;
}

enum class SpecialFieldKind : UInt8
{
    RowPolicyFilter,
    MaskingAssignment,
    BackupPartition,
    BackupClusterHostIds,
    TTLGroupBy,
    TTLAssignment,
};

String makeSpecialFieldQuery(SpecialFieldKind kind, size_t width)
{
    const String expression = makeRepeatedCallExpression(width);
    switch (kind)
    {
        case SpecialFieldKind::RowPolicyFilter: return "CREATE ROW POLICY udt_analysis_row ON db.t USING " + expression + " TO ALL";
        case SpecialFieldKind::MaskingAssignment:
            return "CREATE MASKING POLICY udt_analysis_mask_special ON db.t UPDATE c = " + expression + " TO ALL";
        case SpecialFieldKind::BackupPartition: return "BACKUP TABLE db.t PARTITION " + expression + " TO File('/udt-analysis')";
        case SpecialFieldKind::BackupClusterHostIds:
            return "BACKUP TABLE db.t TO File('/udt-analysis') SETTINGS cluster_host_ids = [" + expression + ']';
        case SpecialFieldKind::TTLGroupBy:
            return "CREATE TABLE udt_analysis_ttl_group (d DateTime, x UInt64) ENGINE = MergeTree ORDER BY x TTL d GROUP BY " + expression
                + " SET x = x";
        case SpecialFieldKind::TTLAssignment:
            return "CREATE TABLE udt_analysis_ttl_assignment (d DateTime, x UInt64) ENGINE = MergeTree ORDER BY x "
                   "TTL d GROUP BY x SET x = "
                + expression;
    }
    throw std::logic_error("unknown special field kind");
}

String makeTTLRecompression(const String & type_name)
{
    return "CREATE TABLE udt_analysis_ttl_recompression (d DateTime, x UInt64) ENGINE = MergeTree ORDER BY x "
           "TTL d RECOMPRESS CODEC(CAST(1, '"
        + type_name + "'))";
}

String makeMaskingPolicy(size_t width)
{
    if (width == 0)
        throw std::invalid_argument("masking-policy width must be positive");

    String query = "CREATE MASKING POLICY udt_analysis_mask ON db.t UPDATE ";
    query.reserve(query.size() + width * 32);
    for (size_t index = 0; index < width; ++index)
    {
        if (index != 0)
            query += ", ";
        query += "c" + std::to_string(index) + " = identity(x)";
    }
    query += " TO ALL";
    return query;
}

String makeLongMalformedDottedType(size_t bytes)
{
    if (bytes < 8)
        throw std::invalid_argument("long dotted type must be at least eight bytes");

    String type_name = "UInt64";
    type_name.append(bytes - type_name.size() - 1, ' ');
    type_name += '.';
    return type_name;
}

#if CLICKHOUSE_UDT_ANALYSIS_BENCHMARK

String makeStructuredCastCreateFunction(size_t width, size_t cast_index, const String & type_name = "app.MissingType")
{
    if (width == 0 || cast_index >= width)
        throw std::invalid_argument("structured-cast position is outside the expression width");

    String query = "CREATE FUNCTION udt_analysis_route AS x -> ";
    if (width != 1)
        query += "tuple(";
    query.reserve(query.size() + width * 18 + type_name.size());
    for (size_t index = 0; index < width; ++index)
    {
        if (index != 0)
            query += ", ";
        if (index == cast_index)
            query += "CAST(x AS " + type_name + ')';
        else
            query += "identity(x)";
    }
    if (width != 1)
        query += ')';
    return query;
}

String makeWideStructuredType(size_t width)
{
    if (width < 2)
        throw std::invalid_argument("wide structured type must have at least two elements");

    String type_name = "Tuple(";
    type_name.reserve(width * 8 + 32);
    for (size_t index = 0; index + 1 < width; ++index)
    {
        if (index != 0)
            type_name += ", ";
        type_name += "UInt64";
    }
    type_name += ", app.MissingType)";
    return type_name;
}

String makeStructuredTypeWithQualifiedElement(size_t width, size_t qualified_index)
{
    if (width == 0 || qualified_index >= width)
        throw std::invalid_argument("qualified type position is outside the type width");

    String type_name = "Tuple(";
    type_name.reserve(width * 8 + 32);
    for (size_t index = 0; index < width; ++index)
    {
        if (index != 0)
            type_name += ", ";
        type_name += index == qualified_index ? "app.MissingType" : "UInt64";
    }
    type_name += ')';
    return type_name;
}

enum class TupleElementSpelling : UInt8
{
    Unnamed,
    Parameterized,
    Named,
    Quoted,
};

String makeTupleTypeWithQualifiedElement(size_t width, size_t qualified_index, TupleElementSpelling spelling)
{
    if (width == 0 || qualified_index >= width)
        throw std::invalid_argument("qualified type position is outside the type width");

    String type_name = "Tuple(";
    type_name.reserve(width * 24 + 32);
    for (size_t index = 0; index < width; ++index)
    {
        if (index != 0)
            type_name += ", ";

        const bool qualified = index == qualified_index;
        switch (spelling)
        {
            case TupleElementSpelling::Unnamed: type_name += qualified ? "app.MissingType" : "UInt64"; break;
            case TupleElementSpelling::Parameterized: type_name += qualified ? "app.MissingType" : "Array(UInt64)"; break;
            case TupleElementSpelling::Named:
                type_name += "c" + std::to_string(index) + ' ' + (qualified ? "app.MissingType" : "UInt64");
                break;
            case TupleElementSpelling::Quoted: type_name += qualified ? "app.MissingType" : "`UInt64`"; break;
        }
    }
    type_name += ')';
    return type_name;
}

#endif

String makeTupleType(size_t width)
{
    String type = "Tuple(";
    type.reserve(width * 16 + 8);
    for (size_t index = 0; index < width; ++index)
    {
        if (index != 0)
            type += ", ";
        type += "c" + std::to_string(index) + " UInt64";
    }
    type += ')';
    return type;
}

ASTPtr parseExpression(const String & text)
{
    ParserExpression parser;
    return parseQuery(
        parser, text, "UDT analysis benchmark expression", 0, DBMS_DEFAULT_MAX_PARSER_DEPTH, DBMS_DEFAULT_MAX_PARSER_BACKTRACKS);
}

ASTPtr parseStatement(const String & text)
{
    ParserQuery parser(text.data() + text.size());
    return parseQuery(
        parser, text, "UDT analysis benchmark statement", 0, DBMS_DEFAULT_MAX_PARSER_DEPTH, DBMS_DEFAULT_MAX_PARSER_BACKTRACKS);
}

DataTypePtr getDataType(const String & text)
{
#if CLICKHOUSE_UDT_ANALYSIS_BENCHMARK
    return DataTypeFactory::instance().getWithFamilyClassification(text);
#else
    return DataTypeFactory::instance().get(text);
#endif
}

void appendExecutionASTChildren(const IAST & ast, size_t child_depth, std::vector<std::pair<const IAST *, size_t>> & pending)
{
    for (const auto & child : ast.children)
        pending.emplace_back(child.get(), child_depth);

    const auto append_if_outside_children = [&](const ASTPtr & hidden_ast)
    {
        if (!hidden_ast)
            return;
        for (const auto & child : ast.children)
            if (child.get() == hidden_ast.get())
                return;
        pending.emplace_back(hidden_ast.get(), child_depth);
    };

    if (const auto * row_policy = ast.as<ASTCreateRowPolicyQuery>())
    {
        for (const auto & [_, filter] : row_policy->filters)
            append_if_outside_children(filter);
    }
    else if (const auto * masking_policy = ast.as<ASTCreateMaskingPolicyQuery>())
    {
        append_if_outside_children(masking_policy->update_assignments);
        append_if_outside_children(masking_policy->where_condition);
    }
    else if (const auto * backup = ast.as<ASTBackupQuery>())
    {
        for (const auto & element : backup->elements)
            if (element.partitions)
                for (const auto & partition : *element.partitions)
                    append_if_outside_children(partition);
        append_if_outside_children(backup->cluster_host_ids);
    }

    if (const auto * ttl = ast.as<ASTTTLElement>())
    {
        for (const auto & expression : ttl->group_by_key)
            append_if_outside_children(expression);
        for (const auto & expression : ttl->group_by_assignments)
            append_if_outside_children(expression);
        append_if_outside_children(ttl->recompression_codec);
    }
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
        for (const auto & child : node->children)
            pending.emplace_back(child.get(), depth + 1);
    }

    pending.emplace_back(ast.get(), 1);
    while (!pending.empty())
    {
        const auto [node, depth] = pending.back();
        pending.pop_back();
        if (!node)
            continue;
        ++metrics.execution_nodes;
        metrics.execution_depth = std::max(metrics.execution_depth, depth);
        metrics.execution_functions += node->as<ASTFunction>() != nullptr;
        appendExecutionASTChildren(*node, depth + 1, pending);
    }
    return metrics;
}

void runCase(const AnalysisCase & benchmark_case)
{
    auto & environment = getEnvironment();
    auto context = environment.getContext(benchmark_case.feature_enabled);

    switch (benchmark_case.operation)
    {
        case AnalysisOperation::QueryTree: {
            auto query_tree = buildQueryTree(benchmark_case.prepared_ast, context);
            QueryTreePassManager pass_manager(context);
            addQueryTreePasses(pass_manager, true);
            pass_manager.runOnlyResolve(query_tree);
            benchmark::DoNotOptimize(query_tree.get());
            return;
        }
        case AnalysisOperation::ExpressionAnalyzer: {
            ASTPtr expression = benchmark_case.prepared_ast->clone();
            auto syntax = TreeRewriter(context).analyze(expression, NamesAndTypesList{});
            ExpressionAnalyzer analyzer(expression, syntax, context);
            auto actions = analyzer.getActionsDAG(false);
            benchmark::DoNotOptimize(&actions);
            return;
        }
        case AnalysisOperation::ExecutionBoundary: {
#if CLICKHOUSE_UDT_ANALYSIS_BENCHMARK
            const auto & settings = context->getSettingsRef();
            auto boundary = UDT::validateUDTExecutionBoundaryAndSize(
                benchmark_case.prepared_ast,
                benchmark_case.max_ast_elements,
                {
                    .allow_experimental_analyzer = static_cast<bool>(settings[Setting::allow_experimental_analyzer]),
                    .allow_experimental_user_defined_types = static_cast<bool>(settings[Setting::allow_experimental_user_defined_types]),
                });
            benchmark::DoNotOptimize(&boundary);
#else
            benchmark::DoNotOptimize(benchmark_case.prepared_ast->checkSize(benchmark_case.max_ast_elements));
#endif
            return;
        }
        case AnalysisOperation::FactoryRoute: {
            ASTPtr query = benchmark_case.prepared_ast;
#if CLICKHOUSE_UDT_ANALYSIS_BENCHMARK
            const auto & settings = context->getSettingsRef();
            auto boundary = UDT::validateUDTExecutionBoundaryAndSize(
                query,
                benchmark_case.max_ast_elements,
                {
                    .allow_experimental_analyzer = static_cast<bool>(settings[Setting::allow_experimental_analyzer]),
                    .allow_experimental_user_defined_types = static_cast<bool>(settings[Setting::allow_experimental_user_defined_types]),
                });
            auto interpreter = environment.getFactory().get(query, context, std::move(boundary));
#else
            query->checkSize(benchmark_case.max_ast_elements);
            auto interpreter = environment.getFactory().get(query, context);
#endif
            benchmark::DoNotOptimize(interpreter.get());
            return;
        }
        case AnalysisOperation::TypeFactory: {
            auto type = getDataType(benchmark_case.text);
            benchmark::DoNotOptimize(type.get());
            return;
        }
    }
}

void runValidatedCase(const AnalysisCase & benchmark_case)
{
    bool saw_expected_error = false;
    try
    {
        runCase(benchmark_case);
    }
    catch (const Exception & exception)
    {
        if (benchmark_case.expected_error_code == 0 || exception.code() != benchmark_case.expected_error_code)
            throw;
        benchmark::DoNotOptimize(exception.code());
        saw_expected_error = true;
    }

    if ((benchmark_case.expected_error_code != 0) != saw_expected_error)
        throw std::logic_error("analysis benchmark fixture did not have its expected outcome");
}

void validateCase(AnalysisCase & benchmark_case)
{
    if (benchmark_case.operation == AnalysisOperation::QueryTree || benchmark_case.operation == AnalysisOperation::ExpressionAnalyzer)
        benchmark_case.prepared_ast = parseExpression(benchmark_case.text);
    else if (
        benchmark_case.operation == AnalysisOperation::ExecutionBoundary || benchmark_case.operation == AnalysisOperation::FactoryRoute)
        benchmark_case.prepared_ast = parseStatement(benchmark_case.text);

    if (benchmark_case.prepared_ast)
    {
        benchmark_case.ast_metrics = collectASTMetrics(benchmark_case.prepared_ast);
        benchmark_case.ast_hash = benchmark_case.prepared_ast->getTreeHash(false);
    }

    if (benchmark_case.operation == AnalysisOperation::TypeFactory && benchmark_case.expected_error_code == 0)
    {
        const String output = getDataType(benchmark_case.text)->getName();
        SipHash hash;
        hash.update(output.data(), output.size());
        benchmark_case.output_hash = getSipHash128AsPair(hash);
        benchmark_case.output_bytes = output.size();
    }

    runValidatedCase(benchmark_case);
    benchmark_case.prepared = true;
}

void exportHash(benchmark::State & state, const char * prefix, const IASTHash & hash)
{
    const UInt64 low = CityHash_v1_0_2::Uint128Low64(hash);
    const UInt64 high = CityHash_v1_0_2::Uint128High64(hash);
    state.counters[String(prefix) + "_high64_high32"] = static_cast<UInt32>(high >> 32);
    state.counters[String(prefix) + "_high64_low32"] = static_cast<UInt32>(high);
    state.counters[String(prefix) + "_low64_high32"] = static_cast<UInt32>(low >> 32);
    state.counters[String(prefix) + "_low64_low32"] = static_cast<UInt32>(low);
}

void benchmarkAnalysis(benchmark::State & state, AnalysisCase * benchmark_case)
{
    if (!benchmark_case->prepared)
        validateCase(*benchmark_case);

    for (auto _ : state)
    {
        static_cast<void>(_);
        try
        {
            runValidatedCase(*benchmark_case);
        }
        catch (const std::exception & exception)
        {
            state.SkipWithError(exception.what());
            break;
        }
    }

    if (!state.error_occurred())
        exportJemallocOperationMemory(state, [&] { runValidatedCase(*benchmark_case); });

    exportHash(state, "ast_hash", benchmark_case->ast_hash);
    exportHash(state, "output_hash", benchmark_case->output_hash);
    state.counters["ast_depth"] = static_cast<double>(benchmark_case->ast_metrics.depth);
    state.counters["ast_nodes"] = static_cast<double>(benchmark_case->ast_metrics.nodes);
    state.counters["classified_factory"] = benchmark_case->operation == AnalysisOperation::TypeFactory ? 1.0 : 0.0;
    state.counters["expected_error_code"] = static_cast<double>(benchmark_case->expected_error_code);
    state.counters["execution_ast_depth"] = static_cast<double>(benchmark_case->ast_metrics.execution_depth);
    state.counters["execution_ast_nodes"] = static_cast<double>(benchmark_case->ast_metrics.execution_nodes);
    state.counters["execution_function_nodes"] = static_cast<double>(benchmark_case->ast_metrics.execution_functions);
    state.counters["feature_enabled"] = benchmark_case->feature_enabled ? 1.0 : 0.0;
    state.counters["function_nodes"] = static_cast<double>(benchmark_case->ast_metrics.functions);
    state.counters["input_bytes"] = static_cast<double>(benchmark_case->text.size());
    state.counters["max_ast_elements"] = static_cast<double>(benchmark_case->max_ast_elements);
    state.counters["output_bytes"] = static_cast<double>(benchmark_case->output_bytes);
    state.counters["type_depth"] = static_cast<double>(benchmark_case->type_depth);
    state.counters["width"] = static_cast<double>(benchmark_case->width);
    state.SetBytesProcessed(static_cast<int64_t>(state.iterations() * benchmark_case->text.size()));
    state.SetItemsProcessed(state.iterations() * std::max<size_t>(benchmark_case->width, 1));
}

void addCase(
    std::vector<AnalysisCase> & cases,
    String name,
    String text,
    AnalysisOperation operation,
    size_t width = 0,
    size_t type_depth = 0,
    bool feature_enabled = false,
    int expected_error_code = 0,
    size_t max_ast_elements = 1'000'000)
{
    cases.push_back({
        .name = std::move(name),
        .text = std::move(text),
        .operation = operation,
        .width = width,
        .type_depth = type_depth,
        .max_ast_elements = max_ast_elements,
        .feature_enabled = feature_enabled,
        .expected_error_code = expected_error_code,
        .prepared_ast = {},
        .ast_metrics = {},
        .ast_hash = {},
        .output_hash = {},
        .output_bytes = 0,
        .prepared = false,
    });
}

std::vector<AnalysisCase> makeCases()
{
    std::vector<AnalysisCase> cases;
    cases.reserve(256);

    for (const size_t width : {1, 128, 1024})
    {
        const String suffix = "/Width" + std::to_string(width);
        addCase(
            cases, "QueryTree/Control/FunctionDense" + suffix, makeFunctionExpression(width, false), AnalysisOperation::QueryTree, width);
        addCase(
            cases,
            "ExpressionAnalyzer/Control/FunctionDense" + suffix,
            makeFunctionExpression(width, false),
            AnalysisOperation::ExpressionAnalyzer,
            width);
        addCase(
            cases,
            "QueryTree/Control/CastDense/FeatureOff" + suffix,
            makeFunctionExpression(width, true),
            AnalysisOperation::QueryTree,
            width);
        addCase(
            cases,
            "QueryTree/Control/CastDense/FeatureOn" + suffix,
            makeFunctionExpression(width, true),
            AnalysisOperation::QueryTree,
            width,
            0,
            true);
        addCase(
            cases,
            "ExpressionAnalyzer/Control/CastDense/FeatureOff" + suffix,
            makeFunctionExpression(width, true),
            AnalysisOperation::ExpressionAnalyzer,
            width);
        addCase(
            cases,
            "ExpressionAnalyzer/Control/CastDense/FeatureOn" + suffix,
            makeFunctionExpression(width, true),
            AnalysisOperation::ExpressionAnalyzer,
            width,
            0,
            true);
    }

    for (const size_t width : {1, 128, 4096})
    {
        const String suffix = "/Width" + std::to_string(width);
        addCase(
            cases,
            "ExecutionBoundary/Control/FunctionDense/FeatureOff" + suffix,
            makeCreateFunction(width, false, false),
            AnalysisOperation::ExecutionBoundary,
            width);
        addCase(
            cases,
            "ExecutionBoundary/Control/FunctionDense/FeatureOn" + suffix,
            makeCreateFunction(width, false, false),
            AnalysisOperation::ExecutionBoundary,
            width,
            0,
            true);
        addCase(
            cases,
            "FactoryRoute/Control/FunctionDense/FeatureOff" + suffix,
            makeCreateFunction(width, false, false),
            AnalysisOperation::FactoryRoute,
            width);
        addCase(
            cases,
            "FactoryRoute/Control/FunctionDense/FeatureOn" + suffix,
            makeCreateFunction(width, false, false),
            AnalysisOperation::FactoryRoute,
            width,
            0,
            true);
    }

    for (const size_t width : {1, 128, 1024})
    {
        addCase(
            cases,
            "ExecutionBoundary/Control/CastDense/FeatureOn/Width" + std::to_string(width),
            makeCreateFunction(width, true, false),
            AnalysisOperation::ExecutionBoundary,
            width,
            0,
            true);
        addCase(
            cases,
            "FactoryRoute/Control/CastDense/FeatureOn/Width" + std::to_string(width),
            makeCreateFunction(width, true, false),
            AnalysisOperation::FactoryRoute,
            width,
            0,
            true);
    }

    const String plain_enum_type = "Enum8(\\'plain_value\\' = 1)";
    const String dotted_enum_type = "Enum8(\\'dotted.value\\' = 1)";
    for (const size_t width : {1, 128, 1024, 4096})
    {
        const String suffix = "/Width" + std::to_string(width);
        addCase(
            cases,
            "ExecutionBoundary/Scaling/StringCastPlainEnum/FeatureOn" + suffix,
            makeRepeatedStringCastCreateFunction(width, plain_enum_type),
            AnalysisOperation::ExecutionBoundary,
            width,
            1,
            true);
        addCase(
            cases,
            "ExecutionBoundary/Scaling/StringCastDottedEnum/FeatureOn" + suffix,
            makeRepeatedStringCastCreateFunction(width, dotted_enum_type),
            AnalysisOperation::ExecutionBoundary,
            width,
            1,
            true);
        addCase(
            cases,
            "ExecutionBoundary/Scaling/StringCastMalformedDotted/FeatureOn" + suffix,
            makeRepeatedStringCastCreateFunction(width, "app."),
            AnalysisOperation::ExecutionBoundary,
            width,
            1,
            true);
    }

    for (const auto & [case_name, function_name] : std::array{
             std::pair{"FastNameMiss", String{"identity"}},
             std::pair{"FullNameMiss", String{"accurateCastOrDefaulx"}},
         })
    {
        addCase(
            cases,
            "ExecutionBoundary/Scaling/" + String{case_name} + "/FeatureOff/Width8192",
            makeRepeatedCallCreateFunction(8192, function_name),
            AnalysisOperation::ExecutionBoundary,
            8192);
        addCase(
            cases,
            "ExecutionBoundary/Scaling/" + String{case_name} + "/FeatureOn/Width8192",
            makeRepeatedCallCreateFunction(8192, function_name),
            AnalysisOperation::ExecutionBoundary,
            8192,
            0,
            true);
    }

    for (const size_t depth : {8, 32, 128, 256, 384})
    {
        addCase(
            cases,
            "ExecutionBoundary/Scaling/DeepFunction/FeatureOn/Depth" + std::to_string(depth),
            makeDeepCreateFunction(depth),
            AnalysisOperation::ExecutionBoundary,
            1,
            depth,
            true);
    }

    for (const size_t bytes : {8, 64, 1024, 4096, 65536, 240000})
    {
        addCase(
            cases,
            "ExecutionBoundary/Scaling/LongMalformedDotted/FeatureOn/Bytes" + std::to_string(bytes),
            makeRepeatedStringCastCreateFunction(1, makeLongMalformedDottedType(bytes)),
            AnalysisOperation::ExecutionBoundary,
            1,
            1,
            true);
    }

    for (const size_t width : {1, 32, 256, 2048})
    {
        addCase(
            cases,
            "ExecutionBoundary/Scaling/StringCastDistinctDottedEnum/FeatureOn/Width" + std::to_string(width),
            makeDistinctDottedEnumStringCasts(width),
            AnalysisOperation::ExecutionBoundary,
            width,
            1,
            true);
    }

    for (const size_t width : {16, 256, 4096, 16384})
    {
        addCase(
            cases,
            "ExecutionBoundary/Scaling/WideFunction/FeatureOff/Width" + std::to_string(width),
            makeRepeatedCallCreateFunction(width, "identity"),
            AnalysisOperation::ExecutionBoundary,
            width);
    }

#if CLICKHOUSE_UDT_ANALYSIS_BENCHMARK
    const int hidden_size_error = ErrorCodes::TOO_BIG_AST;
#else
    const int hidden_size_error = 0;
#endif

    for (const size_t width : {128, 1024, 8192})
    {
        addCase(
            cases,
            "ExecutionBoundary/Scaling/MaskingAssignments/FeatureOn/Width" + std::to_string(width),
            makeMaskingPolicy(width),
            AnalysisOperation::ExecutionBoundary,
            width,
            1,
            true,
            hidden_size_error,
            100);
    }

    constexpr std::array special_fields{
        std::pair{"RowPolicyFilter", SpecialFieldKind::RowPolicyFilter},
        std::pair{"MaskingAssignment", SpecialFieldKind::MaskingAssignment},
        std::pair{"BackupPartition", SpecialFieldKind::BackupPartition},
        std::pair{"BackupClusterHostIds", SpecialFieldKind::BackupClusterHostIds},
        std::pair{"TTLGroupBy", SpecialFieldKind::TTLGroupBy},
        std::pair{"TTLAssignment", SpecialFieldKind::TTLAssignment},
    };
    for (const auto & [field_name, field_kind] : special_fields)
    {
        for (const size_t width : {8, 128, 2048})
        {
            addCase(
                cases,
                "ExecutionBoundary/Scaling/SpecialField/" + String{field_name} + "/FeatureOn/Width" + std::to_string(width),
                makeSpecialFieldQuery(field_kind, width),
                AnalysisOperation::ExecutionBoundary,
                width,
                1,
                true);
        }
        addCase(
            cases,
            "ExecutionBoundary/Scaling/SpecialFieldLimit/" + String{field_name} + "/FeatureOn/Width2048/Limit100",
            makeSpecialFieldQuery(field_kind, 2048),
            AnalysisOperation::ExecutionBoundary,
            2048,
            1,
            true,
            hidden_size_error,
            100);
    }

    for (const size_t bytes : {64, 4096, 65536})
    {
        addCase(
            cases,
            "ExecutionBoundary/Scaling/SpecialField/TTLRecompression/FeatureOn/Bytes" + std::to_string(bytes),
            makeTTLRecompression(makeLongMalformedDottedType(bytes)),
            AnalysisOperation::ExecutionBoundary,
            1,
            1,
            true);
    }

    addCase(
        cases,
        "ExecutionBoundary/Scaling/OversizedFunction/FeatureOff/Width16384",
        makeRepeatedCallCreateFunction(16384, "identity"),
        AnalysisOperation::ExecutionBoundary,
        16384,
        1,
        false,
        ErrorCodes::TOO_BIG_AST,
        10000);

    constexpr size_t oversized_function_nodes = 49'161;
    for (const size_t limit :
         {size_t{1}, size_t{64}, size_t{1024}, oversized_function_nodes - 1, oversized_function_nodes, size_t{1'000'000}})
    {
        addCase(
            cases,
            "ExecutionBoundary/Scaling/ASTLimitSweep/FeatureOff/Width16384/Limit" + std::to_string(limit),
            makeRepeatedCallCreateFunction(16384, "identity"),
            AnalysisOperation::ExecutionBoundary,
            16384,
            1,
            false,
            limit < oversized_function_nodes ? ErrorCodes::TOO_BIG_AST : 0,
            limit);
    }

    addCase(cases, "TypeFactory/Control/UInt64", "UInt64", AnalysisOperation::TypeFactory, 1, 1);
    addCase(cases, "TypeFactory/Control/ArrayDepth32", nestedType("UInt64", 32), AnalysisOperation::TypeFactory, 1, 32);
    addCase(cases, "TypeFactory/Control/TupleWidth100", makeTupleType(100), AnalysisOperation::TypeFactory, 100, 1);

#if CLICKHOUSE_UDT_ANALYSIS_BENCHMARK
    for (const size_t width : {1, 128, 1024})
    {
        const String suffix = "/Width" + std::to_string(width);
        addCase(
            cases,
            "QueryTree/UDT/StructuredLate" + suffix,
            makeLateStructuredCastExpression(width),
            AnalysisOperation::QueryTree,
            width,
            1,
            true,
            ErrorCodes::SUPPORT_IS_DISABLED);
        addCase(
            cases,
            "QueryTree/UDT/StringLate" + suffix,
            makeLateStringCastExpression(width),
            AnalysisOperation::QueryTree,
            width,
            1,
            true,
            ErrorCodes::SUPPORT_IS_DISABLED);
        addCase(
            cases,
            "ExpressionAnalyzer/UDT/StructuredLate" + suffix,
            makeLateStructuredCastExpression(width),
            AnalysisOperation::ExpressionAnalyzer,
            width,
            1,
            true,
            ErrorCodes::SUPPORT_IS_DISABLED);
        addCase(
            cases,
            "ExpressionAnalyzer/UDT/StringLate" + suffix,
            makeLateStringCastExpression(width),
            AnalysisOperation::ExpressionAnalyzer,
            width,
            1,
            true,
            ErrorCodes::SUPPORT_IS_DISABLED);
        addCase(
            cases,
            "FactoryRoute/UDT/StructuredLate" + suffix,
            makeCreateFunction(width, false, true),
            AnalysisOperation::FactoryRoute,
            width,
            1,
            false,
            ErrorCodes::SUPPORT_IS_DISABLED);
        addCase(
            cases,
            "FactoryRoute/UDT/StringLate" + suffix,
            makeCreateFunction(width, true, true),
            AnalysisOperation::FactoryRoute,
            width,
            1,
            true,
            ErrorCodes::SUPPORT_IS_DISABLED);
    }

    for (const size_t width : {256, 4096, 16384})
    {
        const String suffix = "/Width" + std::to_string(width);
        addCase(
            cases,
            "ExecutionBoundary/Scaling/StructuredErrorEarly" + suffix,
            makeStructuredCastCreateFunction(width, 0),
            AnalysisOperation::ExecutionBoundary,
            width,
            1,
            false,
            ErrorCodes::SUPPORT_IS_DISABLED);
        addCase(
            cases,
            "ExecutionBoundary/Scaling/StructuredErrorMiddle" + suffix,
            makeStructuredCastCreateFunction(width, width / 2),
            AnalysisOperation::ExecutionBoundary,
            width,
            1,
            false,
            ErrorCodes::SUPPORT_IS_DISABLED);
        addCase(
            cases,
            "ExecutionBoundary/Scaling/StructuredErrorLate" + suffix,
            makeStructuredCastCreateFunction(width, width - 1),
            AnalysisOperation::ExecutionBoundary,
            width,
            1,
            false,
            ErrorCodes::SUPPORT_IS_DISABLED);
    }

    for (const auto & [position_name, position] : std::array{
             std::pair{"Early", size_t{0}},
             std::pair{"Middle", size_t{2048}},
             std::pair{"Late", size_t{4095}},
         })
    {
        addCase(
            cases,
            "ExecutionBoundary/Scaling/StringQualifiedError" + String{position_name} + "/TypeWidth4096",
            makeRepeatedStringCastCreateFunction(1, makeStructuredTypeWithQualifiedElement(4096, position)),
            AnalysisOperation::ExecutionBoundary,
            1,
            1,
            true,
            ErrorCodes::SUPPORT_IS_DISABLED);
    }

    for (const size_t type_width : {size_t{16}, size_t{256}, size_t{4096}, size_t{16384}})
    {
        addCase(
            cases,
            "ExecutionBoundary/Scaling/StringQualifiedTupleUnnamed/TypeWidth" + std::to_string(type_width),
            makeRepeatedStringCastCreateFunction(
                1, makeTupleTypeWithQualifiedElement(type_width, type_width - 1, TupleElementSpelling::Unnamed)),
            AnalysisOperation::ExecutionBoundary,
            type_width,
            1,
            true,
            ErrorCodes::SUPPORT_IS_DISABLED);
    }

    constexpr std::array spelling_cases{
        std::pair{"Parameterized", TupleElementSpelling::Parameterized},
        std::pair{"NamedControl", TupleElementSpelling::Named},
        std::pair{"QuotedControl", TupleElementSpelling::Quoted},
    };
    for (const auto & [spelling_name, spelling] : spelling_cases)
    {
        for (const size_t type_width : {size_t{256}, size_t{4096}})
        {
            addCase(
                cases,
                "ExecutionBoundary/Scaling/StringQualifiedTuple" + String{spelling_name} + "/TypeWidth" + std::to_string(type_width),
                makeRepeatedStringCastCreateFunction(1, makeTupleTypeWithQualifiedElement(type_width, type_width - 1, spelling)),
                AnalysisOperation::ExecutionBoundary,
                type_width,
                1,
                true,
                ErrorCodes::SUPPORT_IS_DISABLED);
        }
    }

    for (const auto & [position_name, position] : std::array{
             std::pair{"Early", size_t{0}},
             std::pair{"Late", size_t{16383}},
         })
    {
        addCase(
            cases,
            "ExecutionBoundary/Scaling/StructuredError" + String{position_name} + "/Width16384/Limit1024",
            makeStructuredCastCreateFunction(16384, position),
            AnalysisOperation::ExecutionBoundary,
            16384,
            1,
            false,
            ErrorCodes::TOO_BIG_AST,
            1024);
    }

    addCase(
        cases,
        "ExecutionBoundary/Scaling/WideStructuredTarget/Width8192",
        makeStructuredCastCreateFunction(1, 0, makeWideStructuredType(8192)),
        AnalysisOperation::ExecutionBoundary,
        8192,
        1,
        false,
        ErrorCodes::SUPPORT_IS_DISABLED);

    const String nested_missing_type = nestedType("app.MissingType", 32);
    const String nested_built_in_collision = nestedType("app.uInT64", 32);
    for (const auto operation : {AnalysisOperation::QueryTree, AnalysisOperation::ExpressionAnalyzer})
    {
        const String prefix = operation == AnalysisOperation::QueryTree ? "QueryTree" : "ExpressionAnalyzer";
        addCase(
            cases,
            prefix + "/UDT/StructuredNestedMissing/Depth32",
            makeLateStructuredCastExpression(1, nested_missing_type),
            operation,
            1,
            32,
            true,
            ErrorCodes::SUPPORT_IS_DISABLED);
        addCase(
            cases,
            prefix + "/UDT/StructuredNestedBuiltInCollision/Depth32",
            makeLateStructuredCastExpression(1, nested_built_in_collision),
            operation,
            1,
            32,
            true,
            ErrorCodes::BAD_ARGUMENTS);
        addCase(
            cases,
            prefix + "/UDT/StringNestedMissing/Depth32",
            makeLateStringCastExpression(1, nested_missing_type),
            operation,
            1,
            32,
            true,
            ErrorCodes::SUPPORT_IS_DISABLED);
    }

    addCase(
        cases,
        "FactoryRoute/UDT/StructuredNestedMissing/Depth32",
        makeCreateFunction(1, false, true, nested_missing_type),
        AnalysisOperation::FactoryRoute,
        1,
        32,
        false,
        ErrorCodes::SUPPORT_IS_DISABLED);
    addCase(
        cases,
        "FactoryRoute/UDT/StructuredNestedBuiltInCollision/Depth32",
        makeCreateFunction(1, false, true, nested_built_in_collision),
        AnalysisOperation::FactoryRoute,
        1,
        32,
        false,
        ErrorCodes::BAD_ARGUMENTS);
    addCase(
        cases,
        "FactoryRoute/UDT/StringNestedMissing/Depth32",
        makeCreateFunction(1, true, true, nested_missing_type),
        AnalysisOperation::FactoryRoute,
        1,
        32,
        true,
        ErrorCodes::SUPPORT_IS_DISABLED);
    addCase(
        cases,
        "FactoryRoute/UDT/MalformedDottedStringDeferred/Width1",
        makeCreateFunction(1, true, true, "app."),
        AnalysisOperation::FactoryRoute,
        1,
        1,
        true);
    addCase(
        cases,
        "TypeFactory/UDT/QualifiedMissing",
        "app.MissingType",
        AnalysisOperation::TypeFactory,
        1,
        1,
        false,
        ErrorCodes::SUPPORT_IS_DISABLED);
    addCase(
        cases,
        "TypeFactory/UDT/QualifiedBuiltInCollision",
        "app.uInT64",
        AnalysisOperation::TypeFactory,
        1,
        1,
        false,
        ErrorCodes::BAD_ARGUMENTS);
    addCase(
        cases,
        "TypeFactory/UDT/QualifiedMissing/Depth32",
        nested_missing_type,
        AnalysisOperation::TypeFactory,
        1,
        32,
        false,
        ErrorCodes::SUPPORT_IS_DISABLED);
    addCase(
        cases,
        "TypeFactory/UDT/QualifiedBuiltInCollision/Depth32",
        nested_built_in_collision,
        AnalysisOperation::TypeFactory,
        1,
        32,
        false,
        ErrorCodes::BAD_ARGUMENTS);
#endif

    return cases;
}

}
}

int main(int argc, char ** argv)
{
    DB::AnalysisEnvironmentShutdownGuard environment_shutdown_guard;
    try
    {
        auto cases = DB::makeCases();
        for (auto & benchmark_case : cases)
            benchmark::RegisterBenchmark(("UDTAnalysis/" + benchmark_case.name).c_str(), DB::benchmarkAnalysis, &benchmark_case);

        benchmark::AddCustomContext("suite", "UDT analysis and pre-side-effect boundaries");
        benchmark::AddCustomContext(
            "comparison_contract", "Control names are baseline-candidate A/B; UDT rejection names are candidate-only characterization");
        benchmark::AddCustomContext(
            "factory_route_contract", "FactoryRoute measures AST size/boundary validation followed by one-shot interpreter dispatch");
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
        std::cerr << "Cannot prepare UDT analysis benchmark fixtures: " << exception.what() << '\n';
        return 1;
    }
}
