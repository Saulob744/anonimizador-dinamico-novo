import logging
from collections import defaultdict, deque

import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_TABLE_CACHE = {}

# ==================================================
# CONEXÃO
# ==================================================
def connect(url: str) -> Engine:
    """Conecta ao banco garantindo tratamento de caracteres brasileiros."""
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        # Forçamos o cliente a aceitar o que vier e o driver a usar latin1 se falhar
        connect_args={
            "options": "-c client_encoding=latin1" 
        }
    )
# ==================================================
# BANCO DESTINO
# ==================================================
def recreate_database_if_not_exists(server_url: str, db_name: str) -> None:
    engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": db_name}
            ).scalar()

            if not exists:
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
                logger.info(f"Banco criado: {db_name}")
    finally:
        engine.dispose()

# ==================================================
# SCHEMAS
# ==================================================
def get_user_schemas(engine: Engine):
    insp = inspect(engine)
    ignored = {"information_schema", "pg_catalog", "pg_toast"}
    
    schemas = []
    for schema in insp.get_schema_names():
        if schema in ignored or schema.startswith("pg_"):
            continue
        schemas.append(schema)
    
    return sorted(schemas)

# ==================================================
# TABELAS E INFOS
# ==================================================
def get_tables(engine: Engine, schema: str):
    insp = inspect(engine)
    return sorted(insp.get_table_names(schema=schema))

def get_table_info(engine: Engine, table_name: str, schema: str):
    insp = inspect(engine)
    return {
        "columns": insp.get_columns(table_name, schema=schema),
        "primary_keys": insp.get_pk_constraint(table_name, schema=schema).get("constrained_columns", []),
        "foreign_keys": insp.get_foreign_keys(table_name, schema=schema)
    }

# ==================================================
# ORDEM DE DEPENDÊNCIA (ORDENAÇÃO TOPOLÓGICA)
# ==================================================
def build_dependency_graph(engine: Engine, tables: list, schema: str):
    insp = inspect(engine)
    deps = defaultdict(set)
    in_degree = {t: 0 for t in tables}

    for table in tables:
        fks = insp.get_foreign_keys(table, schema=schema)
        for fk in fks:
            ref = fk.get("referred_table")
            if ref and ref in tables and ref != table:
                deps[ref].add(table)
                in_degree[table] += 1

    queue = deque([t for t in tables if in_degree[t] == 0])
    ordered = []

    while queue:
        current = queue.popleft()
        ordered.append(current)
        for dep in deps[current]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    remaining = [t for t in tables if t not in ordered]
    if remaining:
        logger.warning(f"Dependência circular ou complexa detectada: {remaining}")
        ordered.extend(remaining)

    return ordered

# ==================================================
# ESTRUTURA E DDL
# ==================================================
def copy_schema(source_engine: Engine, dest_engine: Engine, schema: str):
    logger.info(f"Refletindo estrutura do schema: {schema}")
    with dest_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    meta = sa.MetaData()
    meta.reflect(bind=source_engine, schema=schema)

    with dest_engine.begin() as conn:
        for table in meta.sorted_tables:
            table.schema = schema
            for col in table.columns:
                if col.server_default:
                    col.server_default = None
            
            logger.info(f"Criando estrutura da tabela: {schema}.{table.name}")
            table.create(bind=conn, checkfirst=True)

# --- FUNÇÃO QUE ESTAVA FALTANDO ---
def truncate_table(engine: Engine, table_name: str, schema: str) -> None:
    """Limpa a tabela antes de inserir novos dados."""
    with engine.begin() as conn:
        conn.execute(text(f'TRUNCATE TABLE "{schema}"."{table_name}" CASCADE'))

# ==================================================
# DADOS E STREAMING
# ==================================================
def get_row_count(engine: Engine, table_name: str, schema: str):
    with engine.connect() as conn:
        return conn.execute(text(f'SELECT COUNT(*) FROM "{schema}"."{table_name}"')).scalar()

def fetch_rows_streaming(engine: Engine, table_name: str, schema: str, chunk_size=1000):
    with engine.connect() as conn:
        result = conn.execution_options(stream_results=True).execute(
            text(f'SELECT * FROM "{schema}"."{table_name}"')
        )
        while True:
            rows = result.mappings().fetchmany(chunk_size)
            if not rows:
                break
            yield rows

def insert_rows(dest_engine: Engine, table_name: str, schema: str, rows: list):
    if not rows:
        return

    cache_key = f"{schema}.{table_name}"
    if cache_key not in _TABLE_CACHE:
        meta = sa.MetaData()
        _TABLE_CACHE[cache_key] = sa.Table(
            table_name, meta, autoload_with=dest_engine, schema=schema
        )

    table = _TABLE_CACHE[cache_key]
    try:
        with dest_engine.begin() as conn:
            conn.execute(table.insert(), rows)
    except Exception as e:
        logger.error(f"Erro no insert massivo em {schema}.{table_name}. Tentando recuperação...")
        for row in rows:
            try:
                with dest_engine.begin() as conn:
                    conn.execute(table.insert(), row)
            except Exception:
                continue

# ==================================================
# CONFIGURAÇÕES DE REPLICAÇÃO E LIMPEZA
# ==================================================
def set_replication_mode(engine: Engine, mode: str = 'replica') -> None:
    try:
        with engine.begin() as conn:
            conn.execute(text(f"SET session_replication_role = '{mode}'"))
    except Exception as e:
        logger.error(f"Erro ao definir session_replication_role para {mode}: {e}")

def cleanup_empty_public_schema(engine: Engine):
    with engine.begin() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'
        """)).scalar()
        if count == 0:
            conn.execute(text('DROP SCHEMA IF EXISTS "public" CASCADE'))