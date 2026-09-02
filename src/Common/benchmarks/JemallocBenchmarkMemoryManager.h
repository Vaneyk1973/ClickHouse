#pragma once

#include <benchmark/benchmark.h>

#include <Core/Defines.h>
#include <Core/Types.h>

#include <Common/Jemalloc.h>

#include <algorithm>
#include <limits>
#include <stdexcept>
#include <utility>

namespace DB
{

#if USE_JEMALLOC

class JemallocBenchmarkMemoryManager final : public benchmark::MemoryManager
{
public:
    bool initialize()
    {
        size_t arena_index_size = sizeof(arena_index);
        if (je_mallctl("arenas.create", &arena_index, &arena_index_size, nullptr, 0) != 0)
            return false;

        const String arena_prefix = "stats.arenas." + std::to_string(arena_index);
        small_allocations_name = arena_prefix + ".small.nmalloc";
        large_allocations_name = arena_prefix + ".large.nmalloc";

        UInt64 ignored = 0;
        return refreshStats() && readUInt64(small_allocations_name.c_str(), ignored) && readUInt64(large_allocations_name.c_str(), ignored)
            && readUInt64("thread.allocated", ignored) && readUInt64("thread.deallocated", ignored) && resetPeak();
    }

    void Start() override
    {
        bool disable_tcache = false;
        size_t tcache_enabled_size = sizeof(previous_tcache_enabled);
        requireMallctl("thread.tcache.enabled", &previous_tcache_enabled, &tcache_enabled_size, &disable_tcache, sizeof(disable_tcache));

        size_t previous_arena_size = sizeof(previous_arena);
        requireMallctl("thread.arena", &previous_arena, &previous_arena_size, &arena_index, sizeof(arena_index));
        require(refreshStats(), "cannot refresh allocator statistics");
        allocations_at_start = readArenaAllocations();
        allocated_bytes_at_start = readUInt64("thread.allocated");
        deallocated_bytes_at_start = readUInt64("thread.deallocated");
        require(resetPeak(), "cannot reset thread peak memory");
        active = true;
    }

    void Stop(Result & result) override
    {
        require(active, "memory measurement was not started");

        const UInt64 peak_bytes = readUInt64("thread.peak.read");
        const UInt64 allocated_bytes = readUInt64("thread.allocated") - allocated_bytes_at_start;
        const UInt64 deallocated_bytes = readUInt64("thread.deallocated") - deallocated_bytes_at_start;

        /// The cache is disabled for this separately measured operation, so
        /// the arena request delta is the exact allocation-call count.
        requireMallctl("thread.arena", nullptr, nullptr, &previous_arena, sizeof(previous_arena));
        requireMallctl("thread.tcache.enabled", nullptr, nullptr, &previous_tcache_enabled, sizeof(previous_tcache_enabled));
        active = false;

        require(refreshStats(), "cannot refresh allocator statistics");
        const UInt64 allocations = readArenaAllocations() - allocations_at_start;

        result.num_allocs = toInt64(allocations);
        result.max_bytes_used = toInt64(peak_bytes);
        result.total_allocated_bytes = toInt64(allocated_bytes);
        result.net_heap_growth = signedDifference(allocated_bytes, deallocated_bytes);
    }

private:
    static Int64 toInt64(UInt64 value)
    {
        constexpr UInt64 max_int64 = static_cast<UInt64>(std::numeric_limits<Int64>::max());
        return static_cast<Int64>(std::min(value, max_int64));
    }

    static Int64 signedDifference(UInt64 lhs, UInt64 rhs)
    {
        if (lhs >= rhs)
            return toInt64(lhs - rhs);
        return -toInt64(rhs - lhs);
    }

    static void require(bool condition, const char * message)
    {
        if (!condition)
            throw std::runtime_error(message);
    }

    static void requireMallctl(const char * name, void * old_value, size_t * old_size, void * new_value, size_t new_size)
    {
        if (je_mallctl(name, old_value, old_size, new_value, new_size) != 0)
            throw std::runtime_error("allocator control failed for " + String(name));
    }

    static bool readUInt64(const char * name, UInt64 & value)
    {
        size_t value_size = sizeof(value);
        return je_mallctl(name, &value, &value_size, nullptr, 0) == 0;
    }

    static UInt64 readUInt64(const char * name)
    {
        UInt64 value = 0;
        require(readUInt64(name, value), "cannot read allocator counter");
        return value;
    }

    static bool refreshStats()
    {
        UInt64 epoch = 1;
        size_t epoch_size = sizeof(epoch);
        return je_mallctl("epoch", &epoch, &epoch_size, &epoch, sizeof(epoch)) == 0;
    }

    static bool resetPeak() { return je_mallctl("thread.peak.reset", nullptr, nullptr, nullptr, 0) == 0; }

    UInt64 readArenaAllocations() const { return readUInt64(small_allocations_name.c_str()) + readUInt64(large_allocations_name.c_str()); }

    unsigned arena_index = 0;
    unsigned previous_arena = 0;
    bool previous_tcache_enabled = true;
    String small_allocations_name;
    String large_allocations_name;
    UInt64 allocations_at_start = 0;
    UInt64 allocated_bytes_at_start = 0;
    UInt64 deallocated_bytes_at_start = 0;
    bool active = false;
};

inline JemallocBenchmarkMemoryManager & getJemallocOperationMemoryManager()
{
    static JemallocBenchmarkMemoryManager memory_manager;
    static const bool initialized = memory_manager.initialize();
    if (!initialized)
        throw std::runtime_error("allocator statistics required by this benchmark are unavailable");
    return memory_manager;
}

template <typename Callback>
void exportJemallocOperationMemory(benchmark::State & state, Callback && callback)
{
    benchmark::MemoryManager::Result result;
    auto & memory_manager = getJemallocOperationMemoryManager();
    memory_manager.Start();
    try
    {
        std::forward<Callback>(callback)();
    }
    catch (...)
    {
        memory_manager.Stop(result);
        throw;
    }
    memory_manager.Stop(result);

    state.counters["operation_allocations"] = static_cast<double>(result.num_allocs);
    state.counters["operation_allocated_bytes"] = static_cast<double>(result.total_allocated_bytes);
    state.counters["operation_peak_net_bytes"] = static_cast<double>(result.max_bytes_used);
    state.counters["operation_net_heap_growth"] = static_cast<double>(result.net_heap_growth);
}

#else

template <typename Callback>
void exportJemallocOperationMemory(benchmark::State &, Callback && callback)
{
    std::forward<Callback>(callback)();
}

#endif

}
