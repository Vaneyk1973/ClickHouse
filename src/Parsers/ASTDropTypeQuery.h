#pragma once

#include <Parsers/IAST.h>

namespace DB
{

class ASTDropTypeQuery : public IAST
{
public:
    String name;
    bool if_exists = false;

    String getID(char delim) const override { return "DropTypeQuery" + (delim + name); }
    ASTPtr clone() const override { return make_intrusive<ASTDropTypeQuery>(*this); }
    QueryKind getQueryKind() const override { return QueryKind::Drop; }
    void updateTreeHashImpl(SipHash & hash_state, bool ignore_aliases) const override;

protected:
    void formatImpl(WriteBuffer & ostr, const FormatSettings & settings, FormatState & state, FormatStateStacked frame) const override;
};

}
