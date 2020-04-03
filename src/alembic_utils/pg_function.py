# pylint: disable=unused-argument,invalid-name,line-too-long
from __future__ import annotations

from hashlib import md5
from pathlib import Path

from typing import Optional, List

from alembic.autogenerate import comparators, renderers
from alembic.operations import Operations
from alembic_utils.replaceable_object import ReplaceableObject
from alembic_utils.reversible_op import ReversibleOp
from sqlalchemy import text as sql_text
from flupy import walk_files
from parse import parse


class PGFunction(ReplaceableObject):
    """ A PostgreSQL Function that can be versioned and replaced """

    @classmethod
    def from_path(cls, path: Path) -> Optional[PGFunction]:
        """ Create an instance of PGFunction from a .sql file path """
        with path.open() as sql_file:
            sql = sql_file.read()
        return cls.from_sql(sql)

    @classmethod
    def from_sql(cls, sql: str) -> Optional[PGFunction]:
        """ Create an instance of PGFunction from a blob of sql """
        template = "create{:s}or{:s}replace{:s}function{:s}{schema}.{signature}{:s}returns{:s}{definition}"

        result = parse(template, sql.strip(), case_sensitive=False)
        if result is not None:
            schema = result["schema"]
            signature = result["signature"]
            definition = "returns " + result["definition"]
            return cls(schema=schema, signature=signature, definition=definition)
        return None

    @staticmethod
    def _normalize_whitespace(text, base_whitespace: str = " ") -> str:
        """ Convert all whitespace to *base_whitespace* """
        return base_whitespace.join(text.split())

    def is_equal_definition(self, other: PGFunction) -> bool:
        """ Is the definition within self and other the same """
        def1 = self._normalize_whitespace(self.definition)
        def2 = self._normalize_whitespace(other.definition)
        return def1 == def2

    def is_equal_signature(self, other: PGFunction) -> bool:
        """ Is the signature of self and other the same """
        def1 = self._normalize_whitespace(self.signature)
        def2 = self._normalize_whitespace(other.signature)
        return def1 == def2 and self.schema == other.schema

    def to_variable_name(self):
        """ A unique and deterministic variable name based on PGFunction's contents """
        schema_name = self.schema.lower().strip()
        function_name = self.signature.split("(")[0].strip().lower()
        signature = self._normalize_whitespace(self.signature.lower())
        signature_md5 = md5(signature.encode()).hexdigest()
        function_variable_name = f"{schema_name}_{function_name}_{signature_md5}"
        return function_variable_name

    def get_required_migration_op(self, connection) -> Optional[ReversibleOp]:
        """ Return the MigrationOp to execute """
        # Get current version of function before migration
        db_live: PGFunction = self.get_db_definition(connection)

        # If it doesn't exist in the database, it should be in the upgrade
        if db_live is None:
            return CreateFunctionOp

        # Create a trash schema, and generate the current function definition within it.
        cls = self.__class__
        adjusted_target = cls("alembic_autogen", self.signature, self.definition)
        connection.execute(f"create schema if not exists {adjusted_target.schema};")
        connection.execute(
            f"CREATE OR REPLACE FUNCTION {adjusted_target.schema}.{adjusted_target.signature} {adjusted_target.definition}"
        )
        temporary_db_version = adjusted_target.get_db_definition(connection)

        # Compare the current
        if db_live.is_equal_definition(temporary_db_version):
            needs_update = False
        else:
            needs_update = True
        assert needs_update is not None

        connection.execute("drop schema alembic_autogen cascade;")

        if needs_update:
            return ReplaceFunctionOp

        return None

    @classmethod
    def get_db_functions(cls, connection, schema="%") -> List[PGFunction]:
        """Get a list of all functions defined in the db"""
        sql = sql_text(
            f"""
        select
            n.nspname as function_schema,
            p.proname as function_name,
            pg_get_function_arguments(p.oid) as function_arguments,
            case
                when l.lanname = 'internal' then p.prosrc
                else pg_get_functiondef(p.oid)
            end as create_statement,
            t.typname as return_type,
            l.lanname as function_language
        from
            pg_proc p
            left join pg_namespace n on p.pronamespace = n.oid
            left join pg_language l on p.prolang = l.oid
            left join pg_type t on t.oid = p.prorettype
        where
            n.nspname like '{schema}';
        """
        )
        rows = connection.execute(sql).fetchall()
        db_functions = [PGFunction.from_sql(x[3]) for x in rows]

        for func in db_functions:
            assert func is not None

        return db_functions

    def get_db_definition(self, connection) -> PGFunction:
        """Geta PGFunction for the existing database version"""

        db_functions = self.get_db_functions(connection, schema=self.schema)
        matches = [x for x in db_functions if self.is_equal_signature(x)]

        if len(matches) == 0:
            return None

        db_match = matches[0]
        return db_match

    def to_sql_statement_create(self) -> str:
        """ Generates a SQL "create function" statement for PGFunction """
        return f"CREATE FUNCTION {self.schema}.{self.signature} {self.definition}"

    def to_sql_statement_drop(self) -> str:
        """ Generates a SQL "drop function" statement for PGFunction """
        return f"DROP FUNCTION {self.schema}.{self.signature}"

    def to_sql_statement_create_or_replace(self) -> str:
        """ Generates a SQL "create or replace function" statement for PGFunction """
        return f"CREATE OR REPLACE FUNCTION {self.schema}.{self.signature} {self.definition}"


##############
# Operations #
##############


@Operations.register_operation("create_function", "invoke_for_target")
class CreateFunctionOp(ReversibleOp):
    def reverse(self):
        return DropFunctionOp(self.target)


@Operations.register_operation("drop_function", "invoke_for_target")
class DropFunctionOp(ReversibleOp):
    def reverse(self):
        return CreateFunctionOp(self.target)


@Operations.register_operation("replace_function", "invoke_for_target")
class ReplaceFunctionOp(ReversibleOp):
    def reverse(self):
        return RevertFunctionOp(self.target)


class RevertFunctionOp(ReversibleOp):
    # Revert is never in an upgrade, so no need to implement reverse
    pass


###################
# Implementations #
###################


@Operations.implementation_for(CreateFunctionOp)
def create_function(operations, operation):
    target = operation.target
    operations.execute(target.to_sql_statement_create())


@Operations.implementation_for(DropFunctionOp)
def drop_function(operations, operation):
    target = operation.target
    operations.execute(target.to_sql_statement_drop())


@Operations.implementation_for(ReplaceFunctionOp)
@Operations.implementation_for(RevertFunctionOp)
def replace_or_revert_function(operations, operation):
    target = operation.target
    operations.execute(target.to_sql_statement_create_or_replace())


##########
# Render #
##########


@renderers.dispatch_for(CreateFunctionOp)
def render_create_function(autogen_context, op):
    var_name = op.target.to_variable_name()
    return f"""{var_name} = PGFunction(
        schema="{op.target.schema}",
        signature="{op.target.signature}",
        definition=\"\"\"{op.target.definition}\"\"\"
    )

op.create_function({var_name})
    """


@renderers.dispatch_for(DropFunctionOp)
def render_drop_function(autogen_context, op):
    var_name = op.target.to_variable_name()
    return f"""{var_name} = PGFunction(
        schema="{op.target.schema}",
        signature="{op.target.signature}",
        definition="# Not Used"
    )

op.drop_function({var_name})
    """


@renderers.dispatch_for(ReplaceFunctionOp)
def render_replace_function(autogen_context, op):
    var_name = op.target.to_variable_name()
    return f"""{var_name} = PGFunction(
        schema="{op.target.schema}",
        signature="{op.target.signature}",
        definition=\"\"\"{op.target.definition}\"\"\"
    )
op.replace_function({var_name})
    """


@renderers.dispatch_for(RevertFunctionOp)
def render_revert_function(autogen_context, op):
    """ Collect the function definition currently live in the database and use its definition
    as the downgrade revert target """
    context = autogen_context
    engine = context.connection.engine

    with engine.connect() as connection:
        db_target = op.target.get_db_definition(connection)

    var_name = op.target.to_variable_name()
    return f"""{var_name} = PGFunction(
        schema="{db_target.schema}",
        signature="{db_target.signature}",
        definition=\"\"\"{db_target.definition}\"\"\"
    )
op.replace_function({var_name})
    """


##################
# Event Listener #
##################
def register_functions(pg_functions: List[PGFunction]) -> None:
    @comparators.dispatch_for("schema")
    def compare_registered_pg_functions(autogen_context, upgrade_ops, schemas):
        context = autogen_context
        engine = context.connection.engine

        with engine.connect() as connection:
            for function in pg_functions:
                maybe_op = function.get_required_migration_op(connection)
                if maybe_op is not None:
                    upgrade_ops.ops.append(maybe_op(function))
