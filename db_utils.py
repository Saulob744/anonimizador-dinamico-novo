import logging
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

_TABLE_CACHE = {}

def connect(url: str) -> Engine:
    """Conecta ao banco forçando UTF-8 para evitar erro de 'ç' e 'ã'."""
    return create_engine(
        url, 
        pool_pre_ping=True, 
        connect_args={'options': '-c client_encoding=utf8'}
    )

def recreate_database_if_not_exists(server_url: str, db_name: str) -> None:
    engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
            logger.info(f"Banco '{db_name}' criado com sucesso.")
    engine.dispose()

def truncate_table(engine: Engine, table_name: str, schema: str) -> None:
    """Limpa a tabela antes da migração para evitar erros de duplicidade (UniqueViolation)."""
    with engine.begin() as conn:
        conn.execute(text(f'TRUNCATE TABLE "{schema}"."{table_name}" CASCADE'))

def get_user_schemas(engine: Engine) -> list[str]:
    insp = inspect(engine)
    system_schemas = {'information_schema', 'pg_catalog', 'pg_toast'}
    return [s for s in insp.get_schema_names() if s not in system_schemas and not s.startswith('pg_temp')]

def get_tables(engine: Engine, schema: str) -> list[str]:
    insp = inspect(engine)
    return insp.get_table_names(schema=schema)

def get_table_info(engine: Engine, table_name: str, schema: str) -> dict:
    insp = inspect(engine)
    return {
        "columns": insp.get_columns(table_name, schema=schema),
        "primary_keys": insp.get_pk_constraint(table_name, schema=schema).get("constrained_columns", []),
        "foreign_keys": insp.get_foreign_keys(table_name, schema=schema),
    }

def build_dependency_graph(engine: Engine, tables: list[str], schema: str) -> list[str]:
    insp = inspect(engine)
    deps = defaultdict(set)
    in_degree = {t: 0 for t in tables}
    for table in tables:
        for fk in insp.get_foreign_keys(table, schema=schema):
            ref = fk.get("referred_table")
            if ref and ref != table and ref in tables:
                deps[ref].add(table)
                in_degree[table] += 1
    queue = deque([t for t in tables if in_degree[t] == 0])
    ordered = []
    while queue:
        u = queue.popleft()
        ordered.append(u)
        for v in deps[u]:
            in_degree[v] -= 1
            if in_degree[v] == 0:
                queue.append(v)
    remaining = [t for t in tables if t not in ordered]
    ordered.extend(remaining)
    return ordered

def copy_schema(source_engine: Engine, dest_engine: Engine, schema: str) -> None:
    with dest_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    meta = sa.MetaData()
    meta.reflect(bind=source_engine, schema=schema)
    for table in meta.tables.values():
        for col in table.columns:
            if col.server_default and "nextval" in str(col.server_default).lower():
                col.server_default = None
    with dest_engine.begin() as conn:
        for table in meta.sorted_tables:
            table.schema = schema
            table.create(bind=conn, checkfirst=True)

def fetch_rows_streaming(engine: Engine, table_name: str, schema: str, chunk_size: int = 1000):
    with engine.connect() as conn:
        result = conn.execution_options(stream_results=True).execute(
            text(f'SELECT * FROM "{schema}"."{table_name}"')
        )
        while True:
            chunk = result.mappings().fetchmany(chunk_size)
            if not chunk: break
            yield chunk

def insert_rows(dest_engine: Engine, table_name: str, schema: str, rows: list) -> None:
    """Insere em massa. Se falhar, tenta linha por linha para identificar o erro."""
    if not rows: return
    cache_key = f"{schema}.{table_name}"
    
    if cache_key not in _TABLE_CACHE:
        meta = sa.MetaData()
        _TABLE_CACHE[cache_key] = sa.Table(table_name, meta, autoload_with=dest_engine, schema=schema)
    
    table = _TABLE_CACHE[cache_key]
    
    try:
        # Tenta o lote inteiro (Rápido)
        with dest_engine.begin() as conn:
            conn.execute(table.insert(), rows)
    except Exception as e:
        # Se falhar (ex: erro de Foreign Key ou Tipo), tenta UM POR UM (Seguro)
        print(f"⚠️ Lote falhou na tabela {table_name}. Tentando modo de recuperação...")
        for i, row in enumerate(rows):
            try:
                with dest_engine.begin() as conn:
                    conn.execute(table.insert(), row)
            except Exception as row_e:
                print(f"❌ ERRO CRÍTICO na linha {i} da tabela {table_name}:")
                print(f"DADO: {row}")
                print(f"MOTIVO: {str(row_e)}")
                # Aqui você decide: para tudo ou pula a linha. Vamos pular para não perder o resto.
                continue

def set_replication_mode(engine: Engine, mode: str = 'replica') -> None:
    with engine.begin() as conn:
        conn.execute(text(f"SET session_replication_role = '{mode}'"))

def cleanup_empty_public_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        query = text("""
            SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'
        """)
        count = conn.execute(query).scalar()
        if count == 0:
            conn.execute(text('DROP SCHEMA IF EXISTS "public" CASCADE'))