#include <Access/Common/AccessFlags.h>
#include <Core/Settings.h>
#include <Interpreters/Context.h>
#include <Interpreters/InterpreterFactory.h>
#include <Interpreters/UserDefinedTypeCatalog.h>
#include <Parsers/ASTCreateTypeQuery.h>
#include <Parsers/ASTDropTypeQuery.h>

namespace DB
{

namespace Setting
{
    extern const SettingsBool fsync_metadata;
}

namespace
{

class InterpreterTypeQuery : public IInterpreter, WithContext
{
public:
    InterpreterTypeQuery(const ASTPtr & query_, ContextPtr context_)
        : WithContext(context_)
        , query(query_)
    {
    }

    BlockIO execute() override
    {
        const auto context_ptr = getContext();
        if (const auto * create = query->as<ASTCreateTypeQuery>())
        {
            context_ptr->checkAccess(AccessType::CREATE_TYPE);
            if (create->or_replace)
                context_ptr->checkAccess(AccessType::DROP_TYPE);

            context_ptr->getUserDefinedTypeCatalog().create(*create, context_ptr, context_ptr->getSettingsRef()[Setting::fsync_metadata]);
        }
        else
        {
            const auto & drop = query->as<ASTDropTypeQuery &>();
            context_ptr->checkAccess(AccessType::DROP_TYPE);
            context_ptr->getUserDefinedTypeCatalog().drop(drop.name, drop.if_exists, context_ptr->getSettingsRef()[Setting::fsync_metadata]);
        }

        return {};
    }

private:
    ASTPtr query;
};

}

void registerInterpreterTypeQuery(InterpreterFactory & factory);
void registerInterpreterTypeQuery(InterpreterFactory & factory)
{
    factory.registerInterpreter("InterpreterTypeQuery", [](const InterpreterFactory::Arguments & args)
    {
        return std::make_unique<InterpreterTypeQuery>(args.query, args.context);
    });
}

}
