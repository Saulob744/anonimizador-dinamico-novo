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

# Configuração de Logs
logger = logging.getLogger(__name__)
_TABLE_CACHE: Dict[str, Table] = {}
# ==================================================
# PERFORMANCE E CPU
# ==================================================
def get_cpu_info() -> int:
    return os.cpu_count() or 1

def calculate_safe_workers(requested_cores: Optional[int] = None) -> int:
    total = get_cpu_info()
    if requested_cores:
        return min(requested_cores, total)
    return max(1, int(total * 0.75))
# ==================================================
# CONEXÃO E BANCO DE DADOS
# ==================================================
def connect(url: str) -> Engine:
    parsed = make_url(url)
    
    if "odbc_connect" in url:
        logger.warning("⚠️ ODBC detectado → usando conexão direta")
        return create_engine(url, pool_pre_ping=True, pool_recycle=3600, future=True)

    if not (db_name := parsed.database):
        raise ValueError("URL fornecida não contém um banco de dados.")

    backend = parsed.get_backend_name()
    # Define o DB padrão para conexão administrativa inicial
    admin_db = "postgres" if backend == "postgresql" else "master" if backend == "mssql" else None
    server_url = parsed.set(database=admin_db)
    
    engine_server = create_engine(server_url, isolation_level="AUTOCOMMIT", future=True)

    # Queries de criação por dialeto
    create_queries = {
        "postgresql": f'CREATE DATABASE "{db_name}"',
        "mysql": f"CREATE DATABASE IF NOT EXISTS `{db_name}`",
        "mssql": f"IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = '{db_name}') EXEC('CREATE DATABASE [{db_name}]')"
    }
    try:
        with engine_server.connect() as conn:
            if backend == "postgresql":
                exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}).scalar()
                if not exists:
                    conn.execute(text(create_queries[backend]))
            elif backend in create_queries:
                conn.execute(text(create_queries[backend]))
    except Exception as e:
        logger.error(f"Erro ao criar DB: {e}")
        raise
    finally:
        engine_server.dispose()

    return create_engine(url, pool_pre_ping=True, pool_recycle=3600, future=True)
# ==================================================
# INSPEÇÃO E FORMATAÇÃO
# ==================================================
def get_db_type(engine: Engine) -> str:
    return engine.dialect.name

def format_table_name(engine: Engine, schema: str, table: str) -> str:
    db_type = get_db_type(engine)
    mapping = {
        "mysql": f"`{schema}`.`{table}`",
        "sqlite": f'"{table}"'
    }
    return mapping.get(db_type, f'"{schema}"."{table}"')

def table_exists(engine: Engine, schema: str, table: str) -> bool:
    return inspect(engine).has_table(table, schema=schema)

def get_tables(engine: Engine, schema: str) -> List[str]:
    return sorted(inspect(engine).get_table_names(schema=schema))

def get_table_info(engine: Engine, table: str, schema: str) -> Dict[str, Any]:
    insp = inspect(engine)
    return {
        "columns": insp.get_columns(table, schema=schema),
        "primary_keys": insp.get_pk_constraint(table, schema=schema).get("constrained_columns", []),
        "foreign_keys": insp.get_foreign_keys(table, schema=schema)
    }

def get_table_count(engine: Engine, table: str, schema: str) -> int:
    try:
        full_name = format_table_name(engine, schema, table)
        with engine.connect() as conn:
            return conn.execute(text(f"SELECT COUNT(*) FROM {full_name}")).scalar() or 0
    except Exception as e:
        logger.warning(f"COUNT fallback {schema}.{table}: {e}")
        return 1000
def get_user_schemas(engine: Engine) -> List[str]:
    db_type, insp = get_db_type(engine), inspect(engine)
    ignored = {"information_schema", "pg_catalog", "pg_toast"} if db_type == "postgresql" else {"information_schema"}
    return sorted([s for s in insp.get_schema_names() if s not in ignored and not s.startswith("pg_")])
# ==================================================
# OPERAÇÕES DE SCHEMA E DADOS
# =================================================
def copy_schema(src_engine: Engine, dst_engine: Engine, schema: str):
    if get_db_type(dst_engine) == "postgresql":
        with dst_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    meta = MetaData()
    with src_engine.connect() as conn:
        meta.reflect(bind=conn, schema=schema, resolve_fks=False)

    with dst_engine.begin() as conn:
        for table in meta.sorted_tables:
            try:
                table.schema = schema
                for col in table.columns: 
                    col.server_default = None
                table.create(bind=conn, checkfirst=True)
            except Exception as e:
                logger.warning(f"CREATE SKIP {schema}.{table.name}: {e}")

def fetch_rows_streaming(engine: Engine, table: str, schema: str, chunk_size: int = 1000, order_by: Optional[str] = None) -> Generator:
    full_name = format_table_name(engine, schema, table)
    
    # Constrói a query com ordenação se uma coluna for fornecida
    query_str = f"SELECT * FROM {full_name}"
    if order_by:
        # Sanitização básica para evitar problemas com nomes de colunas reservados
        db_type = get_db_type(engine)
        quoted_pk = f'"{order_by}"' if db_type != "mysql" else f"`{order_by}`"
        query_str += f" ORDER BY {quoted_pk}"
    
    with engine.connect() as conn:
        # execution_options(stream_results=True) é vital para não estourar a RAM
        result = conn.execution_options(stream_results=True).execute(text(query_str))
        while rows := result.mappings().fetchmany(chunk_size):
            yield rows

def _sanitize_row_data(table: Table, rows: List[Dict]) -> List[Dict]:
    """Trunca strings que excedem o limite da coluna."""
    safe_rows = []
    for row in rows:
        new_row = dict(row)
        for col in table.columns:
            val = new_row.get(col.name)
            if isinstance(val, str) and getattr(col.type, "length", None):
                new_row[col.name] = val[:col.type.length]
        safe_rows.append(new_row)
    return safe_rows

def insert_rows(engine: Engine, table_name: str, schema: str, rows: List[Dict], max_retries: int = 3):
    if not rows: return
    
    key = f"{schema}.{table_name}"
    if key not in _TABLE_CACHE:
        _TABLE_CACHE[key] = Table(table_name, MetaData(), autoload_with=engine, schema=schema)
    
    table = _TABLE_CACHE[key]
    db_type = engine.dialect.name

    for attempt in range(max_retries):
        try:
            safe_rows = _sanitize_row_data(table, rows)
            
            with engine.begin() as conn:
                if db_type == "postgresql":
                    stmt = pg_insert(table).values(safe_rows).on_conflict_do_nothing()
                    conn.execute(stmt)
                else:
                    conn.execute(table.insert(), safe_rows)
            return
        except Exception as e:
            logger.warning(f"⚠️ Erro tentativa {attempt+1} em {key}: {e}")
            time.sleep(1)

    logger.error(f"❌ Falha definitiva em {key}")

def truncate_table(engine: Engine, table_name: str, schema: str):
    if not table_exists(engine, schema, table_name):
        logger.warning(f"⚠️ SKIP TRUNCATE (não existe): {schema}.{table_name}")
        return

    table_ref = format_table_name(engine, schema, table_name)
    main_query = f'TRUNCATE TABLE {table_ref} CASCADE' if get_db_type(engine) == "postgresql" else f'DELETE FROM {table_ref}'

    with engine.begin() as conn:
        try:
            conn.execute(text(main_query))
        except Exception as e:
            logger.warning(f"TRUNCATE fallback em {schema}.{table_name}: {e}")
            conn.execute(text(f'DELETE FROM {table_ref}'))
# ==================================================
# DEPENDÊNCIAS
# ==================================================
def build_dependency_graph(engine: Engine, tables: List[str], schema: str) -> List[str]:
    insp = inspect(engine)
    deps = defaultdict(set)
    in_degree = {t: 0 for t in tables}

    for table in tables:
        for fk in insp.get_foreign_keys(table, schema=schema):
            ref = fk.get("referred_table")
            if ref in tables and ref != table:
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
    # Retorna ordenados + remanescentes (em caso de ciclos ou tabelas isoladas)
    return ordered + [t for t in tables if t not in ordered]

def set_replication_mode(engine: Engine, mode: str = 'replica'):
    if get_db_type(engine) == "postgresql":
        with engine.begin() as conn:
            conn.execute(text(f"SET session_replication_role = '{mode}'"))