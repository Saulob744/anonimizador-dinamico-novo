import logging
from collections import defaultdict, deque
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text

logger = logging.getLogger(__name__)
_TABLE_CACHE = {}

# ==================================================
# CONEXÃO
# ==================================================
def connect(url: str):
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True
    )

# ==================================================
# BANCO
# ==================================================
def recreate_database_if_not_exists(server_url: str, db_name: str):
    engine = create_engine(server_url, isolation_level="AUTOCOMMIT", future=True)

    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": db_name}
        ).scalar()

        if not exists:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
            logger.info(f"Banco criado: {db_name}")

# ==================================================
# SCHEMAS
# ==================================================
def get_user_schemas(engine):
    insp = inspect(engine)
    ignored = {"information_schema", "pg_catalog", "pg_toast"}

    return sorted([
        s for s in insp.get_schema_names()
        if s not in ignored and not s.startswith("pg_")
    ])

# ==================================================
# TABELAS
# ==================================================
def get_tables(engine, schema):
    return sorted(inspect(engine).get_table_names(schema=schema))

def get_table_info(engine, table, schema):
    insp = inspect(engine)
    return {
        "columns": insp.get_columns(table, schema=schema),
        "primary_keys": insp.get_pk_constraint(table, schema=schema).get("constrained_columns", []),
        "foreign_keys": insp.get_foreign_keys(table, schema=schema)
    }

# ==================================================
# DEPENDÊNCIA (🔥 FUNDAMENTAL)
# ==================================================
def build_dependency_graph(engine, tables, schema):
    insp = inspect(engine)

    deps = defaultdict(set)
    in_degree = {t: 0 for t in tables}

    for table in tables:
        for fk in insp.get_foreign_keys(table, schema=schema):
            ref = fk.get("referred_table")

            if ref and ref in tables and ref != table:
                deps[ref].add(table)
                in_degree[table] += 1

    queue = deque([t for t in tables if in_degree[t] == 0])
    ordered = []

    while queue:
        t = queue.popleft()
        ordered.append(t)

        for d in deps[t]:
            in_degree[d] -= 1
            if in_degree[d] == 0:
                queue.append(d)

    remaining = [t for t in tables if t not in ordered]

    if remaining:
        logger.warning(f"Tabelas com dependência circular: {remaining}")

    return ordered + remaining

# ==================================================
# SCHEMA COPY
# ==================================================
def copy_schema(src_engine, dst_engine, schema):

    with dst_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    meta = sa.MetaData()

    with src_engine.connect() as conn:
        meta.reflect(bind=conn, schema=schema)

    with dst_engine.begin() as conn:
        for table in meta.sorted_tables:
            table.schema = schema

            for col in table.columns:
                col.server_default = None

            table.create(bind=conn, checkfirst=True)

# ==================================================
# STREAMING
# ==================================================
def fetch_rows_streaming(engine, table, schema, chunk_size=1000):

    with engine.connect() as conn:
        result = conn.execution_options(stream_results=True).execute(
            text(f'SELECT * FROM "{schema}"."{table}"')
        )

        while True:
            rows = result.mappings().fetchmany(chunk_size)
            if not rows:
                break
            yield rows

# ==================================================
# INSERT (🔥 SUPER ROBUSTO)
# ==================================================
def insert_rows(engine, table_name, schema, rows):
    if not rows:
        return

    key = f"{schema}.{table_name}"

    if key not in _TABLE_CACHE:
        meta = sa.MetaData()
        _TABLE_CACHE[key] = sa.Table(
            table_name,
            meta,
            autoload_with=engine,
            schema=schema
        )

    table = _TABLE_CACHE[key]

    try:
        with engine.begin() as conn:
            # 🔥 sempre desativa FK na conexão atual
            conn.execute(text("SET session_replication_role = 'replica'"))

            # 🔥 proteção contra tamanho de coluna
            for row in rows:
                for col in table.columns:
                    val = row.get(col.name)

                    if isinstance(val, str) and hasattr(col.type, "length") and col.type.length:
                        row[col.name] = val[:col.type.length]

            conn.execute(table.insert(), rows)

    except Exception as e:
        logger.error(f"❌ Erro batch {schema}.{table_name}: {e}")

        # 🔥 fallback linha a linha (evita tabela vazia)
        for row in rows:
            try:
                with engine.begin() as conn:
                    conn.execute(text("SET session_replication_role = 'replica'"))
                    conn.execute(table.insert(), row)

            except Exception as e2:
                logger.error(f"⚠️ Linha ignorada em {table_name}: {e2}")

# ==================================================
# COUNT (debug)
# ==================================================
def get_row_count(engine, table, schema):
    with engine.connect() as conn:
        return conn.execute(
            text(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
        ).scalar()

# ==================================================
# SEQUENCES (🔥 ESSENCIAL)
# ==================================================
def fix_sequences(engine, schema):

    query = """
    SELECT
        sequence_name,
        column_name,
        table_name
    FROM information_schema.sequences s
    JOIN information_schema.columns c
      ON c.column_default LIKE '%' || s.sequence_name || '%'
    WHERE sequence_schema = :schema
    """

    with engine.begin() as conn:
        rows = conn.execute(text(query), {"schema": schema}).fetchall()

        for seq, col, table in rows:
            conn.execute(text(f"""
                SELECT setval('{schema}.{seq}',
                COALESCE((SELECT MAX("{col}") FROM "{schema}"."{table}"), 1))
            """))

# ==================================================
# REPLICATION
# ==================================================
def set_replication_mode(engine, mode='replica'):
    with engine.begin() as conn:
        conn.execute(text(f"SET session_replication_role = '{mode}'"))

# ==================================================
# TRUNCATE
# ==================================================
def truncate_table(engine, table_name, schema):
    with engine.begin() as conn:
        conn.execute(
            text(f'TRUNCATE TABLE "{schema}"."{table_name}" CASCADE')
        )