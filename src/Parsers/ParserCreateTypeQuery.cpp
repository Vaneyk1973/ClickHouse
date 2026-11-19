#include <Parsers/ParserCreateTypeQuery.h>

#include <Parsers/ASTCreateTypeQuery.h>
#include <Parsers/ASTIdentifier_fwd.h>
#include <Parsers/CommonParsers.h>
#include <Parsers/ExpressionElementParsers.h>
#include <Parsers/ExpressionListParsers.h>

namespace DB
{

bool ParserCreateTypeQuery::parseImpl(Pos & pos, ASTPtr & node, Expected & expected)
{
    if (!ParserKeyword(Keyword::CREATE).ignore(pos, expected))
        return false;

    bool or_replace = ParserKeyword(Keyword::OR_REPLACE).ignore(pos, expected);

    ParserIdentifier identifier_p;
    ASTPtr name;
    ASTPtr base_name;
    if (!ParserKeyword(Keyword::TYPE).ignore(pos, expected)
        || !identifier_p.parse(pos, name, expected)
        || !ParserKeyword(Keyword::AS).ignore(pos, expected)
        || !identifier_p.parse(pos, base_name, expected))
        return false;

    auto query = make_intrusive<ASTCreateTypeQuery>();
    query->name = getIdentifierName(name);
    query->base_type = getIdentifierName(base_name);
    query->or_replace = or_replace;

    ParserKeyword check_keyword(Keyword::CHECK);
    ParserExpression expression_parser;
    while (check_keyword.ignore(pos, expected))
    {
        ASTPtr check;
        if (!expression_parser.parse(pos, check, expected))
            return false;
        query->children.push_back(std::move(check));
    }

    node = std::move(query);
    return true;
}

}
