import logging
import os
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Generator

import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text, Table, MetaData
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

# ==================================================
# LOGS
# ==================================================
logger = logging.getLogger(__name__)

_TABLE_CACHE: Dict[str, Table] = {}
# ==================================================
# ENCODING / SANITIZAÇÃO
# ==================================================
def safe_decode(value):
    """
    Corrige problemas de encoding sem derrubar pipeline.
    """
    if value is None:
        return None
    # bytes -> utf8 -> latin1 -> replace
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return value.decode("latin1")
            except Exception:
                return value.decode("utf-8", errors="replace")

    # strings problemáticas
    if isinstance(value, str):
        try:
            value.encode("utf-8")
            return value
        except UnicodeEncodeError:
            return value.encode(
                "utf-8",
                errors="replace"
            ).decode("utf-8")

    return value

# ==================================================
# PERFORMANCE E CPU
# ==================================================
def get_cpu_info() -> int:
    return os.cpu_count() or 1

def calculate_safe_workers(
    requested_cores: Optional[int] = None
) -> int:

    total = get_cpu_info()
    if requested_cores:
        return min(requested_cores, total)
    return max(1, int(total * 0.75))

# ==================================================
# CONEXÃO E BANCO
# ==================================================
def connect(url: str) -> Engine:
    parsed = make_url(url)
    if "odbc_connect" in url:
        logger.warning(
            "⚠️ ODBC detectado → usando conexão direta"
        )
        return create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            future=True
        )

    if not (db_name := parsed.database):
        raise ValueError(
            "URL fornecida não contém um banco de dados."
        )

    backend = parsed.get_backend_name()

    admin_db = (
        "postgres"
        if backend == "postgresql"
        else "master"
        if backend == "mssql"
        else None
    )
    # ==================================================
    # CRIAÇÃO DE DATABASE
    # =================================================
    if admin_db:
        server_url = parsed.set(database=admin_db)
        try:
            engine_server = create_engine(
                server_url,
                isolation_level="AUTOCOMMIT",
                future=True
            )
            create_queries = {
                "postgresql":
                    f'CREATE DATABASE "{db_name}"',
                "mysql":
                    f"CREATE DATABASE IF NOT EXISTS `{db_name}`",
                "mssql":
                    f"""
                    IF NOT EXISTS (
                        SELECT name
                        FROM sys.databases
                        WHERE name = '{db_name}'
                    )
                    EXEC('CREATE DATABASE [{db_name}]')
                    """
            }
            with engine_server.connect() as conn:

                if backend == "postgresql":
                    exists = conn.execute(
                        text(
                            """
                            SELECT 1
                            FROM pg_database
                            WHERE datname = :name
                            """
                        ),
                        {"name": db_name}
                    ).scalar()

                    if not exists:
                        conn.execute(
                            text(create_queries[backend])
                        )

                        logger.info(
                            f"✅ Banco '{db_name}' criado."
                        )
                elif backend in create_queries:
                    conn.execute(
                        text(create_queries[backend])
                    )
        except sa.exc.OperationalError:
            logger.warning(
                f"⚠️ Sem acesso ao banco admin "
                f"para criar/verificar '{db_name}'."
            )
        except sa.exc.ProgrammingError:
            logger.warning(
                f"⚠️ Sem permissão CREATE DATABASE "
                f"para '{db_name}'."
            )
        except Exception as e:
            logger.error(
                f"⚠️ Erro ao criar/verificar DB: {e}"
            )
        finally:

            if "engine_server" in locals():
                engine_server.dispose()
    # ==================================================
    # ENGINE PRINCIPAL
    # ==================================================
    connect_args = {}
    if backend == "postgresql":
        connect_args["client_encoding"] = "utf8"
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True,
        connect_args=connect_args
    )

# ==================================================
# INSPEÇÃO
# ==================================================
def get_db_type(engine: Engine) -> str:
    return engine.dialect.name

def format_table_name(
    engine: Engine,
    schema: Optional[str],
    table: str
) -> str:
    db_type = get_db_type(engine)
    if not schema:
        return (
            f"`{table}`"
            if db_type == "mysql"
            else f'"{table}"'
        )

    mapping = {
        "mysql": f"`{schema}`.`{table}`",
        "sqlite": f'"{table}"'
    }

    return mapping.get(
        db_type,
        f'"{schema}"."{table}"'
    )

def table_exists(
    engine: Engine,
    schema: Optional[str],
    table: str
) -> bool:
    return inspect(engine).has_table(
        table,
        schema=schema
    )

def get_tables(
    engine: Engine,
    schema: Optional[str] = None
) -> List[str]:
    return sorted(
        inspect(engine).get_table_names(schema=schema)
    )

def get_table_info(
    engine: Engine,
    table: str,
    schema: Optional[str] = None
) -> Dict[str, Any]:
    insp = inspect(engine)
    return {
        "columns":
            insp.get_columns(table, schema=schema),

        "primary_keys":
            insp.get_pk_constraint(
                table,
                schema=schema
            ).get("constrained_columns", []),

        "foreign_keys":
            insp.get_foreign_keys(
                table,
                schema=schema
            )
    }

def get_table_count(
    engine: Engine,
    table: str,
    schema: Optional[str] = None
) -> int:
    try:

        tbl = Table(
            table,
            MetaData(),
            autoload_with=engine,
            schema=schema
        )
        with engine.connect() as conn:
            return conn.execute(
                sa.select(sa.func.count())
                .select_from(tbl)
            ).scalar() or 0
    except Exception as e:
        logger.warning(
            f"⚠️ COUNT fallback "
            f"{schema or 'default'}.{table}: {e}"
        )
        return 1000

def get_user_schemas(engine: Engine) -> List[str]:
    db_type = get_db_type(engine)
    insp = inspect(engine)
    ignored = (
        {"information_schema", "pg_catalog", "pg_toast"}
        if db_type == "postgresql"
        else {"information_schema"}
    )
    return sorted([
        s for s in insp.get_schema_names()
        if s not in ignored and not s.startswith("pg_")
    ])

# ==================================================
# SCHEMA
# ==================================================
def copy_schema(
    src_engine: Engine,
    dst_engine: Engine,
    schema: Optional[str] = None
):
    if (
        schema
        and get_db_type(dst_engine) == "postgresql"
    ):
        try:
            with dst_engine.begin() as conn:
                conn.execute(
                    text(
                        f'CREATE SCHEMA IF NOT EXISTS "{schema}"'
                    )
                )
        except Exception as e:

            logger.warning(
                f"⚠️ Sem permissão para criar "
                f"schema '{schema}': {e}"
            )
    meta = MetaData()
    with src_engine.connect() as conn:
        meta.reflect(
            bind=conn,
            schema=schema,
            resolve_fks=False
        )
    with dst_engine.begin() as conn:
        for table in meta.sorted_tables:
            try:
                table.schema = schema
                for col in table.columns:
                    col.server_default = None
                table.create(
                    bind=conn,
                    checkfirst=True
                )
            except Exception as e:
                logger.warning(
                    f"⚠️ Falha criar tabela "
                    f"{schema or 'default'}."
                    f"{table.name}: {e}"
                )
# ==================================================
# STREAMING
# ==================================================

def fetch_rows_streaming(
    engine: Engine,
    table: str,
    schema: Optional[str] = None,
    chunk_size: int = 1000,
    order_by: Optional[str] = None
) -> Generator:

    meta = MetaData()

    tbl = Table(
        table,
        meta,
        autoload_with=engine,
        schema=schema
    )

    stmt = sa.select(tbl)

    if order_by:

        stmt = stmt.order_by(
            sa.column(order_by)
        )

    else:

        pks = [c for c in tbl.primary_key.columns]

        if pks:
            stmt = stmt.order_by(*pks)

    with engine.connect() as conn:

        result = conn.execution_options(
            stream_results=True
        ).execute(stmt)

        while rows := result.mappings().fetchmany(chunk_size):

            safe_rows = []

            for row in rows:

                fixed = {}

                for k, v in row.items():
                    fixed[k] = safe_decode(v)

                safe_rows.append(fixed)

            yield safe_rows


# ==================================================
# SANITIZAÇÃO
# ==================================================

def _sanitize_row_data(
    table: Table,
    rows: List[Dict]
) -> List[Dict]:

    safe_rows = []

    for row in rows:

        new_row = {}

        for col in table.columns:

            val = row.get(col.name)

            # Corrige encoding
            val = safe_decode(val)

            # Limita tamanho
            if (
                isinstance(val, str)
                and getattr(col.type, "length", None)
            ):
                val = val[:col.type.length]

            new_row[col.name] = val

        safe_rows.append(new_row)

    return safe_rows


# ==================================================
# INSERT
# ==================================================

def insert_rows(
    engine: Engine,
    table_name: str,
    schema: Optional[str] = None,
    rows: List[Dict] = None,
    max_retries: int = 3,
    ignore_conflicts: bool = False
):

    if not rows:
        return

    key = (
        f"{engine.url}_"
        f"{schema or 'default'}_"
        f"{table_name}"
    )

    if key not in _TABLE_CACHE:

        _TABLE_CACHE[key] = Table(
            table_name,
            MetaData(),
            autoload_with=engine,
            schema=schema
        )

    table = _TABLE_CACHE[key]

    db_type = engine.dialect.name

    for attempt in range(max_retries):

        try:

            safe_rows = _sanitize_row_data(
                table,
                rows
            )

            with engine.begin() as conn:

                if ignore_conflicts:

                    if db_type == "postgresql":

                        stmt = (
                            pg_insert(table)
                            .values(safe_rows)
                            .on_conflict_do_nothing()
                        )

                    elif db_type == "mysql":

                        stmt = (
                            mysql_insert(table)
                            .values(safe_rows)
                            .on_duplicate_key_update(
                                **{
                                    c.name: c
                                    for c in mysql_insert(
                                        table
                                    ).inserted
                                }
                            )
                        )

                    elif db_type == "sqlite":

                        stmt = (
                            sqlite_insert(table)
                            .values(safe_rows)
                            .on_conflict_do_nothing()
                        )

                    else:

                        stmt = table.insert().values(
                            safe_rows
                        )

                else:

                    stmt = table.insert().values(
                        safe_rows
                    )

                conn.execute(stmt)

            return

        except sa.exc.IntegrityError as e:

            if ignore_conflicts:

                logger.warning(
                    f"⚠️ Conflito ignorado "
                    f"em {table_name}"
                )

                return

            logger.warning(
                f"⚠️ Integridade tentativa "
                f"{attempt + 1} "
                f"em {table_name}: {e}"
            )

            time.sleep(1)

        except Exception as e:

            logger.warning(
                f"⚠️ Erro tentativa "
                f"{attempt + 1} "
                f"em {table_name}: {e}"
            )

            if rows:
                logger.warning(
                    f"🔎 Linha exemplo: "
                    f"{str(rows[0])[:500]}"
                )

            time.sleep(1)

    logger.error(
        f"❌ Falha definitiva ao inserir "
        f"em {table_name}"
    )


# ==================================================
# TRUNCATE
# ==================================================

def truncate_table(
    engine: Engine,
    table_name: str,
    schema: Optional[str] = None
):

    if not table_exists(
        engine,
        schema,
        table_name
    ):
        return

    table_ref = format_table_name(
        engine,
        schema,
        table_name
    )

    with engine.begin() as conn:

        try:

            if get_db_type(engine) == "postgresql":

                conn.execute(
                    text(
                        f"TRUNCATE TABLE "
                        f"{table_ref} CASCADE"
                    )
                )

            else:

                conn.execute(
                    text(
                        f"TRUNCATE TABLE {table_ref}"
                    )
                )

        except Exception as e:

            logger.warning(
                f"⚠️ Falha TRUNCATE "
                f"{table_ref}. "
                f"Fallback DELETE: {e}"
            )

            try:

                conn.execute(
                    text(
                        f"DELETE FROM {table_ref}"
                    )
                )

            except Exception as delete_e:

                logger.error(
                    f"❌ Falha DELETE "
                    f"{table_ref}: {delete_e}"
                )


# ==================================================
# DEPENDÊNCIAS
# ==================================================

def build_dependency_graph(
    engine: Engine,
    tables: List[str],
    schema: Optional[str] = None
) -> List[str]:

    insp = inspect(engine)

    deps = defaultdict(set)

    in_degree = {
        t: 0 for t in tables
    }

    for table in tables:

        for fk in insp.get_foreign_keys(
            table,
            schema=schema
        ):

            ref = fk.get("referred_table")

            if ref in tables and ref != table:

                deps[ref].add(table)

                in_degree[table] += 1

    queue = deque([
        t for t in tables
        if in_degree[t] == 0
    ])

    ordered = []

    while queue:

        t = queue.popleft()

        ordered.append(t)

        for d in deps[t]:

            in_degree[d] -= 1

            if in_degree[d] == 0:
                queue.append(d)

    return ordered + [
        t for t in tables
        if t not in ordered
    ]


# ==================================================
# REPLICATION MODE
# ==================================================

def set_replication_mode(
    engine: Engine,
    mode: str = "replica"
):
    """
    Tenta ativar modo replica no PostgreSQL.
    Se o usuário não possuir SUPERUSER,
    apenas ignora sem quebrar pipeline.
    """

    if get_db_type(engine) != "postgresql":
        return

    try:

        with engine.begin() as conn:

            conn.execute(
                text(
                    """
                    SET session_replication_role
                    = :mode
                    """
                ),
                {"mode": mode}
            )

            logger.info(
                f"✅ session_replication_role={mode}"
            )

    except sa.exc.DBAPIError as e:

        error_msg = str(e).lower()

        # Sem permissão -> ignora
        if (
            "permission denied" in error_msg
            or "insufficientprivilege" in error_msg
            or "session_replication_role" in error_msg
        ):

            logger.warning(
                "⚠️ Sem privilégio SUPERUSER. "
                "Continuando sem replication_role."
            )

            return

        # outros erros reais
        raise