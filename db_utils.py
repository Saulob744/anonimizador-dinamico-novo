import logging
from collections import defaultdict, deque
from typing import Any

import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# CACHE DE TABELAS (performance)
# Chave ajustada para "schema.table_name" para evitar colisões
_TABLE_CACHE: dict[str, sa.Table] = {}


# =========================================
# CONNECTION
# =========================================
def connect(url: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return engine


# =========================================
# DATABASE
# =========================================
def create_database_if_not_exists(server_url: str, db_name: str) -> None:
    engine = create_engine(server_url, isolation_level="AUTOCOMMIT")

    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": db_name},
        ).scalar()

        if not exists:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
            logger.info(f"Banco '{db_name}' criado.")
        else:
            logger.info(f"Banco '{db_name}' já existe.")

    engine.dispose()


# =========================================
# METADATA & SCHEMAS
# =========================================
def get_user_schemas(engine: Engine) -> list[str]:
    """Retorna todos os schemas, ignorando os de sistema do PostgreSQL."""
    insp = inspect(engine)
    system_schemas = {'information_schema', 'pg_catalog', 'pg_toast'}
    # Filtra schemas do sistema e schemas temporários
    return [s for s in insp.get_schema_names() if s not in system_schemas and not s.startswith('pg_temp')]


def get_tables(engine: Engine, schema: str) -> list[str]:
    return inspect(engine).get_table_names(schema=schema)


def get_table_info(engine: Engine, table_name: str, schema: str) -> dict:
    insp = inspect(engine)

    return {
        "columns": insp.get_columns(table_name, schema=schema),
        "primary_keys": insp.get_pk_constraint(table_name, schema=schema).get("constrained_columns", []),
        "foreign_keys": insp.get_foreign_keys(table_name, schema=schema),
    }


# =========================================
# DEPENDENCY GRAPH
# =========================================
def build_dependency_graph(engine: Engine, tables: list[str], schema: str) -> list[str]:
    insp = inspect(engine)

    deps = defaultdict(set)

    for table in tables:
        for fk in insp.get_foreign_keys(table, schema=schema):
            ref = fk.get("referred_table")
            if ref and ref != table and ref in tables:
                deps[table].add(ref)

    # cálculo correto de in-degree
    in_degree = {t: 0 for t in tables}
    for table, dependencies in deps.items():
        for dep in dependencies:
            in_degree[table] += 1

    queue = deque([t for t in tables if in_degree[t] == 0])
    ordered = []

    while queue:
        node = queue.popleft()
        ordered.append(node)

        for table in tables:
            if node in deps.get(table, set()):
                in_degree[table] -= 1
                if in_degree[table] == 0:
                    queue.append(table)

    # ciclos
    remaining = [t for t in tables if t not in ordered]
    if remaining:
        logger.warning(f"Ciclo detectado no schema '{schema}': {remaining}")
        ordered.extend(remaining)

    return ordered


# =========================================
# SCHEMA COPY
# =========================================
def copy_schema(source_engine: Engine, dest_engine: Engine, schema: str) -> None:
    logger.info(f"Copiando estrutura do banco para o schema '{schema}'...")

    # Garante que o schema existe no destino
    with dest_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    meta = sa.MetaData()

    # carrega estrutura do banco origem especificando o schema
    meta.reflect(bind=source_engine, schema=schema)

    # REMOVE DEFAULT nextval (para não quebrar a sequência no destino se copiar id manualmente)
    for table in meta.tables.values():
        for col in table.columns:
            if col.server_default is not None:
                if "nextval" in str(col.server_default).lower():
                    col.server_default = None

    # cria no destino
    with dest_engine.begin() as conn:
        for table in meta.sorted_tables:
            try:
                table.schema = schema
                table.create(bind=conn, checkfirst=True)
                logger.info(f"✔ Tabela '{schema}.{table.name}' criada")
            except Exception as e:
                logger.error(f"Erro criando '{schema}.{table.name}': {e}")


# =========================================
# DATA
# =========================================
def get_row_count(engine: Engine, table_name: str, schema: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(f'SELECT COUNT(*) FROM "{schema}"."{table_name}"')
        ).scalar() or 0


def fetch_rows_chunked(engine: Engine, table_name: str, schema: str, chunk_size: int = 1000):
    with engine.connect() as conn:
        offset = 0

        while True:
            rows = conn.execute(
                text(f'SELECT * FROM "{schema}"."{table_name}" LIMIT :limit OFFSET :offset'),
                {"limit": chunk_size, "offset": offset},
            ).mappings().fetchall()

            if not rows:
                break

            yield rows
            offset += chunk_size


# =========================================
# INSERT (COM CACHE)
# =========================================
def insert_rows(dest_engine: Engine, table_name: str, schema: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return

    cache_key = f"{schema}.{table_name}"

    if cache_key not in _TABLE_CACHE:
        meta = sa.MetaData()
        _TABLE_CACHE[cache_key] = sa.Table(
            table_name,
            meta,
            autoload_with=dest_engine,
            schema=schema,
        )

    table = _TABLE_CACHE[cache_key]

    with dest_engine.begin() as conn:
        conn.execute(table.insert(), rows)


# =========================================
# FK CONTROL
# =========================================
def disable_fk_constraints(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("SET session_replication_role = 'replica'"))


def enable_fk_constraints(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("SET session_replication_role = 'origin'"))