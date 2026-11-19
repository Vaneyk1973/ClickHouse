#include <Interpreters/validateUserDefinedTypeCheck.h>

#include <Columns/ColumnConst.h>
#include <Common/Exception.h>
#include <Common/StringUtils.h>
#include <Common/VectorWithMemoryTracking.h>
#include <Common/checkStackSize.h>
#include <Common/likePatternToRegexp.h>
#include <Core/NamesAndTypes.h>
#include <DataTypes/DataTypeFactory.h>
#include <DataTypes/DataTypeLowCardinality.h>
#include <DataTypes/DataTypeNullable.h>
#include <Functions/FunctionFactory.h>
#include <Functions/Regexps.h>
#include <Functions/UserDefined/UserDefinedExecutableFunctionFactory.h>
#include <Functions/UserDefined/UserDefinedSQLFunctionFactory.h>
#include <Interpreters/ActionsDAG.h>
#include <Interpreters/ExpressionAnalyzer.h>
#include <Interpreters/TreeRewriter.h>
#include <Parsers/ASTCreateTypeQuery.h>
#include <Parsers/ASTExpressionList.h>
#include <Parsers/ASTFunction.h>
#include <Parsers/ASTIdentifier.h>
#include <Parsers/ASTLiteral.h>
#include <Parsers/ASTWithAlias.h>

#include <algorithm>
#include <iterator>
#include <optional>
#include <string_view>

namespace DB
{

namespace ErrorCodes
{
    extern const int BAD_ARGUMENTS;
}

namespace
{

/// shortcut: the function factory has no purity contract. Admit reviewed functions until reliable purity metadata is available.
constexpr std::string_view check_functions[]{
    "plus", "minus", "multiply", "divide", "divideExact", "intDiv", "intDivOrZero", "modulo", "moduloOrZero", "positiveModulo",
    "negate", "abs", "sign", "greatest", "least", "floor", "ceil", "trunc", "round", "roundBankers", "exp", "log",
    "log2", "log10", "pow", "sqrt", "isFinite", "isInfinite", "isNaN",
    "bitAnd", "bitOr", "bitXor", "bitNot", "bitShiftLeft", "bitShiftRight", "bitTest", "bitCount",
    "equals", "notEquals", "less", "lessOrEquals", "greater", "greaterOrEquals", "isDistinctFrom", "isNotDistinctFrom",
    "and", "or", "not", "xor", "if", "multiIf", "caseWithExpression",
    "isNull", "isNotNull", "ifNull", "nullIf", "coalesce", "toNullable",
    "empty", "notEmpty", "length", "lengthUTF8", "isValidUTF8", "lower", "upper", "lowerUTF8", "upperUTF8",
    "trimLeft", "trimRight", "trimBoth", "concat", "substring", "substringUTF8", "replaceOne", "replaceAll",
    "startsWith", "endsWith", "position", "positionCaseInsensitive", "like", "ilike", "notLike", "notILike", "match",
    "array", "arrayElement", "has",
    "toInt8", "toInt16", "toInt32", "toInt64", "toInt128", "toInt256",
    "toUInt8", "toUInt16", "toUInt32", "toUInt64", "toUInt128", "toUInt256",
    "toFloat32", "toFloat64", "toInt64OrNull", "toUInt64OrNull", "toFloat64OrNull", "toString", "toBool", "CAST",
};

constexpr std::string_view check_cast_types[]{
    "Int8", "Int16", "Int32", "Int64", "Int128", "Int256",
    "UInt8", "UInt16", "UInt32", "UInt64", "UInt128", "UInt256",
    "Float32", "Float64", "String", "Bool",
};

ASTPtr bindCheck(const IAST & check, const String & column_name, const ContextPtr & context)
{
    checkStackSize();

    if (const auto * with_alias = dynamic_cast<const ASTWithAlias *>(&check);
        with_alias && (!with_alias->alias.empty() || with_alias->parametrised_alias))
        throw Exception(ErrorCodes::BAD_ARGUMENTS, "Aliases are not allowed in user-defined type CHECK expressions");

    if (const auto * identifier = check.as<ASTIdentifier>())
    {
        if (!identifier->isShort() || identifier->isParam() || !equalsCaseInsensitive(identifier->name(), "VALUE"))
            throw Exception(ErrorCodes::BAD_ARGUMENTS, "Only the unqualified identifier VALUE is allowed in user-defined type CHECK expressions");
        return make_intrusive<ASTIdentifier>(column_name);
    }

    if (check.as<ASTLiteral>())
        return check.clone();

    const auto * function = check.as<ASTFunction>();
    const auto * arguments = function && function->arguments ? function->arguments->as<ASTExpressionList>() : nullptr;
    if (!arguments || function->parameters || function->isWindowFunction() || function->isCompoundName()
        || function->getKind() != ASTFunction::Kind::ORDINARY_FUNCTION || function->getNullsAction() != NullsAction::EMPTY)
        throw Exception(ErrorCodes::BAD_ARGUMENTS, "Only literals, VALUE and supported scalar calls are allowed in user-defined type CHECK expressions");

    const auto & factory = FunctionFactory::instance();
    const auto name = factory.getCanonicalNameIfAny(factory.getAliasToOrName(function->name));
    if (std::ranges::find(check_functions, name) == std::end(check_functions) || !factory.has(function->name))
        throw Exception(ErrorCodes::BAD_ARGUMENTS, "Function {} is not allowed in user-defined type CHECK expressions", function->name);

    const auto is_user_defined = [&](const String & candidate)
    {
        return UserDefinedSQLFunctionFactory::instance().has(candidate)
            || UserDefinedExecutableFunctionFactory::has(candidate, context);
    };
    /// The native analyzer consults the UDF registries first. Check the proposed canonical name as well as the original spelling.
    if (context && (is_user_defined(name) || (name != function->name && is_user_defined(function->name))))
        throw Exception(ErrorCodes::BAD_ARGUMENTS, "Function {} is not allowed in user-defined type CHECK expressions", function->name);

    if (name == "CAST")
    {
        /// Resolve only known scalar targets. Arbitrary type descriptions can instantiate other objects.
        const auto * type = arguments->children.size() == 2 ? arguments->children[1]->as<ASTLiteral>() : nullptr;
        if (!type || type->value.getType() != Field::Types::String
            || std::ranges::find(check_cast_types, type->value.safeGet<String>()) == std::end(check_cast_types))
            throw Exception(ErrorCodes::BAD_ARGUMENTS, "CAST in a user-defined type CHECK requires a supported built-in scalar type");
    }

    /// Use the registered spelling when the predicate is checked by the ordinary constraint interpreter.
    auto bound_function = make_intrusive<ASTFunction>(*function);
    auto bound_arguments = make_intrusive<ASTExpressionList>(arguments->getSeparator());
    bound_arguments->children.reserve(arguments->children.size());
    for (const auto & argument : arguments->children)
        bound_arguments->children.push_back(bindCheck(*argument, column_name, context));
    bound_function->name = name;
    bound_function->arguments = bound_arguments;
    bound_function->children = {std::move(bound_arguments)};
    return bound_function;
}

ActionsDAG analyzeCheck(const ASTPtr & expression, const NamesAndTypesList & columns, const ContextPtr & context)
{
    /// Use the native `CHECK` analysis to resolve types and fold constant arguments.
    auto rewritten = expression->clone();
    auto syntax = TreeRewriter(context).analyze(rewritten, columns, {},
        {} /* storage_snapshot */, false /* allow_aggregations */, true /* allow_self_aliases */, false /* execute_scalar_subqueries */);
    return ExpressionAnalyzer(expression, syntax, context).getActionsDAG(false);
}

struct StringSearchOptions
{
    std::string_view function;
    ASTPtr pattern;
    ASTPtr escape;
};

void collectStringSearchOptions(
    const IAST & check, ASTExpressionList & expressions, VectorWithMemoryTracking<StringSearchOptions> & searches)
{
    checkStackSize();
    if (const auto * function = check.as<ASTFunction>())
    {
        const auto & name = function->name;
        const auto & args = function->arguments->children;
        const bool is_like = name == "like" || name == "ilike" || name == "notLike" || name == "notILike";
        if ((name == "match" && args.size() == 2) || (is_like && (args.size() == 2 || args.size() == 3)))
        {
            auto & options = searches.emplace_back(name, args[1]->clone(), args.size() == 3 ? args[2]->clone() : nullptr);
            expressions.children.push_back(options.pattern);
            if (options.escape)
                expressions.children.push_back(options.escape);
        }
    }

    for (const auto & child : check.children)
        collectStringSearchOptions(*child, expressions, searches);
}

std::optional<String> getConstantString(const ActionsDAG & actions, const IAST & expression)
{
    const auto & constant = actions.findInOutputs(expression.getColumnName()).column;
    if (!constant)
        return {};

    auto value = constant->getField();
    if (value.getType() != Field::Types::String)
        return {};

    return std::move(value).safeGet<String>();
}

void validateStringSearchOptions(const IAST & check, const NamesAndTypesList & columns, const ContextPtr & context)
{
    auto expressions = make_intrusive<ASTExpressionList>();
    VectorWithMemoryTracking<StringSearchOptions> searches;
    collectStringSearchOptions(check, *expressions, searches);
    if (searches.empty())
        return;

    /// Analyze options independently so a conditional or folded parent cannot hide an invalid constant.
    /// Keep the typed `VALUE` input available even if rewriting an option prunes its references.
    expressions->children.push_back(make_intrusive<ASTIdentifier>("VALUE"));
    const auto actions = analyzeCheck(expressions, columns, context);
    for (const auto & options : searches)
    {
        auto pattern = getConstantString(actions, *options.pattern);
        if (options.function == "match")
        {
            if (pattern)
                Regexps::createRegexp<false, false, true, false>(*pattern);
            continue;
        }

        if (options.escape)
        {
            const auto escape = getConstantString(actions, *options.escape);
            if (!escape)
                continue;
            const auto escape_char = parseLikeEscapeCharacter(*escape, options.function);
            if (pattern)
                *pattern = likePatternWithCustomEscapeToLikePattern(*pattern, escape_char);
        }

        /// The native substring path accepts patterns without compiling a regular expression.
        String substring;
        if (!pattern || likePatternIsSubstring(*pattern, substring))
            continue;

        if (options.function == "ilike" || options.function == "notILike")
            Regexps::createRegexp<true, false, true, true>(*pattern);
        else
            Regexps::createRegexp<true, false, true, false>(*pattern);
    }
}

}

void validateUserDefinedTypeChecks(const ASTCreateTypeQuery & query, ContextPtr context)
{
    if (query.checks().empty())
        return;

    ASTs checks;
    checks.reserve(query.checks().size());
    for (const auto & check : query.checks())
        checks.push_back(bindCheck(*check, "VALUE", context));

    const NamesAndTypesList columns{{"VALUE", DataTypeFactory::instance().get(query.base_type)}};
    for (const auto & check : checks)
    {
        validateStringSearchOptions(*check, columns, context);
        auto actions = analyzeCheck(check, columns, context);
        const auto & type = actions.findInOutputs(check->getColumnName()).result_type;
        if (!isUInt8(removeNullable(removeLowCardinality(type))))
            throw Exception(ErrorCodes::BAD_ARGUMENTS, "User-defined type CHECK must return UInt8 or Bool, got {}", type->getName());
    }
}

ASTPtr bindUserDefinedTypeCheck(const IAST & check, const String & column_name)
{
    return bindCheck(check, column_name, {});
}

}
