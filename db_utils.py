import logging
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_TABLE_CACHE = {}

# ==================================================
# CONEXÃO
# ==================================================
def connect(url: str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        connect_args={
            "options": "-c client_encoding=latin1"
        }
    )

def execute_with_replica(conn):
    """Garante desativação de FK/TRIGGERS na sessão atual"""
    conn.execute(text("SET session_replication_role = 'replica'"))

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

    return sorted([
        s for s in insp.get_schema_names()
        if s not in ignored and not s.startswith("pg_")
    ])

# ==================================================
# TABELAS
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
# ESTRUTURA (DDL)
# ==================================================
def copy_schema(source_engine: Engine, dest_engine: Engine, schema: str):
    logger.info(f"📂 Copiando schema: {schema}")

    with dest_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    meta = sa.MetaData()
    meta.reflect(bind=source_engine, schema=schema)

    with dest_engine.begin() as conn:
        for table in meta.sorted_tables:
            table.schema = schema

            # remove defaults que podem quebrar insert
            for col in table.columns:
                col.server_default = None

            logger.info(f"🧱 Criando tabela: {schema}.{table.name}")
            table.create(bind=conn, checkfirst=True)

# ==================================================
# LIMPEZA
# ==================================================
def truncate_table(engine: Engine, table_name: str, schema: str):
    with engine.begin() as conn:
        execute_with_replica(conn)
        conn.execute(text(
            f'TRUNCATE TABLE "{schema}"."{table_name}" RESTART IDENTITY CASCADE'
        ))

# ==================================================
# STREAM DE DADOS
# ==================================================
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

# ==================================================
# INSERT
# ==================================================
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

    with dest_engine.begin() as conn:
        execute_with_replica(conn)
        conn.execute(table.insert(), rows)

# ==================================================
# SEQUENCES (CRÍTICO)
# ==================================================
def fix_sequences(engine: Engine, schema: str):
    logger.info(f"🔁 Ajustando sequences do schema: {schema}")

    query = f"""
    SELECT
        t.relname AS table_name,
        a.attname AS column_name,
        pg_get_serial_sequence('"{schema}".' || t.relname, a.attname) AS seq
    FROM pg_class t
    JOIN pg_namespace n ON n.oid = t.relnamespace
    JOIN pg_attribute a ON a.attrelid = t.oid
    WHERE t.relkind = 'r'
      AND n.nspname = :schema
      AND a.attnum > 0
      AND pg_get_serial_sequence('"{schema}".' || t.relname, a.attname) IS NOT NULL;
    """

    with engine.begin() as conn:
        rows = conn.execute(text(query), {"schema": schema}).fetchall()

        for table, column, seq in rows:
            if seq:
                logger.info(f"🔧 Fix sequence: {schema}.{table}.{column}")
                conn.execute(text(f"""
                    SELECT setval('{seq}',
                        COALESCE(
                            (SELECT MAX("{column}") FROM "{schema}"."{table}"),
                            1
                        ),
                        true
                    )
                """))

# ==================================================
# REPLICA MODE
# ==================================================
def set_replication_mode(engine: Engine, mode: str = 'replica'):
    with engine.begin() as conn:
        conn.execute(text(f"SET session_replication_role = '{mode}'"))

# ==================================================
# LIMPEZA FINAL
# ==================================================
def cleanup_empty_public_schema(engine: Engine):
    with engine.begin() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) 
            FROM information_schema.tables 
            WHERE table_schema='public'
        """)).scalar()

        if count == 0:
            logger.info("🧹 Removendo schema public vazio")
            conn.execute(text('DROP SCHEMA IF EXISTS "public" CASCADE'))