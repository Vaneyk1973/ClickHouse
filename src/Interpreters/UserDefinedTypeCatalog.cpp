#include <Interpreters/UserDefinedTypeCatalog.h>

#include <algorithm>
#include <optional>

#include <Common/Exception.h>
#include <Common/SipHash.h>
#include <Common/StringUtils.h>
#include <Common/atomicRename.h>
#include <Common/escapeForFileName.h>
#include <Common/filesystemHelpers.h>
#include <Common/quoteString.h>
#include <DataTypes/DataTypeFactory.h>
#include <Disks/LocalDirectorySyncGuard.h>
#include <IO/ReadBufferFromFile.h>
#include <IO/ReadHelpers.h>
#include <IO/WriteBufferFromFile.h>
#include <IO/WriteHelpers.h>
#include <Interpreters/validateUserDefinedTypeCheck.h>
#include <Parsers/ASTColumnDeclaration.h>
#include <Parsers/ASTCreateTypeQuery.h>
#include <Parsers/ASTDataType.h>
#include <Parsers/ParserCreateQuery.h>
#include <Parsers/ParserCreateTypeQuery.h>
#include <Parsers/parseQuery.h>


namespace fs = std::filesystem;

namespace DB
{

namespace ErrorCodes
{
    extern const int BAD_ARGUMENTS;
    extern const int CORRUPTED_DATA;
    extern const int UNKNOWN_TYPE;
}

namespace
{

void validateBaseType(const ASTCreateTypeQuery & query)
{
    const auto & base_type = query.base_type;
    if (base_type != "Int64" && base_type != "UInt64" && base_type != "String")
        throw Exception(
            ErrorCodes::BAD_ARGUMENTS, "User-defined type {} must have base type Int64, UInt64, or String", backQuote(query.name));
}

bool canUseAsColumnType(const String & name)
{
    /// `ASTDataType` prints the name unquoted. A leading dollar tag can consume later tokens as a heredoc.
    if (!std::ranges::all_of(name, [](char c) { return isWordCharASCII(c) || c == '$'; })
        || (name.starts_with('$') && name.find('$', 1) != String::npos))
        return false;

    const String declaration = "x " + name;
    const char * begin = declaration.data();
    String parse_error;
    ParserColumnDeclaration parser{true, true};
    auto ast = tryParseQuery(parser, begin, declaration.data() + declaration.size(), parse_error, false, "column declaration", false, 0, 0, 0, true);
    auto type = ast ? ast->as<ASTColumnDeclaration &>().getType() : nullptr;
    const auto * data_type = type ? type->as<ASTDataType>() : nullptr;
    return data_type && data_type->name == name;
}

void validateName(const String & name)
{
    if (!canUseAsColumnType(name))
        throw Exception(ErrorCodes::BAD_ARGUMENTS, "Invalid user-defined type name {}", backQuote(name));

    const auto builtin_names = DataTypeFactory::instance().getAllRegisteredNames();
    if (std::ranges::any_of(builtin_names, [&](const auto & builtin) { return equalsCaseInsensitive(builtin, name); }))
        throw Exception(ErrorCodes::BAD_ARGUMENTS, "User-defined type {} conflicts with a built-in type or alias", backQuote(name));
}

struct DefinitionFile
{
    fs::path path;
    ASTPtr query;
};

DefinitionFile findDefinition(const fs::path & directory, const String & name)
{
    /// A lowercase hash distinguishes SQL names even on a case-insensitive filesystem. The hyphen cannot occur in a legacy stem.
    const auto new_path = directory / ("type-" + sipHash128String(name) + ".sql");
    const bool legacy = !fs::exists(new_path);
    const auto path = legacy ? directory / (escapeForFileName(name) + ".sql") : new_path;
    if (legacy && !existsOrFileNameTooLong([&] { return fs::exists(path); }))
        return {new_path, {}};

    ReadBufferFromFile in(path.string());
    String statement;
    readStringUntilEOF(statement, in);
    ParserCreateTypeQuery parser;
    auto ast = parseQuery(parser, statement, "in file " + path.string(), 0, 0, 0);
    const auto & stored_name = ast->as<const ASTCreateTypeQuery &>().name;
    if (stored_name == name)
        return {path, std::move(ast)};

    /// An old file can be reached by another spelling. Leave it with its original owner, including on replace and drop.
    if (legacy && equalsCaseInsensitive(name, stored_name))
    {
        const auto stored_path = directory / (escapeForFileName(stored_name) + ".sql");
        if (fs::exists(stored_path) && fs::equivalent(stored_path, path))
            return {new_path, {}};
    }
    throw Exception(ErrorCodes::CORRUPTED_DATA, "Type name {} does not match its metadata file {}", backQuote(stored_name), path.string());
}

void syncCatalogAfterChange(LocalDirectorySyncGuard & directory_sync, const String & name, const char * current_state)
{
    try
    {
        directory_sync.sync();
    }
    catch (Exception & e)
    {
        e.addMessage("User-defined type {} {} on this server. Its state after a restart is uncertain", backQuote(name), current_state);
        throw;
    }
}

}

UserDefinedTypeCatalog::UserDefinedTypeCatalog(const String & data_path)
    : directory(fs::path(data_path) / "user_defined_types")
{
}

void UserDefinedTypeCatalog::create(const ASTCreateTypeQuery & query, const ContextPtr & context, bool fsync_metadata)
{
    validateName(query.name);
    validateBaseType(query);
    validateUserDefinedTypeChecks(query, context);

    auto normalized = query;
    normalized.or_replace = false;
    const auto create_statement = normalized.formatWithSecretsOneLine();
    std::lock_guard lock(mutex);
    const auto [file_path, existing_query] = findDefinition(directory, query.name);
    if (!query.or_replace && existing_query)
        throw Exception(ErrorCodes::BAD_ARGUMENTS, "User-defined type {} already exists", backQuote(query.name));

    /// The first create may have had `fsync_metadata` disabled. Sync the directory entry on the first durable write.
    std::optional<LocalDirectorySyncGuard> parent_sync;
    if (fsync_metadata && !directory_entry_synced)
        parent_sync.emplace(directory.parent_path().string());
    fs::create_directory(directory);
    if (parent_sync)
    {
        parent_sync->sync();
        directory_entry_synced = true;
    }

    const auto temporary_path = file_path.string() + ".tmp";
    try
    {
        WriteBufferFromFile out(temporary_path, create_statement.size() + 1);
        writeString(create_statement, out);
        writeChar('\n', out);
        if (fsync_metadata)
            out.sync();
        out.close();

        std::optional<LocalDirectorySyncGuard> directory_sync;
        if (fsync_metadata)
            directory_sync.emplace(directory.string());
        if (query.or_replace)
            fs::rename(temporary_path, file_path);
        else
            renameNoReplace(temporary_path, file_path.string());
        if (directory_sync)
            syncCatalogAfterChange(*directory_sync, query.name, "has the new definition");
    }
    catch (...)
    {
        std::error_code ignored;
        fs::remove(temporary_path, ignored);
        throw;
    }
}

void UserDefinedTypeCatalog::drop(const String & name, bool if_exists, bool fsync_metadata)
{
    std::lock_guard lock(mutex);
    const auto [path, query] = findDefinition(directory, name);
    if (!query)
    {
        if (!if_exists)
            throw Exception(ErrorCodes::UNKNOWN_TYPE, "User-defined type {} doesn't exist", backQuote(name));
        /// A previous drop may have unlinked the file and then failed to sync the directory.
        if (!fsync_metadata || !fs::exists(directory))
            return;
    }
    std::optional<LocalDirectorySyncGuard> directory_sync;
    if (fsync_metadata)
        directory_sync.emplace(directory.string());
    if (query && !fs::remove(path) && !if_exists)
        throw Exception(ErrorCodes::UNKNOWN_TYPE, "User-defined type {} doesn't exist", backQuote(name));
    if (directory_sync)
        syncCatalogAfterChange(*directory_sync, name, "is absent");
}

ASTPtr UserDefinedTypeCatalog::tryGetDefinition(const String & name) const
{
    std::lock_guard lock(mutex);
    auto file = findDefinition(directory, name);
    if (file.query)
        validateBaseType(file.query->as<const ASTCreateTypeQuery &>());
    return std::move(file.query);
}

}
