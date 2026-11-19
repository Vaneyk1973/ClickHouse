#pragma once

#include <filesystem>
#include <mutex>
#include <optional>

#include <base/types.h>


namespace DB
{

class ASTCreateTypeQuery;

/// Server-wide definitions expanded to built-in types by `CREATE TABLE`.
class UserDefinedTypeCatalog
{
public:
    explicit UserDefinedTypeCatalog(const String & data_path);

    void create(const ASTCreateTypeQuery & query, bool fsync_metadata);
    void drop(const String & name, bool if_exists, bool fsync_metadata);
    [[nodiscard]] std::optional<String> tryGetBaseType(const String & name) const;

private:
    const std::filesystem::path directory;
    mutable std::mutex mutex;
    bool directory_entry_synced = false;
};

}
