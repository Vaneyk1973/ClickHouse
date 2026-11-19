#pragma once

#include <filesystem>
#include <mutex>

#include <Interpreters/Context_fwd.h>
#include <Parsers/IAST_fwd.h>


namespace DB
{

class ASTCreateTypeQuery;

/// Server-wide definitions expanded to built-in types by `CREATE TABLE`.
class UserDefinedTypeCatalog
{
public:
    explicit UserDefinedTypeCatalog(const String & data_path);

    void create(const ASTCreateTypeQuery & query, const ContextPtr & context, bool fsync_metadata);
    void drop(const String & name, bool if_exists, bool fsync_metadata);

    /// Returns a freshly parsed `ASTCreateTypeQuery` or null if the name is absent.
    [[nodiscard]] ASTPtr tryGetDefinition(const String & name) const;

private:
    const std::filesystem::path directory;
    mutable std::mutex mutex;
    bool directory_entry_synced = false;
};

}
