import logging
from collections import defaultdict, deque
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url
import subprocess

logger = logging.getLogger(__name__)
_TABLE_CACHE = {}

# ==================================================
# CONEXÃO INTELIGENTE (AUTO CREATE DB)
# ==================================================
def connect(url: str):
    parsed = make_url(url)

    # 🔥 ODBC (LocalDB etc)
    if "odbc_connect" in url:
        logger.warning("⚠️ ODBC detectado → pulando criação de banco")
        return create_engine(url, pool_pre_ping=True, pool_recycle=3600, future=True)

    db_name = parsed.database
    if not db_name:
        raise ValueError("URL não contém nome do banco")

    backend = parsed.get_backend_name()

    # -----------------------------------------
    # DEFINE BANCO ADMIN
    # -----------------------------------------
    if backend == "postgresql":
        server_url = parsed.set(database="postgres")
    elif backend == "mssql":
        server_url = parsed.set(database="master")
    else:
        server_url = parsed.set(database=None)

    engine_server = create_engine(
        server_url,
        isolation_level="AUTOCOMMIT",
        future=True
    )

    # -----------------------------------------
    # CREATE DATABASE
    # -----------------------------------------
    try:
        with engine_server.connect() as conn:

            if backend == "postgresql":
                exists = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": db_name}
                ).scalar()

                if not exists:
                    conn.execute(text(f'CREATE DATABASE "{db_name}"'))

            elif backend == "mysql":
                conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db_name}`"))

            elif backend == "mssql":
                conn.execute(text(f"""
                    IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = '{db_name}')
                    BEGIN
                        DECLARE @sql NVARCHAR(MAX)
                        SET @sql = 'CREATE DATABASE [{db_name}]'
                        EXEC(@sql)
                    END
                """))

    except Exception as e:
        logger.error(f"❌ Erro ao criar banco: {e}")
        raise

    # -----------------------------------------
    # CONEXÃO FINAL
    # -----------------------------------------
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True
    )

# ==================================================
# UTILS
# ==================================================
def get_db_type(engine):
    return engine.dialect.name

def format_table_name(engine, schema, table):
    db_type = get_db_type(engine)

    if db_type == "mysql":
        return f"`{schema}`.`{table}`"
    elif db_type == "sqlite":
        return f'"{table}"'
    else:
        return f'"{schema}"."{table}"'

def disable_fk(conn, db_type):
    try:
        if db_type == "postgresql":
            conn.execute(text("SET session_replication_role = 'replica'"))

        elif db_type == "mysql":
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))

        elif db_type == "mssql":
            conn.execute(text("EXEC sp_msforeachtable 'ALTER TABLE ? NOCHECK CONSTRAINT all'"))
    except Exception as e:
        logger.warning(f"FK disable falhou: {e}")

# ==================================================
# SCHEMAS
# ==================================================
def get_user_schemas(engine):
    insp = inspect(engine)
    db_type = get_db_type(engine)

    ignored = {"information_schema"}

    if db_type == "postgresql":
        ignored.update({"pg_catalog", "pg_toast"})

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
# DEPENDÊNCIA
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
        logger.warning(f"Dependência circular: {remaining}")

    return ordered + remaining

# ==================================================
# SCHEMA COPY
# ==================================================
def copy_schema(src_engine, dst_engine, schema):
    db_type = get_db_type(dst_engine)

    with dst_engine.begin() as conn:
        try:
            if db_type == "postgresql":
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            elif db_type == "mssql":
                conn.execute(text(f"""
                    IF NOT EXISTS (SELECT * FROM sys.schemas WHERE name = '{schema}')
                    EXEC('CREATE SCHEMA [{schema}]')
                """))
        except Exception:
            pass

    meta = sa.MetaData()
    with src_engine.connect() as conn:
        meta.reflect(bind=conn, schema=schema)

    with dst_engine.begin() as conn:
        for table in meta.sorted_tables:
            table.schema = schema if db_type != "sqlite" else None

            for col in table.columns:
                col.server_default = None

            table.create(bind=conn, checkfirst=True)

# ==================================================
# STREAMING
# ==================================================
def fetch_rows_streaming(engine, table, schema, chunk_size=1000):
    table_ref = format_table_name(engine, schema, table)

    with engine.connect() as conn:
        result = conn.execution_options(stream_results=True).execute(
            text(f"SELECT * FROM {table_ref}")
        )

        while True:
            rows = result.mappings().fetchmany(chunk_size)
            if not rows:
                break
            yield rows

# ==================================================
# INSERT
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
    db_type = get_db_type(engine)

    try:
        with engine.begin() as conn:
            disable_fk(conn, db_type)

            for row in rows:
                for col in table.columns:
                    val = row.get(col.name)

                    if isinstance(val, str) and hasattr(col.type, "length") and col.type.length:
                        row[col.name] = val[:col.type.length]

            conn.execute(table.insert(), rows)

    except Exception as e:
        logger.error(f"❌ Batch error {schema}.{table_name}: {e}")

# ==================================================
# TRUNCATE
# ==================================================
def truncate_table(engine, table_name, schema):
    db_type = get_db_type(engine)
    table_ref = format_table_name(engine, schema, table_name)

    with engine.begin() as conn:
        try:
            if db_type == "postgresql":
                conn.execute(text(f'TRUNCATE TABLE {table_ref} CASCADE'))
            else:
                conn.execute(text(f'DELETE FROM {table_ref}'))
        except Exception:
            conn.execute(text(f'DELETE FROM {table_ref}'))

# ==================================================
# SEQUENCES
# ==================================================
def fix_sequences(engine, schema):
    if get_db_type(engine) != "postgresql":
        return

    with engine.begin() as conn:
        conn.execute(text(f"""
            DO $$
            DECLARE r RECORD;
            BEGIN
                FOR r IN
                    SELECT sequence_name, table_name, column_name
                    FROM information_schema.sequences s
                    JOIN information_schema.columns c
                    ON c.column_default LIKE '%' || s.sequence_name || '%'
                    WHERE sequence_schema = '{schema}'
                LOOP
                    EXECUTE format(
                        'SELECT setval(''%I.%I'', COALESCE((SELECT MAX(%I) FROM %I.%I),1))',
                        '{schema}', r.sequence_name, r.column_name, '{schema}', r.table_name
                    );
                END LOOP;
            END$$;
        """))

# ==================================================
# REPLICATION
# ==================================================
def set_replication_mode(engine, mode='replica'):
    if get_db_type(engine) == "postgresql":
        with engine.begin() as conn:
            conn.execute(text(f"SET session_replication_role = '{mode}'"))

# ==================================================
# LOCALDB
# ==================================================
def create_db_localdb(db_name):
    subprocess.run(
        [
            "sqlcmd",
            "-S", r"(localdb)\MSSQLLocalDB",
            "-Q", f"IF DB_ID('{db_name}') IS NULL CREATE DATABASE [{db_name}]"
        ],
        check=True
    )