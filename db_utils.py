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

# O cache agora será mapeado usando a URL do banco para evitar colisão entre DBs diferentes
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
    admin_db = "postgres" if backend == "postgresql" else "master" if backend == "mssql" else None
    
    if admin_db:
        server_url = parsed.set(database=admin_db)
        engine_server = create_engine(server_url, isolation_level="AUTOCOMMIT", future=True)

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

def format_table_name(engine: Engine, schema: Optional[str], table: str) -> str:
    db_type = get_db_type(engine)
    if not schema:
        return f"`{table}`" if db_type == "mysql" else f'"{table}"'
    
    mapping = {
        "mysql": f"`{schema}`.`{table}`",
        "sqlite": f'"{table}"'
    }
    return mapping.get(db_type, f'"{schema}"."{table}"')

def table_exists(engine: Engine, schema: Optional[str], table: str) -> bool:
    return inspect(engine).has_table(table, schema=schema)

def get_tables(engine: Engine, schema: Optional[str] = None) -> List[str]:
    return sorted(inspect(engine).get_table_names(schema=schema))

def get_table_info(engine: Engine, table: str, schema: Optional[str] = None) -> Dict[str, Any]:
    insp = inspect(engine)
    return {
        "columns": insp.get_columns(table, schema=schema),
        "primary_keys": insp.get_pk_constraint(table, schema=schema).get("constrained_columns", []),
        "foreign_keys": insp.get_foreign_keys(table, schema=schema)
    }

def get_table_count(engine: Engine, table: str, schema: Optional[str] = None) -> int:
    try:
        # Usando SQLAlchemy Core ao invés de string crua
        tbl = Table(table, MetaData(), autoload_with=engine, schema=schema)
        with engine.connect() as conn:
            return conn.execute(sa.select(sa.func.count()).select_from(tbl)).scalar() or 0
    except Exception as e:
        logger.warning(f"COUNT fallback {schema or 'default'}.{table}: {e}")
        return 1000

def get_user_schemas(engine: Engine) -> List[str]:
    db_type, insp = get_db_type(engine), inspect(engine)
    ignored = {"information_schema", "pg_catalog", "pg_toast"} if db_type == "postgresql" else {"information_schema"}
    return sorted([s for s in insp.get_schema_names() if s not in ignored and not s.startswith("pg_")])

# ==================================================
# OPERAÇÕES DE SCHEMA E DADOS
# ==================================================
def copy_schema(src_engine: Engine, dst_engine: Engine, schema: Optional[str] = None):
    if schema and get_db_type(dst_engine) == "postgresql":
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
                logger.warning(f"CREATE SKIP {schema or 'default'}.{table.name}: {e}")

def fetch_rows_streaming(engine: Engine, table: str, schema: Optional[str] = None, chunk_size: int = 1000, order_by: Optional[str] = None) -> Generator:
    # Usando SQLAlchemy Core para garantir sanitização e independência de banco
    meta = MetaData()
    tbl = Table(table, meta, autoload_with=engine, schema=schema)
    stmt = sa.select(tbl)
    
    # Lógica aprimorada de ordenação
    if order_by:
        stmt = stmt.order_by(sa.column(order_by))
    else:
        # Fallback inteligente: tentar ordenar pela(s) chave(s) primária(s) para garantir consistência
        pks = [c for c in tbl.primary_key.columns]
        if pks:
            stmt = stmt.order_by(*pks)

    with engine.connect() as conn:
        result = conn.execution_options(stream_results=True).execute(stmt)
        while rows := result.mappings().fetchmany(chunk_size):
            yield rows

def _sanitize_row_data(table: Table, rows: List[Dict]) -> List[Dict]:
    safe_rows = []
    for row in rows:
        new_row = dict(row)
        for col in table.columns:
            val = new_row.get(col.name)
            if isinstance(val, str) and getattr(col.type, "length", None):
                new_row[col.name] = val[:col.type.length]
        safe_rows.append(new_row)
    return safe_rows

def insert_rows(engine: Engine, table_name: str, schema: Optional[str] = None, rows: List[Dict] = None, max_retries: int = 3, ignore_conflicts: bool = False):
    if not rows: return
    
    # Chave de cache mais robusta
    key = f"{engine.url}_{schema or 'default'}_{table_name}"
    if key not in _TABLE_CACHE:
        _TABLE_CACHE[key] = Table(table_name, MetaData(), autoload_with=engine, schema=schema)
    
    table = _TABLE_CACHE[key]
    db_type = engine.dialect.name

    for attempt in range(max_retries):
        try:
            safe_rows = _sanitize_row_data(table, rows)
            
            with engine.begin() as conn:
                if db_type == "postgresql" and ignore_conflicts:
                    # Só usa on_conflict_do_nothing se explicitamente solicitado (evita gastar sequências à toa)
                    stmt = pg_insert(table).values(safe_rows).on_conflict_do_nothing()
                    conn.execute(stmt)
                else:
                    # Insert padrão e limpo
                    conn.execute(table.insert(), safe_rows)
            return
        except Exception as e:
            logger.warning(f"⚠️ Erro tentativa {attempt+1} em {table_name}: {e}")
            time.sleep(1)

    logger.error(f"❌ Falha definitiva ao inserir em {table_name}")

def truncate_table(engine: Engine, table_name: str, schema: Optional[str] = None):
    if not table_exists(engine, schema, table_name):
        logger.warning(f"⚠️ SKIP TRUNCATE (não existe): {schema or 'default'}.{table_name}")
        return

    table_ref = format_table_name(engine, schema, table_name)
    main_query = f'TRUNCATE TABLE {table_ref} CASCADE' if get_db_type(engine) == "postgresql" else f'DELETE FROM {table_ref}'

    with engine.begin() as conn:
        try:
            conn.execute(text(main_query))
        except Exception as e:
            logger.warning(f"TRUNCATE fallback em {schema or 'default'}.{table_name}: {e}")
            conn.execute(text(f'DELETE FROM {table_ref}'))

# ==================================================
# DEPENDÊNCIAS
# ==================================================
def build_dependency_graph(engine: Engine, tables: List[str], schema: Optional[str] = None) -> List[str]:
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
                
    return ordered + [t for t in tables if t not in ordered]

def set_replication_mode(engine: Engine, mode: str = 'replica'):
    if get_db_type(engine) == "postgresql":
        with engine.begin() as conn:
            conn.execute(text(f"SET session_replication_role = '{mode}'"))