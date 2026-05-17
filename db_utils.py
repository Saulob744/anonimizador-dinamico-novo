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
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

# ==================================================
# LOGS
# ==================================================
logger = logging.getLogger(__name__)
# DICA: Certifique-se de configurar o nível básico do logger no seu arquivo principal:
# logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(message)s')

_TABLE_CACHE: Dict[str, Table] = {}

# ==================================================
# ENCODING / SANITIZAÇÃO
# ==================================================
def safe_decode(value):
    """
    Corrige problemas de encoding sem derrubar pipeline.
    """
    if value is None:
        return None
    # bytes -> utf8 -> latin1 -> replace
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            logger.debug(f"Falha ao decodificar UTF-8 para o valor (bytes), tentando latin1.")
            try:
                return value.decode("latin1")
            except Exception:
                logger.debug(f"Falha ao decodificar latin1, usando replace.")
                return value.decode("utf-8", errors="replace")

    # strings problemáticas
    if isinstance(value, str):
        try:
            value.encode("utf-8")
            return value
        except UnicodeEncodeError:
            logger.debug(f"Falha de encoding na string, aplicando replace.")
            return value.encode(
                "utf-8",
                errors="replace"
            ).decode("utf-8")

    return value

# ==================================================
# PERFORMANCE E CPU
# ==================================================
def get_cpu_info() -> int:
    cores = os.cpu_count() or 1
    logger.debug(f"CPU info: detectados {cores} cores.")
    return cores

def calculate_safe_workers(
    requested_cores: Optional[int] = None
) -> int:
    total = get_cpu_info()
    if requested_cores:
        safe_workers = min(requested_cores, total)
    else:
        safe_workers = max(1, int(total * 0.75))
    
    logger.debug(f"Workers calculados: {safe_workers} (Requisitado: {requested_cores}, Total CPU: {total})")
    return safe_workers

# ==================================================
# CONEXÃO E BANCO
# ==================================================
def connect(url: str) -> Engine:
    parsed = make_url(url)
    logger.info(f"🔌 Iniciando conexão com o banco: {parsed.host} / {parsed.database}")

    if "odbc_connect" in url:
        logger.warning("⚠️ ODBC detectado → usando conexão direta")
        return create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            future=True
        )

    if not (db_name := parsed.database):
        logger.error("❌ Falha na conexão: URL fornecida não contém um banco de dados.")
        raise ValueError("URL fornecida não contém um banco de dados.")

    backend = parsed.get_backend_name()
    logger.debug(f"Backend detectado: {backend}")

    admin_db = (
        "postgres"
        if backend == "postgresql"
        else "master"
        if backend == "mssql"
        else None
    )
    
    # ==================================================
    # CRIAÇÃO DE DATABASE
    # =================================================
    if admin_db:
        logger.info(f"Verificando existência do banco '{db_name}' através do admin_db '{admin_db}'")
        server_url = parsed.set(database=admin_db)
        try:
            engine_server = create_engine(
                server_url,
                isolation_level="AUTOCOMMIT",
                future=True
            )
            create_queries = {
                "postgresql": f'CREATE DATABASE "{db_name}"',
                "mysql": f"CREATE DATABASE IF NOT EXISTS `{db_name}`",
                "mssql": f"""
                    IF NOT EXISTS (
                        SELECT name
                        FROM sys.databases
                        WHERE name = '{db_name}'
                    )
                    EXEC('CREATE DATABASE [{db_name}]')
                    """
            }
            with engine_server.connect() as conn:
                if backend == "postgresql":
                    exists = conn.execute(
                        text("""
                            SELECT 1
                            FROM pg_database
                            WHERE datname = :name
                        """),
                        {"name": db_name}
                    ).scalar()

                    if not exists:
                        logger.info(f"⚙️ Banco '{db_name}' não existe. Tentando criar...")
                        conn.execute(text(create_queries[backend]))
                        logger.info(f"✅ Banco '{db_name}' criado com sucesso.")
                    else:
                        logger.debug(f"✅ Banco '{db_name}' já existe.")
                        
                elif backend in create_queries:
                    logger.debug(f"⚙️ Executando query de criação para {backend}...")
                    conn.execute(text(create_queries[backend]))
                    logger.info(f"✅ Verificação/Criação do banco '{db_name}' concluída.")

        except sa.exc.OperationalError as e:
            logger.warning(f"⚠️ Sem acesso ao banco admin para criar/verificar '{db_name}'. Erro: {e}")
        except sa.exc.ProgrammingError as e:
            logger.warning(f"⚠️ Sem permissão CREATE DATABASE para '{db_name}'. Erro: {e}")
        except Exception as e:
            logger.exception(f"❌ Erro inesperado ao criar/verificar DB '{db_name}': {e}")
        finally:
            if "engine_server" in locals():
                engine_server.dispose()
                logger.debug("Engine server (admin) descartado.")

    # ==================================================
    # ENGINE PRINCIPAL
    # ==================================================
    connect_args = {}
    if backend == "postgresql":
        connect_args["client_encoding"] = "utf8"
        
    logger.debug("Criando engine principal...")
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True,
        connect_args=connect_args
    )
    logger.info("✅ Engine principal criada com sucesso.")
    return engine

# ==================================================
# INSPEÇÃO
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
    logger.debug(f"Inspecionando se tabela {schema or 'default'}.{table} existe...")
    exists = inspect(engine).has_table(table, schema=schema)
    logger.debug(f"Tabela {table} existe? {exists}")
    return exists

def get_tables(engine: Engine, schema: Optional[str] = None) -> List[str]:
    logger.debug(f"Listando tabelas do schema '{schema or 'default'}'...")
    tables = sorted(inspect(engine).get_table_names(schema=schema))
    logger.debug(f"Tabelas encontradas: {len(tables)}")
    return tables

def get_table_info(engine: Engine, table: str, schema: Optional[str] = None) -> Dict[str, Any]:
    logger.debug(f"Obtendo informações da tabela {schema or 'default'}.{table}...")
    insp = inspect(engine)
    return {
        "columns": insp.get_columns(table, schema=schema),
        "primary_keys": insp.get_pk_constraint(table, schema=schema).get("constrained_columns", []),
        "foreign_keys": insp.get_foreign_keys(table, schema=schema)
    }

def get_table_count(engine: Engine, table: str, schema: Optional[str] = None) -> int:
    logger.debug(f"Calculando COUNT(*) para {schema or 'default'}.{table}...")
    try:
        tbl = Table(table, MetaData(), autoload_with=engine, schema=schema)
        with engine.connect() as conn:
            count = conn.execute(sa.select(sa.func.count()).select_from(tbl)).scalar() or 0
            logger.debug(f"COUNT calculado para {table}: {count}")
            return count
    except Exception as e:
        logger.exception(f"⚠️ Erro ao calcular COUNT para {schema or 'default'}.{table}. Retornando fallback (1000). Erro: {e}")
        return 1000

def get_user_schemas(engine: Engine) -> List[str]:
    logger.debug("Buscando schemas do usuário...")
    db_type = get_db_type(engine)
    insp = inspect(engine)
    ignored = (
        {"information_schema", "pg_catalog", "pg_toast"}
        if db_type == "postgresql"
        else {"information_schema"}
    )
    schemas = sorted([
        s for s in insp.get_schema_names()
        if s not in ignored and not s.startswith("pg_")
    ])
    logger.debug(f"Schemas encontrados: {schemas}")
    return schemas

# ==================================================
# SCHEMA
# ==================================================
def copy_schema(src_engine: Engine, dst_engine: Engine, schema: Optional[str] = None):
    logger.info(f"🔄 Iniciando cópia do schema '{schema or 'default'}'...")
    
    if schema and get_db_type(dst_engine) == "postgresql":
        try:
            logger.debug(f"Tentando criar schema '{schema}' no destino...")
            with dst_engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        except Exception as e:
            logger.warning(f"⚠️ Sem permissão para criar schema '{schema}': {e}")
            
    meta = MetaData()
    logger.debug(f"Refletindo tabelas da origem...")
    with src_engine.connect() as conn:
        meta.reflect(bind=conn, schema=schema, resolve_fks=False)
        
    tables_to_copy = meta.sorted_tables
    logger.info(f"Total de tabelas a serem copiadas: {len(tables_to_copy)}")
    
    with dst_engine.begin() as conn:
        for table in tables_to_copy:
            try:
                logger.debug(f"Criando tabela {table.name}...")
                table.schema = schema
                for col in table.columns:
                    col.server_default = None
                table.create(bind=conn, checkfirst=True)
            except Exception as e:
                logger.exception(f"⚠️ Falha ao criar tabela {schema or 'default'}.{table.name}: {e}")
                
    logger.info("✅ Cópia de schema finalizada.")

# ==================================================
# STREAMING
# ==================================================
# ==================================================
# STREAMING
# ==================================================
def fetch_rows_streaming(
    engine: Engine,
    table: str,
    schema: Optional[str] = None,
    chunk_size: int = 1000,
    order_by: Optional[str] = None
) -> Generator:
    
    logger.info(f"📥 Iniciando fetch streaming da tabela {schema or 'default'}.{table} (chunk: {chunk_size})")
    meta = MetaData()
    tbl = Table(table, meta, autoload_with=engine, schema=schema)
    stmt = sa.select(tbl)

    # 1. ORDENAÇÃO MANUAL (Prioridade Máxima se informada)
    if order_by:
        stmt = stmt.order_by(sa.text(order_by))
        logger.debug(f"Ordenando streaming explicitamente por: {order_by}")
        
    # 2. ORDENAÇÃO AUTOMÁTICA (Buscando Chave Primária ou Fallback)
    else:
        pks = [c for c in tbl.primary_key.columns]
        
        if pks:
            stmt = stmt.order_by(*pks)
            logger.debug(f"Ordenando streaming pelas PKs: {[pk.name for pk in pks]}")
        else:
            # 3. FALLBACK: Sem PK, tenta usar todas as colunas disponíveis para garantir alguma estabilidade.
            # O banco de dados pode não garantir a mesma ordem entre leituras diferentes se não houver um ORDER BY explícito.
            # Ordenar por todas as colunas é o mais próximo que podemos chegar de uma ordem determinística.
            logger.warning(f"⚠️ A tabela {table} NÃO possui Primary Key! Tentando ordenação de contingência (todas as colunas).")
            
            fallback_cols = []
            for col in tbl.columns:
                # Evita ordenar por colunas gigantes ou tipos binários/JSON que a maioria dos bancos rejeita no ORDER BY
                if not isinstance(col.type, (sa.LargeBinary, sa.JSON, sa.ARRAY, getattr(sa, 'JSONB', sa.String))):
                   fallback_cols.append(col)
                   
            if fallback_cols:
                 stmt = stmt.order_by(*fallback_cols)
                 logger.debug(f"Ordenação de contingência aplicada usando {len(fallback_cols)} colunas compatíveis.")
            else:
                 # Pior cenário absoluto: Uma tabela sem PK e só com tipos complexos (muito raro).
                 logger.error(f"❌ Impossível aplicar qualquer ordenação determinística na tabela {table}. A ordem das linhas extraídas será arbitrária (ordem natural do disco).")

    with engine.connect() as conn:
        logger.debug("Executando query de streaming...")
        
        # execution_options(stream_results=True) é essencial para não carregar a tabela inteira na RAM do Python.
        result = conn.execution_options(stream_results=True).execute(stmt)
        
        chunks_yielded = 0
        total_rows = 0
        
        while rows := result.mappings().fetchmany(chunk_size):
            safe_rows = []
            for row in rows:
                fixed = {}
                for k, v in row.items():
                    fixed[k] = safe_decode(v)
                safe_rows.append(fixed)
            
            chunks_yielded += 1
            total_rows += len(safe_rows)
            logger.debug(f"Yielding chunk #{chunks_yielded} com {len(safe_rows)} linhas. (Total até agora: {total_rows})")
            yield safe_rows
            
    logger.info(f"✅ Streaming finalizado para {table}. Total lido: {total_rows} linhas.")

# ==================================================
# SANITIZAÇÃO
# ==================================================
def _sanitize_row_data(table: Table, rows: List[Dict]) -> List[Dict]:
    logger.debug(f"Sanitizando {len(rows)} linhas para a tabela {table.name}...")
    safe_rows = []
    
    for row in rows:
        new_row = {}
        for col in table.columns:
            val = row.get(col.name)
            val = safe_decode(val)
            
            # Limita tamanho
            if isinstance(val, str) and getattr(col.type, "length", None):
                if len(val) > col.type.length:
                    logger.debug(f"Truncando valor da coluna '{col.name}' (tamanho {len(val)} excede {col.type.length})")
                val = val[:col.type.length]
                
            new_row[col.name] = val
        safe_rows.append(new_row)
        
    return safe_rows

# ==================================================
# INSERT
# ==================================================
def insert_rows(
    engine: Engine,
    table_name: str,
    schema: Optional[str] = None,
    rows: List[Dict] = None,
    max_retries: int = 3,
    ignore_conflicts: bool = False
):
    if not rows:
        logger.debug("Nenhuma linha fornecida para inserção. Pulando.")
        return

    logger.info(f"📤 Inserindo {len(rows)} linhas em {schema or 'default'}.{table_name}")

    key = f"{engine.url}_{schema or 'default'}_{table_name}"
    if key not in _TABLE_CACHE:
        logger.debug(f"Tabela {table_name} não está no cache. Refletindo meta dados...")
        _TABLE_CACHE[key] = Table(
            table_name,
            MetaData(),
            autoload_with=engine,
            schema=schema
        )

    table = _TABLE_CACHE[key]
    db_type = engine.dialect.name

    for attempt in range(max_retries):
        logger.debug(f"Tentativa de inserção {attempt + 1}/{max_retries}...")
        try:
            safe_rows = _sanitize_row_data(table, rows)
            
            with engine.begin() as conn:
                if ignore_conflicts:
                    if db_type == "postgresql":
                        stmt = pg_insert(table).values(safe_rows).on_conflict_do_nothing()
                    elif db_type == "mysql":
                        stmt = mysql_insert(table).values(safe_rows).on_duplicate_key_update(
                            **{c.name: c for c in mysql_insert(table).inserted}
                        )
                    elif db_type == "sqlite":
                        stmt = sqlite_insert(table).values(safe_rows).on_conflict_do_nothing()
                    else:
                        stmt = table.insert().values(safe_rows)
                else:
                    stmt = table.insert().values(safe_rows)

                logger.debug("Executando statement de insert no banco...")
                conn.execute(stmt)

            logger.info(f"✅ Inserção concluída com sucesso em {table_name}.")
            return

        except sa.exc.IntegrityError as e:
            if ignore_conflicts:
                logger.warning(f"⚠️ Conflito ignorado em {table_name}. Erro: {e}")
                return
                
            logger.exception(f"⚠️ Erro de Integridade na tentativa {attempt + 1} em {table_name}")
            
            # Se for a última tentativa, interrompe o pipeline e não deixa o erro passar silencioso
            if attempt == max_retries - 1:
                logger.error(f"❌ Falha definitiva de integridade em {table_name}.")
                raise e 
            time.sleep(1)

        except Exception as e:
            logger.exception(f"⚠️ Erro desconhecido na tentativa {attempt + 1} em {table_name}")
            if rows:
                logger.warning(f"🔎 Linha exemplo que causou erro: {str(rows[0])[:500]}")
                
            # Se for a última tentativa, levanta a exceção para que o orquestrador saiba que o lote falhou
            if attempt == max_retries - 1:
                logger.error(f"❌ Falha definitiva ao inserir {len(rows)} linhas em {table_name} após {max_retries} tentativas.")
                raise e 
            time.sleep(1)

# ==================================================
# TRUNCATE
# ==================================================
def truncate_table(engine: Engine, table_name: str, schema: Optional[str] = None):
    logger.info(f"🧹 Iniciando TRUNCATE para a tabela {schema or 'default'}.{table_name}...")

    if not table_exists(engine, schema, table_name):
        logger.warning(f"⚠️ Tabela {table_name} não existe para fazer TRUNCATE. Pulando.")
        return

    table_ref = format_table_name(engine, schema, table_name)

    with engine.begin() as conn:
        try:
            if get_db_type(engine) == "postgresql":
                logger.debug(f"Executando TRUNCATE CASCADE no PostgreSQL...")
                conn.execute(text(f"TRUNCATE TABLE {table_ref} CASCADE"))
            else:
                logger.debug(f"Executando TRUNCATE padrão...")
                conn.execute(text(f"TRUNCATE TABLE {table_ref}"))
                
            logger.info(f"✅ TRUNCATE concluído em {table_ref}.")

        except Exception as e:
            logger.exception(f"⚠️ Falha no TRUNCATE de {table_ref}. Tentando fallback via DELETE...")
            try:
                conn.execute(text(f"DELETE FROM {table_ref}"))
                logger.info(f"✅ Fallback (DELETE FROM) executado com sucesso em {table_ref}.")
            except Exception as delete_e:
                logger.exception(f"❌ Falha definitiva no DELETE de {table_ref}")

# ==================================================
# DEPENDÊNCIAS
# ==================================================
def build_dependency_graph(engine: Engine, tables: List[str], schema: Optional[str] = None) -> List[str]:
    logger.info(f"🏗️ Construindo grafo de dependências para {len(tables)} tabelas...")
    insp = inspect(engine)
    deps = defaultdict(set)
    in_degree = {t: 0 for t in tables}

    for table in tables:
        logger.debug(f"Inspecionando FKs da tabela {table}...")
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

    final_order = ordered + [t for t in tables if t not in ordered]
    logger.debug(f"Ordem de dependência gerada: {final_order}")
    return final_order

# ==================================================
# REPLICATION MODE
# ==================================================
def set_replication_mode(engine: Engine, mode: str = "replica"):
    logger.info(f"⚙️ Configurando replication mode para '{mode}'...")
    
    if get_db_type(engine) != "postgresql":
        logger.debug("Banco não é PostgreSQL. Replication mode ignorado.")
        return

    try:
        with engine.begin() as conn:
            conn.execute(
                text("SET session_replication_role = :mode"),
                {"mode": mode}
            )
            logger.info(f"✅ session_replication_role alterado para: {mode}")

    except sa.exc.DBAPIError as e:
        error_msg = str(e).lower()
        if (
            "permission denied" in error_msg
            or "insufficientprivilege" in error_msg
            or "session_replication_role" in error_msg
        ):
            logger.warning("⚠️ Sem privilégio SUPERUSER. Continuando sem replication_role.")
        else:
            logger.exception("❌ Erro ao tentar aplicar session_replication_role.")
            raise