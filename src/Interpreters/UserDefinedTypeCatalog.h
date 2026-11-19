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
    void drop(const String & name, bool if_exists);
    [[nodiscard]] std::optional<String> tryGetBaseType(const String & name) const;

private:
    [[nodiscard]] std::filesystem::path getFilePath(const String & name) const;

    const std::filesystem::path directory;
    mutable std::mutex mutex;
};

}
