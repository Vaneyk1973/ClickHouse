#pragma once

#include <Interpreters/Context_fwd.h>
#include <Parsers/IAST_fwd.h>

namespace DB
{

class ASTCreateTypeQuery;

void validateUserDefinedTypeChecks(const ASTCreateTypeQuery & query, ContextPtr context);
ASTPtr bindUserDefinedTypeCheck(const IAST & check, const String & column_name);

}
