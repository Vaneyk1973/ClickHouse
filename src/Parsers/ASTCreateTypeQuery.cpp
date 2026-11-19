#include <Parsers/ASTCreateTypeQuery.h>

#include <Common/SipHash.h>
#include <Common/quoteString.h>
#include <IO/Operators.h>

namespace DB
{

void ASTCreateTypeQuery::updateTreeHashImpl(SipHash & hash_state, bool ignore_aliases) const
{
    IAST::updateTreeHashImpl(hash_state, ignore_aliases);
    hash_state.update(or_replace);
    hash_state.update(base_type.size());
    hash_state.update(base_type);
}

void ASTCreateTypeQuery::formatImpl(WriteBuffer & ostr, const FormatSettings &, FormatState &, FormatStateStacked) const
{
    ostr << "CREATE " << (or_replace ? "OR REPLACE " : "") << "TYPE " << backQuoteIfNeed(name)
         << " AS " << backQuoteIfNeed(base_type);
}

}
