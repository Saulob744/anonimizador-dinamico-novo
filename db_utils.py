import logging
from collections import defaultdict, deque
from typing import Any

import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# CACHE DE TABELAS (performance)
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
# METADATA
# =========================================
def get_tables(engine: Engine) -> list[str]:
    return inspect(engine).get_table_names(schema="public")


def get_table_info(engine: Engine, table_name: str) -> dict:
    insp = inspect(engine)

    return {
        "columns": insp.get_columns(table_name, schema="public"),
        "primary_keys": insp.get_pk_constraint(table_name, schema="public").get("constrained_columns", []),
        "foreign_keys": insp.get_foreign_keys(table_name, schema="public"),
    }


# =========================================
# DEPENDENCY GRAPH
# =========================================
def build_dependency_graph(engine: Engine, tables: list[str]) -> list[str]:
    insp = inspect(engine)

    deps = defaultdict(set)

    for table in tables:
        for fk in insp.get_foreign_keys(table, schema="public"):
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
        logger.warning(f"Ciclo detectado: {remaining}")
        ordered.extend(remaining)

    return ordered


# =========================================
# SCHEMA COPY
# =========================================
def copy_schema(source_engine: Engine, dest_engine: Engine) -> None:
    logger.info("Copiando estrutura do banco...")

    meta = sa.MetaData()

    #  carrega estrutura do banco origem
    meta.reflect(bind=source_engine, schema="public")

    #  REMOVE DEFAULT nextval (
    for table in meta.tables.values():
        for col in table.columns:
            if col.server_default is not None:
                if "nextval" in str(col.server_default).lower():
                    col.server_default = None

    #  cria no destino
    with dest_engine.begin() as conn:
        for table in meta.sorted_tables:
            try:
                table.schema = "public"
                table.create(bind=conn, checkfirst=True)
                logger.info(f"✔ Tabela '{table.name}' criada")
            except Exception as e:
                logger.error(f"Erro criando '{table.name}': {e}")
# =========================================
# DATA
# =========================================
def get_row_count(engine: Engine, table_name: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(f'SELECT COUNT(*) FROM "{table_name}"')
        ).scalar() or 0


def fetch_rows_chunked(engine: Engine, table_name: str, chunk_size: int = 1000):
    with engine.connect() as conn:
        offset = 0

        while True:
            rows = conn.execute(
                text(f'SELECT * FROM "{table_name}" LIMIT :limit OFFSET :offset'),
                {"limit": chunk_size, "offset": offset},
            ).mappings().fetchall()

            if not rows:
                break

            yield rows
            offset += chunk_size


# =========================================
# INSERT (COM CACHE)
# =========================================
def insert_rows(dest_engine: Engine, table_name: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return

    if table_name not in _TABLE_CACHE:
        meta = sa.MetaData()
        _TABLE_CACHE[table_name] = sa.Table(
            table_name,
            meta,
            autoload_with=dest_engine,
            schema="public",
        )

    table = _TABLE_CACHE[table_name]

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