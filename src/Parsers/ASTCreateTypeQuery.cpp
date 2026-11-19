#include <Parsers/ASTCreateTypeQuery.h>

#include <Common/SipHash.h>
#include <Common/quoteString.h>
#include <IO/Operators.h>

namespace DB
{

ASTPtr ASTCreateTypeQuery::clone() const
{
    auto clone = make_intrusive<ASTCreateTypeQuery>(*this);
    clone->cloneChildren();
    return clone;
}

void ASTCreateTypeQuery::updateTreeHashImpl(SipHash & hash_state, bool ignore_aliases) const
{
    IAST::updateTreeHashImpl(hash_state, ignore_aliases);
    hash_state.update(or_replace);
    hash_state.update(base_type.size());
    hash_state.update(base_type);
}

void ASTCreateTypeQuery::formatImpl(WriteBuffer & ostr, const FormatSettings & settings, FormatState & state, FormatStateStacked frame) const
{
    ostr << "CREATE " << (or_replace ? "OR REPLACE " : "") << "TYPE " << backQuoteIfNeed(name)
         << " AS " << backQuoteIfNeed(base_type);

    for (const auto & check : checks())
    {
        ostr << " CHECK ";
        auto nested_frame = frame;
        if (!check->tryGetAlias().empty())
            nested_frame.need_parens = true;
        check->format(ostr, settings, state, nested_frame);
    }
}

}
