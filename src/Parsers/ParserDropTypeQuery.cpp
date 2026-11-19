#include <Parsers/ParserDropTypeQuery.h>

#include <Parsers/ASTDropTypeQuery.h>
#include <Parsers/ASTIdentifier_fwd.h>
#include <Parsers/CommonParsers.h>
#include <Parsers/ExpressionElementParsers.h>

namespace DB
{

bool ParserDropTypeQuery::parseImpl(Pos & pos, ASTPtr & node, Expected & expected)
{
    if (!ParserKeyword(Keyword::DROP).ignore(pos, expected) || !ParserKeyword(Keyword::TYPE).ignore(pos, expected))
        return false;

    bool if_exists = ParserKeyword(Keyword::IF_EXISTS).ignore(pos, expected);

    ParserIdentifier identifier_p;
    ASTPtr name;
    if (!identifier_p.parse(pos, name, expected))
        return false;

    auto query = make_intrusive<ASTDropTypeQuery>();
    query->name = getIdentifierName(name);
    query->if_exists = if_exists;
    node = std::move(query);
    return true;
}

}
