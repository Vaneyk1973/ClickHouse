#include <benchmark/benchmark.h>

#include <DataTypes/IDataType.h>
#include <DataTypes/UDT/IAuthorityAdapter.h>
#include <DataTypes/UDT/TemplateChecker.h>
#include <DataTypes/UDT/TypeResolver.h>
#include <DataTypes/UDT/Catalog.h>

#include <Core/Field.h>

#include <Parsers/ASTDataType.h>
#include <Parsers/ASTExpressionList.h>
#include <Parsers/ASTLiteral.h>

#include <algorithm>
#include <array>
#include <cstddef>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <tuple>
#include <unordered_map>
#include <utility>
#include <vector>

namespace DB::UDT
{
namespace
{

UUID makeUUID(UInt64 high, UInt64 low)
{
    UUID result{};
    UUIDHelpers::getHighBytes(result) = high;
    UUIDHelpers::getLowBytes(result) = low;
    return result;
}

const UUID & benchmarkDatabaseUUID()
{
    static const UUID value = makeUUID(0x7d930777ec754eafULL, 0xa791621fbdbed001ULL);
    return value;
}

DefinitionIdentity benchmarkIdentity(UInt64 ordinal, UInt64 revision = 1)
{
    return {
        .database_uuid = benchmarkDatabaseUUID(),
        .type_uuid = makeUUID(0xa8dc004956314cf8ULL, ordinal + 1),
        .revision = revision,
    };
}

TemplateNode builtIn(String family)
{
    TemplateNode result;
    result.kind = TemplateNodeKind::BuiltIn;
    result.atom = std::move(family);
    return result;
}

DefinitionInput scalarDefinitionInput(UInt64 ordinal, UInt64 revision = 1, String qualifier = "bench")
{
    DefinitionInput result;
    result.identity = benchmarkIdentity(ordinal, revision);
    result.normalized_local_name = "CatalogType" + std::to_string(ordinal);
    result.normalized_name = std::move(qualifier) + "." + result.normalized_local_name;
    result.nodes.push_back(builtIn("UInt64"));
    return result;
}

DefinitionInput typeAliasDefinitionInput(UInt64 ordinal)
{
    DefinitionInput result;
    result.identity = benchmarkIdentity(ordinal);
    result.normalized_local_name = "BindingType" + std::to_string(ordinal);
    result.normalized_name = "bench." + result.normalized_local_name;
    result.parameters = {{.normalized_name = "T", .kind = ParameterKind::Type}};

    TemplateNode formal;
    formal.kind = TemplateNodeKind::TypeParameter;
    formal.parameter = 0;
    result.nodes.push_back(std::move(formal));
    return result;
}

TemplateCheckerLimits checkerLimits(UInt64 definition_count, UInt64 node_count)
{
    TemplateCheckerLimits result;
    result.maximum_definitions = std::max(result.maximum_definitions, definition_count);
    result.maximum_catalog_nodes = std::max(result.maximum_catalog_nodes, node_count);
    result.maximum_catalog_input_bytes = 512ULL << 20;
    result.maximum_catalog_checker_work = std::max(result.maximum_catalog_checker_work, node_count * 128);
    result.maximum_canonical_catalog_bytes = 256ULL << 20;
    result.maximum_scratch_bytes = 512ULL << 20;
    return result;
}

std::vector<Definition::Ptr>
checkDefinitions(std::vector<DefinitionInput> inputs, TemplateCheckerStatistics * statistics = nullptr)
{
    UInt64 node_count = 0;
    for (const auto & input : inputs)
        node_count += input.nodes.size();
    const auto limits = checkerLimits(inputs.size(), node_count);
    return TemplateChecker::checkAll(std::move(inputs), limits, statistics);
}

std::vector<DefinitionInput> makeCatalogInputs(std::size_t definition_count)
{
    std::vector<DefinitionInput> result;
    result.reserve(definition_count);
    for (std::size_t index = 0; index < definition_count; ++index)
        result.push_back(scalarDefinitionInput(index));
    return result;
}

std::vector<DefinitionInput> makeCheckerInputs(std::size_t leaf_count)
{
    DefinitionInput input;
    input.identity = benchmarkIdentity(0x100000);
    input.normalized_name = "bench.CheckerWork";
    input.normalized_local_name = "CheckerWork";

    if (leaf_count == 1)
    {
        input.nodes.push_back(builtIn("UInt64"));
    }
    else
    {
        auto tuple = builtIn("Tuple");
        tuple.children.reserve(leaf_count);
        for (std::size_t index = 0; index < leaf_count; ++index)
            tuple.children.push_back({.reference = static_cast<TemplateNodeID>(index + 1), .label = {}});
        input.nodes.push_back(std::move(tuple));
        for (std::size_t index = 0; index < leaf_count; ++index)
            input.nodes.push_back(builtIn("UInt64"));
    }
    return {std::move(input)};
}

void exportCheckerCounters(benchmark::State & state, const TemplateCheckerStatistics & statistics)
{
    state.counters["accepted_bytes"] = static_cast<double>(statistics.accepted_input_bytes);
    state.counters["canonical_bytes"] = static_cast<double>(statistics.canonical_bytes);
    state.counters["charged_work"] = static_cast<double>(statistics.charged_work);
    state.counters["checked_definitions"] = static_cast<double>(statistics.checked_definitions);
    state.counters["scratch_peak_bytes"] = static_cast<double>(statistics.scratch_peak_bytes);
}

void BM_TemplateCheckerChargedWork(benchmark::State & state)
{
    const auto prepared_inputs = makeCheckerInputs(static_cast<std::size_t>(state.range(0)));
    const auto limits = checkerLimits(prepared_inputs.size(), prepared_inputs.front().nodes.size());

    TemplateCheckerStatistics last_statistics;
    for (auto _ : state)
    {
        static_cast<void>(_);
        state.PauseTiming();
        auto inputs = prepared_inputs;
        state.ResumeTiming();

        TemplateCheckerStatistics statistics;
        auto checked = TemplateChecker::checkAll(std::move(inputs), limits, &statistics);
        benchmark::DoNotOptimize(checked);

        state.PauseTiming();
        last_statistics = statistics;
        checked.clear();
        state.ResumeTiming();
    }

    exportCheckerCounters(state, last_statistics);
    state.SetItemsProcessed(static_cast<int64_t>(state.iterations() * last_statistics.charged_work));
}

struct CatalogFixture
{
    std::vector<Definition::Ptr> definitions;
    TypeCatalogBuildLimits limits;
    TypeCatalogRoot::Ptr root;
    Definition::Ptr replacement;
};

std::shared_ptr<const CatalogFixture> makeCatalogFixture(std::size_t definition_count)
{
    auto fixture = std::make_shared<CatalogFixture>();
    fixture->definitions = checkDefinitions(makeCatalogInputs(definition_count));
    fixture->root = TypeCatalogBuilder::build(1, fixture->definitions, fixture->limits);
    fixture->replacement = checkDefinitions({scalarDefinitionInput(0, 1, "renamed")}).front();
    return fixture;
}

std::shared_ptr<const CatalogFixture> catalogFixture(std::size_t definition_count)
{
    static std::mutex mutex;
    static std::unordered_map<std::size_t, std::shared_ptr<const CatalogFixture>> fixtures;

    std::lock_guard lock(mutex);
    const auto existing = fixtures.find(definition_count);
    if (existing != fixtures.end())
        return existing->second;
    auto fixture = makeCatalogFixture(definition_count);
    return fixtures.emplace(definition_count, std::move(fixture)).first->second;
}

void exportCatalogCounters(benchmark::State & state, const CatalogFixture & fixture)
{
    state.counters["definitions"] = static_cast<double>(fixture.definitions.size());
    state.counters["root_bytes"] = static_cast<double>(fixture.root->getAccountedBytes());
    state.counters["shards"] = static_cast<double>(fixture.root->getShardCount());
}

void BM_TypeCatalogBulkBuild(benchmark::State & state)
{
    const auto fixture = catalogFixture(static_cast<std::size_t>(state.range(0)));

    UInt64 last_root_bytes = 0;
    for (auto _ : state)
    {
        static_cast<void>(_);
        auto root = TypeCatalogBuilder::build(2, fixture->definitions, fixture->limits);
        benchmark::DoNotOptimize(root);

        state.PauseTiming();
        last_root_bytes = root->getAccountedBytes();
        root.reset();
        state.ResumeTiming();
    }

    exportCatalogCounters(state, *fixture);
    state.counters["built_root_bytes"] = static_cast<double>(last_root_bytes);
    state.SetItemsProcessed(static_cast<int64_t>(state.iterations() * fixture->definitions.size()));
}

void BM_TypeCatalogLookup(benchmark::State & state)
{
    const auto fixture = catalogFixture(static_cast<std::size_t>(state.range(0)));

    std::size_t index = 0;
    for (auto _ : state)
    {
        static_cast<void>(_);
        const auto & definition = fixture->definitions[index];
        const auto by_name = fixture->root->findByName(definition->getNormalizedLocalName());
        const auto by_identity = fixture->root->findByIdentity(definition->getIdentity());
        benchmark::DoNotOptimize(by_name);
        benchmark::DoNotOptimize(by_identity);
        if (++index == fixture->definitions.size())
            index = 0;
    }

    exportCatalogCounters(state, *fixture);
    state.counters["lookups_per_iteration"] = 2;
    state.SetItemsProcessed(static_cast<int64_t>(state.iterations() * 2));
}

void BM_TypeCatalogOneShardMutation(benchmark::State & state)
{
    const auto fixture = catalogFixture(static_cast<std::size_t>(state.range(0)));
    const auto mutation = TypeCatalogMutation::replace(fixture->definitions.front()->getIdentity(), fixture->replacement);
    auto probe = TypeCatalogBuilder::applyMutation(*fixture->root, 2, mutation, fixture->limits);
    UInt64 copied_identity_shards = 0;
    UInt64 copied_name_shards = 0;
    for (std::size_t shard = 0; shard < fixture->root->getShardCount(); ++shard)
    {
        copied_identity_shards += !fixture->root->sharesIdentityShardWith(*probe, shard);
        copied_name_shards += !fixture->root->sharesNameShardWith(*probe, shard);
    }
    if (copied_identity_shards != 1 || copied_name_shards != 1)
        throw std::logic_error("one logical catalog replacement did not copy exactly one shard per index");
    probe.reset();

    UInt64 last_root_bytes = 0;
    for (auto _ : state)
    {
        static_cast<void>(_);
        auto root = TypeCatalogBuilder::applyMutation(*fixture->root, 2, mutation, fixture->limits);
        benchmark::DoNotOptimize(root);

        state.PauseTiming();
        last_root_bytes = root->getAccountedBytes();
        root.reset();
        state.ResumeTiming();
    }

    exportCatalogCounters(state, *fixture);
    state.counters["copied_identity_shards"] = static_cast<double>(copied_identity_shards);
    state.counters["copied_name_shards"] = static_cast<double>(copied_name_shards);
    state.counters["mutated_root_bytes"] = static_cast<double>(last_root_bytes);
    state.SetItemsProcessed(state.iterations());
}

TypeAuthorityCapabilities transientCapabilities(UInt64 definition_count)
{
    TypeAuthorityCapabilities result;
    result.mask = typeAuthorityCapabilityBit(TypeAuthorityCapability::TransientResolution)
        | typeAuthorityCapabilityBit(TypeAuthorityCapability::Limits) | typeAuthorityCapabilityBit(TypeAuthorityCapability::Templates);
    result.limits = {
        .maximum_definitions = definition_count,
        .maximum_definition_bytes = 1ULL << 20,
        .maximum_template_nodes = 4'096,
        .maximum_direct_dependencies = 256,
        .maximum_transitive_dependencies = 1'024,
        .maximum_checker_work = 65'536,
    };
    return result;
}

struct BindingFixture
{
    ASTPtr declaration;
    std::vector<DeclaredTypeReferenceInput> references;
    AuthorityAdapterPtr authority;
};

std::shared_ptr<const BindingFixture>
makeBindingFixture(std::size_t occurrences, std::size_t distinct_definitions, std::size_t distinct_specializations)
{
    if (occurrences == 0 || distinct_definitions == 0 || distinct_specializations == 0 || distinct_definitions > distinct_specializations
        || distinct_specializations > occurrences || distinct_specializations % distinct_definitions != 0)
        throw std::invalid_argument("binding benchmark requires 0 < D <= S <= K and S divisible by D");

    std::vector<DefinitionInput> definition_inputs;
    definition_inputs.reserve(distinct_definitions);
    for (std::size_t index = 0; index < distinct_definitions; ++index)
        definition_inputs.push_back(typeAliasDefinitionInput(index));
    auto definitions = checkDefinitions(std::move(definition_inputs));

    std::vector<ASTPtr> physical_arguments;
    std::vector<CanonicalTypeArguments> canonical_arguments;
    physical_arguments.reserve(distinct_specializations);
    canonical_arguments.reserve(distinct_specializations);
    for (std::size_t specialization = 0; specialization < distinct_specializations; ++specialization)
    {
        const std::size_t definition_index = specialization % distinct_definitions;
        const std::size_t argument_index = specialization / distinct_definitions;
        ASTPtr physical = makeASTDataType("FixedString", make_intrusive<ASTLiteral>(Field(static_cast<UInt64>(argument_index + 1))));
        std::vector<CanonicalTypeArgumentValue> actuals;
        actuals.push_back(CanonicalTypeArgumentValue::type(physical));
        canonical_arguments.push_back(CanonicalTypeArguments::validate(definitions[definition_index]->getParameters(), std::move(actuals)));
        physical_arguments.push_back(std::move(physical));
    }

    auto declaration = make_intrusive<ASTDataType>();
    declaration->name = "Tuple";
    auto arguments = make_intrusive<ASTExpressionList>();
    arguments->children.reserve(occurrences);
    declaration->children.push_back(arguments);

    auto fixture = std::make_shared<BindingFixture>();
    fixture->declaration = declaration;
    fixture->references.reserve(occurrences);
    for (std::size_t occurrence = 0; occurrence < occurrences; ++occurrence)
    {
        const std::size_t specialization = occurrence % distinct_specializations;
        const std::size_t definition_index = specialization % distinct_definitions;
        ASTPtr marker = makeASTDataType(definitions[definition_index]->getNormalizedName(), physical_arguments[specialization]->clone());
        arguments->children.push_back(marker);
        fixture->references.push_back({
            .reference_node = marker.get(),
            .definition_identity = definitions[definition_index]->getIdentity(),
            .canonical_arguments = canonical_arguments[specialization],
            .type_argument_lineage = {},
        });
    }

    fixture->authority = makeTransientAuthorityAdapter(
        benchmarkDatabaseUUID(), transientCapabilities(definitions.size()), std::move(definitions));
    return fixture;
}

using BindingKey = std::tuple<std::size_t, std::size_t, std::size_t>;

struct BindingKeyHash
{
    std::size_t operator()(const BindingKey & key) const noexcept
    {
        const auto [occurrences, definitions, specializations] = key;
        return occurrences ^ (definitions << 1) ^ (specializations << 7);
    }
};

std::shared_ptr<const BindingFixture> bindingFixture(std::size_t occurrences, std::size_t definitions, std::size_t specializations)
{
    static std::mutex mutex;
    static std::unordered_map<BindingKey, std::shared_ptr<const BindingFixture>, BindingKeyHash> fixtures;

    std::lock_guard lock(mutex);
    const BindingKey key{occurrences, definitions, specializations};
    const auto existing = fixtures.find(key);
    if (existing != fixtures.end())
        return existing->second;
    auto fixture = makeBindingFixture(occurrences, definitions, specializations);
    return fixtures.emplace(key, std::move(fixture)).first->second;
}

void exportBindingCounters(benchmark::State & state, const TypeResolverStatistics & statistics)
{
    state.counters["bound_nodes"] = static_cast<double>(statistics.bound_nodes);
    state.counters["definition_lookups"] = static_cast<double>(statistics.specializer.definition_lookups);
    state.counters["distinct_definitions"] = static_cast<double>(statistics.specializer.distinct_definition_handles);
    state.counters["distinct_specializations"] = static_cast<double>(statistics.specializer.distinct_specializations);
    state.counters["logical_occurrences"] = static_cast<double>(statistics.logical_occurrences);
    state.counters["memo_hits"] = static_cast<double>(statistics.specializer.specialization_memo_hits);
    state.counters["physical_factory_calls"] = static_cast<double>(statistics.physical_factory_calls);
    state.counters["specialization_requests"] = static_cast<double>(statistics.specializer.specialization_requests);
    state.counters["specializer_work"] = static_cast<double>(statistics.specializer.charged_work);
}

void BM_TypeResolverBuiltInFastPath(benchmark::State & state)
{
    const ASTPtr declaration = makeASTDataType("Tuple", makeASTDataType("UInt64"), makeASTDataType("Array", makeASTDataType("String")));
    const auto unsupported = makeUnsupportedAuthorityAdapter();
    TypeResolverLimits deliberately_invalid_udt_limits;
    deliberately_invalid_udt_limits.maximum_input_references = 0;

    TypeResolverStatistics last_statistics;
    for (auto _ : state)
    {
        static_cast<void>(_);
        auto result = TypeResolver::resolve(declaration, {}, *unsupported, deliberately_invalid_udt_limits, &last_statistics);
        benchmark::DoNotOptimize(result.getPhysicalType());
    }

    exportBindingCounters(state, last_statistics);
    state.counters["udt_state_created"] = static_cast<double>(last_statistics.input_references != 0);
    state.SetItemsProcessed(state.iterations());
}

void BM_TypeResolverActivatedBinding(benchmark::State & state)
{
    const auto occurrences = static_cast<std::size_t>(state.range(0));
    const auto definitions = static_cast<std::size_t>(state.range(1));
    const auto specializations = static_cast<std::size_t>(state.range(2));
    const auto fixture = bindingFixture(occurrences, definitions, specializations);

    TypeResolverStatistics last_statistics;
    for (auto _ : state)
    {
        static_cast<void>(_);
        TypeResolverStatistics statistics;
        auto result = TypeResolver::resolve(fixture->declaration, fixture->references, *fixture->authority, {}, &statistics);
        benchmark::DoNotOptimize(result.getPhysicalType());

        state.PauseTiming();
        last_statistics = statistics;
        result = BoundDeclaredTypeResult::physicalOnly(result.getPhysicalType());
        state.ResumeTiming();
    }

    exportBindingCounters(state, last_statistics);
    state.counters["D"] = static_cast<double>(definitions);
    state.counters["K"] = static_cast<double>(occurrences);
    state.counters["S"] = static_cast<double>(specializations);
    state.SetItemsProcessed(static_cast<int64_t>(state.iterations() * occurrences));
}

struct BoundResultFixture
{
    explicit BoundResultFixture(BoundDeclaredTypeResult result_)
        : result(std::move(result_))
    {
    }

    BoundDeclaredTypeResult result;
};

std::shared_ptr<const BoundResultFixture>
makeReleasedBoundResultFixture(std::size_t occurrences, std::size_t definitions, std::size_t specializations)
{
    auto binding = makeBindingFixture(occurrences, definitions, specializations);
    auto result = TypeResolver::resolve(binding->declaration, binding->references, *binding->authority);
    if (!result.hasLogicalTree())
        throw std::logic_error("activated binding benchmark produced no logical tree");
    binding.reset();
    return std::make_shared<BoundResultFixture>(std::move(result));
}

std::shared_ptr<const BoundResultFixture>
releasedBoundResultFixture(std::size_t occurrences, std::size_t definitions, std::size_t specializations)
{
    static std::mutex mutex;
    static std::unordered_map<BindingKey, std::shared_ptr<const BoundResultFixture>, BindingKeyHash> fixtures;

    std::lock_guard lock(mutex);
    const BindingKey key{occurrences, definitions, specializations};
    const auto existing = fixtures.find(key);
    if (existing != fixtures.end())
        return existing->second;
    auto fixture = makeReleasedBoundResultFixture(occurrences, definitions, specializations);
    return fixtures.emplace(key, std::move(fixture)).first->second;
}

void exportBoundResultCounters(benchmark::State & state, const BoundResultFixture & fixture)
{
    const auto & tree = fixture.result.getLogicalTree();
    state.counters["authority_released"] = 1;
    state.counters["bound_nodes"] = static_cast<double>(tree->getNodeCount());
    state.counters["descriptors"] = static_cast<double>(tree->getDescriptors().size());
    state.counters["retained_definitions"] = static_cast<double>(tree->getDefinitionHandles().size());
}

void BM_OrdinaryBoundResultAccess(benchmark::State & state)
{
    const auto fixture = releasedBoundResultFixture(
        static_cast<std::size_t>(state.range(0)), static_cast<std::size_t>(state.range(1)), static_cast<std::size_t>(state.range(2)));

    for (auto _ : state)
    {
        static_cast<void>(_);
        benchmark::DoNotOptimize(fixture->result.getPhysicalType());
        benchmark::DoNotOptimize(fixture->result.hasLogicalTree());
    }

    exportBoundResultCounters(state, *fixture);
    state.SetItemsProcessed(state.iterations());
}

void BM_BoundResultRetentionAfterAuthorityRelease(benchmark::State & state)
{
    const auto fixture = releasedBoundResultFixture(
        static_cast<std::size_t>(state.range(0)), static_cast<std::size_t>(state.range(1)), static_cast<std::size_t>(state.range(2)));

    const auto & tree = fixture->result.getLogicalTree();
    std::size_t index = 0;
    for (auto _ : state)
    {
        static_cast<void>(_);
        const auto & definition = tree->getDefinitionHandles()[index];
        benchmark::DoNotOptimize(definition->getIdentity());
        benchmark::DoNotOptimize(tree->getDescriptors()[index]->getPhysicalType());
        if (++index == tree->getDefinitionHandles().size())
            index = 0;
    }

    exportBoundResultCounters(state, *fixture);
    state.SetItemsProcessed(state.iterations());
}

BENCHMARK(BM_TemplateCheckerChargedWork)->Arg(1)->Arg(32)->Arg(1'024)->Unit(benchmark::kMicrosecond);

BENCHMARK(BM_TypeCatalogBulkBuild)->Arg(1)->Arg(10'000)->Arg(100'000)->Unit(benchmark::kMillisecond);
BENCHMARK(BM_TypeCatalogLookup)->Arg(1)->Arg(10'000)->Arg(100'000);
BENCHMARK(BM_TypeCatalogOneShardMutation)->Arg(1)->Arg(10'000)->Arg(100'000)->Unit(benchmark::kMicrosecond);

BENCHMARK(BM_TypeResolverBuiltInFastPath);
BENCHMARK(BM_TypeResolverActivatedBinding)->Args({1, 1, 1})->Args({100, 1, 1})->Args({10'000, 10, 100})->Unit(benchmark::kMicrosecond);
BENCHMARK(BM_OrdinaryBoundResultAccess)->Args({10'000, 10, 100});
BENCHMARK(BM_BoundResultRetentionAfterAuthorityRelease)->Args({10'000, 10, 100});

}
}

BENCHMARK_MAIN();
