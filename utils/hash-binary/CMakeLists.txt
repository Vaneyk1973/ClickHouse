# This is a standalone host tool with no ClickHouse dependencies.
# It must be compilable with the host compiler when a separate native build is required.

if (USE_SEPARATE_NATIVE_BUILD)
    # We don't build `hash-binary` for the target by default in this mode.
    # It is built for the host in the `native` subdirectory during CMake configure.
    # We only need to register it so `add_native_target` knows about it.
    # Use `EXCLUDE_FROM_ALL` so it doesn't get built for the target architecture.
    add_executable(hash-binary EXCLUDE_FROM_ALL hash-binary.cpp)
else()
    add_executable(hash-binary hash-binary.cpp)
endif()

# Mark `hash-binary` for the separate native build.
add_native_target(hash-binary)
