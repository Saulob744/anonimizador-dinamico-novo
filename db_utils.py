import os
import logging
import time
import re
from collections import defaultdict, deque
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url
import hashlib

logger = logging.getLogger(__name__)
_TABLE_CACHE = {}

# ==================================================
# PERFORMANCE E CPU
# ==================================================
def get_cpu_info() -> int:
    return os.cpu_count() or 1

# ==================================================
# CONEXÃO E CRIAÇÃO DE BANCOS
# ==================================================
def connect(url: str):
    parsed = make_url(url)

    if "odbc_connect" in url:
        logger.warning("⚠️ ODBC detectado → usando conexão direta")
        return create_engine(url, pool_pre_ping=True, pool_recycle=3600, future=True)

    db_name = parsed.database
    if not db_name:
        raise ValueError("URL sem database")

    backend = parsed.get_backend_name()
    server_url = parsed.set(database="postgres" if backend == "postgresql" else "master" if backend == "mssql" else None)
    
    try:
        engine_server = create_engine(server_url, isolation_level="AUTOCOMMIT", future=True)
        with engine_server.connect() as conn:
            if backend == "postgresql":
                if not conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}).scalar():
                    conn.execute(text(f'CREATE DATABASE "{db_name}"'))
            elif backend == "mysql":
                conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db_name}`"))
            elif backend == "mssql":
                conn.execute(text(f"IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = '{db_name}') EXEC('CREATE DATABASE [{db_name}]')"))
    except Exception as e:
        logger.warning(f"⚠️ Acesso root bloqueado. Assumindo que '{db_name}' já existe. Seguindo para conexão normal...")

    connect_args = {}
    if backend == "postgresql":
        connect_args = {
            "keepalives": 1,
            "keepalives_idle": 30,       
            "keepalives_interval": 10,   
            "keepalives_count": 5        
        }

    return create_engine(
        url, 
        pool_pre_ping=True, 
        pool_recycle=1800,
        connect_args=connect_args, 
        future=True
    )

# ==================================================
# UTILITÁRIOS E INSPEÇÃO
# ==================================================
def get_db_type(engine):
    return engine.dialect.name

def format_table_name(engine, schema, table):
    db_type = get_db_type(engine)
    if db_type == "mysql": return f"`{schema}`.`{table}`"
    if db_type == "sqlite": return f'"{table}"'
    return f'"{schema}"."{table}"'

def table_exists(engine, schema, table):
    return inspect(engine).has_table(table, schema=schema)

def get_tables(engine, schema):
    return sorted(inspect(engine).get_table_names(schema=schema))

def get_table_info(engine, table, schema):
    insp = inspect(engine)
    return {
        "columns": insp.get_columns(table, schema=schema),
        "primary_keys": insp.get_pk_constraint(table, schema=schema).get("constrained_columns", []),
        "foreign_keys": insp.get_foreign_keys(table, schema=schema)
    }

def get_table_count(engine, table, schema):
    try:
        with engine.connect() as conn:
            return conn.execute(text(f"SELECT COUNT(*) FROM {format_table_name(engine, schema, table)}")).scalar() or 0
    except Exception as e:
        logger.warning(f"COUNT fallback {schema}.{table}: {e}")
        return 1000

# ==================================================
# GERENCIAMENTO DE SCHEMAS
# ==================================================
def get_user_schemas(engine):
    db_type, insp = get_db_type(engine), inspect(engine)
    ignored = {"information_schema", "pg_catalog", "pg_toast"} if db_type == "postgresql" else {"information_schema"}
    return sorted([s for s in insp.get_schema_names() if s not in ignored and not s.startswith("pg_")])

def copy_schema(src_engine, dst_engine, schema):
    if get_db_type(dst_engine) == "postgresql":
        with dst_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    meta = sa.MetaData()
    with src_engine.connect() as conn:
        meta.reflect(bind=conn, schema=schema, resolve_fks=False)

    with dst_engine.begin() as conn:
        for table in meta.sorted_tables:
            try:
                table.schema = schema
                for col in table.columns: col.server_default = None
                table.create(bind=conn, checkfirst=True)
            except Exception as e:
                logger.warning(f"CREATE SKIP {schema}.{table.name}: {e}")

# ==================================================
# LEITURA E ESCRITA 
# ==================================================
def fetch_rows_streaming(engine, table, schema, chunk_size=1000):
    key = f"{schema}.{table}"
    
    if key not in _TABLE_CACHE:
        _TABLE_CACHE[key] = sa.Table(table, sa.MetaData(), autoload_with=engine, schema=schema)
    t_ref = _TABLE_CACHE[key]

    select_cols = []
    for col in t_ref.columns:
        if isinstance(col.type, (sa.Date, sa.DateTime, sa.TIMESTAMP, sa.TIME)):
            select_cols.append(sa.cast(col, sa.String).label(col.name))
        else:
            select_cols.append(col)

    query = sa.select(*select_cols)

    with engine.connect() as conn:
        result = conn.execution_options(stream_results=True).execute(query)
        while rows := result.mappings().fetchmany(chunk_size):
            yield rows

def insert_rows(engine, table_name, schema, rows, max_retries=3):
    if not rows: return
    key = f"{schema}.{table_name}"

    if key not in _TABLE_CACHE:
        _TABLE_CACHE[key] = sa.Table(table_name, sa.MetaData(), autoload_with=engine, schema=schema)
    table = _TABLE_CACHE[key]
    num_cols = len(table.columns)

    max_rows_per_chunk = max(1, 30000 // max(num_cols, 1))
    sub_chunks = [rows[i:i + max_rows_per_chunk] for i in range(0, len(rows), max_rows_per_chunk)]

    for chunk in sub_chunks:
        success = False
        
        safe_rows = []
        for row in chunk:
            new_row = dict(row)
            for col in table.columns:
                val = new_row.get(col.name)
                if isinstance(val, str) and hasattr(col.type, "length") and col.type.length:
                    new_row[col.name] = val[:col.type.length]
            safe_rows.append(new_row)

        for attempt in range(max_retries):
            try:
                with engine.begin() as conn:
                    conn.execute(table.insert(), safe_rows)
                success = True
                break
            except Exception as e:
                logger.warning(f"⚠️ Retry {attempt+1} em {key}. Banco rejeitou o lote. Erro: {str(e)[:150]}...")
                time.sleep(0.5)

        if not success:
            logger.warning(f"🔄 Fallback Unitário ativado em {key}: Salvando linha a linha para isolar o erro.")
            for idx, row in enumerate(safe_rows):
                try:
                    with engine.begin() as trans_conn:
                        trans_conn.execute(table.insert(), [row])
                except Exception as inner_e:
                 
                    row_signature = hashlib.sha256(str(row).encode('utf-8')).hexdigest()[:8]
                    
                    logger.error(f"🚨 [FALHA DEFINITIVA NO REGISTRO - {key}]")
                    logger.error(f"👉 Assinatura (Hash) do Registro: {row_signature}")
                    logger.error(f"👉 Mensagem Técnica: {str(inner_e)[:200]}")

def truncate_table(engine, table_name, schema):
    if not table_exists(engine, schema, table_name):
        return logger.warning(f"⚠️ SKIP TRUNCATE (não existe): {schema}.{table_name}")

    table_ref = format_table_name(engine, schema, table_name)
    query = f'TRUNCATE TABLE {table_ref} CASCADE' if get_db_type(engine) == "postgresql" else f'DELETE FROM {table_ref}'

    try:
        with engine.begin() as conn:
            conn.execute(text(query))
    except Exception as e:
        logger.warning(f"TRUNCATE fallback {schema}.{table_name}: {e}")
        try:
            with engine.begin() as conn:
                conn.execute(text(f'DELETE FROM {table_ref}'))
        except Exception as e2:
            logger.error(f"❌ TRUNCATE FAILED TOTAL: {schema}.{table_name} -> {e2}")

# ==================================================
# DEPENDÊNCIAS E REPLICAÇÃO
# ==================================================
def build_dependency_graph(engine, tables, schema):
    insp = inspect(engine)
    deps, in_degree = defaultdict(set), {t: 0 for t in tables}

    for table in tables:
        for fk in insp.get_foreign_keys(table, schema=schema):
            ref = fk.get("referred_table")
            if ref in tables and ref != table:
                deps[ref].add(table)
                in_degree[table] += 1

    queue, ordered = deque([t for t in tables if in_degree[t] == 0]), []

    while queue:
        t = queue.popleft()
        ordered.append(t)
        for d in deps[t]:
            in_degree[d] -= 1
            if in_degree[d] == 0: queue.append(d)

    return ordered + [t for t in tables if t not in ordered]

def set_replication_mode(engine, mode='replica'):
    if get_db_type(engine) == "postgresql":
        with engine.begin() as conn:
            conn.execute(text(f"SET session_replication_role = '{mode}'"))