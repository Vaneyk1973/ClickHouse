#include <Interpreters/UserDefinedTypeCatalog.h>

#include <algorithm>

#include <Common/Exception.h>
#include <Common/StringUtils.h>
#include <Common/atomicRename.h>
#include <Common/escapeForFileName.h>
#include <Common/quoteString.h>
#include <DataTypes/DataTypeFactory.h>
#include <IO/ReadBufferFromFile.h>
#include <IO/ReadHelpers.h>
#include <IO/WriteBufferFromFile.h>
#include <IO/WriteHelpers.h>
#include <Parsers/ASTCreateTypeQuery.h>
#include <Parsers/ParserCreateTypeQuery.h>
#include <Parsers/ParserDataType.h>
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

const String & getBaseTypeName(const ASTCreateTypeQuery & query)
{
    const auto & base_type = query.base_type;
    if (base_type != "Int64" && base_type != "UInt64" && base_type != "String")
        throw Exception(
            ErrorCodes::BAD_ARGUMENTS, "User-defined type {} must have base type Int64, UInt64, or String", backQuote(query.name));
    return base_type;
}

void validateName(const String & name)
{
    /// The name must also work where it is used, after a column name.
    const auto quoted_name = backQuote(name);
    const char * begin = quoted_name.data();
    String parse_error;
    ParserDataType parser;
    if (!tryParseQuery(parser, begin, quoted_name.data() + quoted_name.size(), parse_error, false, "data type name", false, 0, 0, 0, true))
        throw Exception(ErrorCodes::BAD_ARGUMENTS, "Invalid user-defined type name {}", quoted_name);

    const auto builtin_names = DataTypeFactory::instance().getAllRegisteredNames();
    if (std::ranges::any_of(builtin_names, [&](const auto & builtin) { return equalsCaseInsensitive(builtin, name); }))
        throw Exception(ErrorCodes::BAD_ARGUMENTS, "User-defined type {} conflicts with a built-in type or alias", quoted_name);
}

}

UserDefinedTypeCatalog::UserDefinedTypeCatalog(const String & data_path)
    : directory(fs::path(data_path) / "user_defined_types")
{
}

fs::path UserDefinedTypeCatalog::getFilePath(const String & name) const
{
    return directory / (escapeForFileName(name) + ".sql");
}

void UserDefinedTypeCatalog::create(const ASTCreateTypeQuery & query, bool fsync_metadata)
{
    validateName(query.name);
    getBaseTypeName(query);

    auto normalized = query;
    normalized.or_replace = false;
    const auto create_statement = normalized.formatWithSecretsOneLine();
    const auto file_path = getFilePath(query.name);
    const auto temporary_path = file_path.string() + ".tmp";

    std::lock_guard lock(mutex);
    if (!query.or_replace && fs::exists(file_path))
        throw Exception(ErrorCodes::BAD_ARGUMENTS, "User-defined type {} already exists", backQuote(query.name));

    fs::create_directories(directory);
    try
    {
        WriteBufferFromFile out(temporary_path, create_statement.size() + 1);
        writeString(create_statement, out);
        writeChar('\n', out);
        if (fsync_metadata)
            out.sync();
        out.close();

        if (query.or_replace)
            fs::rename(temporary_path, file_path);
        else
            renameNoReplace(temporary_path, file_path.string());
    }
    catch (...)
    {
        std::error_code ignored;
        fs::remove(temporary_path, ignored);
        throw;
    }
}

void UserDefinedTypeCatalog::drop(const String & name, bool if_exists)
{
    std::lock_guard lock(mutex);
    if (!fs::remove(getFilePath(name)) && !if_exists)
        throw Exception(ErrorCodes::UNKNOWN_TYPE, "User-defined type {} doesn't exist", backQuote(name));
}

std::optional<String> UserDefinedTypeCatalog::tryGetBaseType(const String & name) const
{
    const auto file_path = getFilePath(name);
    String create_statement;
    {
        std::lock_guard lock(mutex);
        if (!fs::exists(file_path))
            return std::nullopt;

        ReadBufferFromFile in(file_path.string());
        readStringUntilEOF(create_statement, in);
    }

    ParserCreateTypeQuery parser;
    auto ast = parseQuery(parser, create_statement, "in file " + file_path.string(), 0, 0, 0);
    const auto & query = ast->as<const ASTCreateTypeQuery &>();
    if (query.name != name)
        throw Exception(
            ErrorCodes::CORRUPTED_DATA, "Type name {} does not match its metadata file {}", backQuote(query.name), file_path.string());
    return getBaseTypeName(query);
}

}
