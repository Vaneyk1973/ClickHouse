#include <Parsers/ASTDropTypeQuery.h>

#include <Common/SipHash.h>
#include <Common/quoteString.h>
#include <IO/Operators.h>

namespace DB
{

void ASTDropTypeQuery::updateTreeHashImpl(SipHash & hash_state, bool ignore_aliases) const
{
    IAST::updateTreeHashImpl(hash_state, ignore_aliases);
    hash_state.update(if_exists);
}

void ASTDropTypeQuery::formatImpl(WriteBuffer & ostr, const FormatSettings &, FormatState &, FormatStateStacked) const
{
    ostr << "DROP TYPE " << (if_exists ? "IF EXISTS " : "") << backQuoteIfNeed(name);
}

}
