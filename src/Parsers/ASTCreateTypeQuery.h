#pragma once

#include <Parsers/IAST.h>

namespace DB
{

class ASTCreateTypeQuery : public IAST
{
public:
    String name;
    String base_type;
    bool or_replace = false;

    String getID(char delim) const override { return "CreateTypeQuery" + (delim + name); }
    ASTPtr clone() const override { return make_intrusive<ASTCreateTypeQuery>(*this); }
    QueryKind getQueryKind() const override { return QueryKind::Create; }
    void updateTreeHashImpl(SipHash & hash_state, bool ignore_aliases) const override;

protected:
    void formatImpl(WriteBuffer & ostr, const FormatSettings & settings, FormatState & state, FormatStateStacked frame) const override;
};

}
