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
# LOGS E CACHE
# ==================================================
logger = logging.getLogger(__name__)
_TABLE_CACHE: Dict[str, Table] = {}

# ==================================================
# ENCODING / SANITIZAÇÃO (BLINDADO)
# ==================================================
def safe_decode(value: Any) -> Any:
    """
    Corrige problemas de encoding e limpa caracteres assassinos de DBs (ex: Null Bytes).
    """
    if value is None:
        return None
        
    if isinstance(value, bytes):
        try:
            # Tenta o padrão ouro
            return value.decode("utf-8").replace('\x00', '')
        except UnicodeDecodeError:
            logger.debug("Falha ao decodificar UTF-8 (bytes), tentando latin1.")
            try:
                return value.decode("latin1").replace('\x00', '')
            except Exception:
                logger.debug("Falha ao decodificar latin1, usando replace de emergência.")
                return value.decode("utf-8", errors="replace").replace('\x00', '')

    if isinstance(value, str):
        # Remoção de Null Bytes (\x00) é crítica pois o PostgreSQL rejeita a string inteira
        clean_val = value.replace('\x00', '')
        try:
            clean_val.encode("utf-8")
            return clean_val
        except UnicodeEncodeError:
            logger.debug("Falha de encoding na string, aplicando replace.")
            return clean_val.encode("utf-8", errors="replace").decode("utf-8")

    return value

# ==================================================
# PERFORMANCE E CPU
# ==================================================
def get_cpu_info() -> int:
    cores = os.cpu_count() or 1
    logger.debug(f"CPU info: detectados {cores} cores.")
    return cores

def calculate_safe_workers(requested_cores: Optional[int] = None) -> int:
    total = get_cpu_info()
    if requested_cores:
        safe_workers = min(max(1, requested_cores), total)
    else:
        safe_workers = max(1, int(total * 0.75))
    
    logger.debug(f"Workers calculados: {safe_workers} (Requisitado: {requested_cores}, Total CPU: {total})")
    return safe_workers

# ==================================================
# CONEXÃO E BANCO
# ==================================================
def connect(url: str) -> Engine:
    parsed = make_url(url)
    # Mascara a senha no log por segurança
    safe_log_url = repr(parsed).replace(parsed.password, '***') if parsed.password else repr(parsed)
    logger.info(f"🔌 Iniciando conexão com o banco: {parsed.host} / {parsed.database}")
    logger.debug(f"URL de Conexão (Mascarada): {safe_log_url}")

    if "odbc_connect" in url:
        logger.warning("⚠️ ODBC detectado → usando conexão direta")
        return create_engine(url, pool_pre_ping=True, pool_recycle=3600, future=True)

    if not (db_name := parsed.database):
        logger.error("❌ Falha na conexão: URL fornecida não contém um banco de dados.")
        raise ValueError("URL fornecida não contém um banco de dados.")

    backend = parsed.get_backend_name()
    logger.debug(f"Backend detectado: {backend}")

    admin_db = (
        "postgres" if backend == "postgresql" 
        else "master" if backend == "mssql" 
        else None
    )
    
    # --- CRIAÇÃO DE DATABASE AUTOMÁTICA ---
    if admin_db:
        logger.info(f"Verificando existência do banco '{db_name}' através do admin_db '{admin_db}'")
        server_url = parsed.set(database=admin_db)
        try:
            engine_server = create_engine(server_url, isolation_level="AUTOCOMMIT", future=True)
            create_queries = {
                "postgresql": f'CREATE DATABASE "{db_name}"',
                "mysql": f"CREATE DATABASE IF NOT EXISTS `{db_name}`",
                "mssql": f"IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = '{db_name}') EXEC('CREATE DATABASE [{db_name}]')"
            }
            
            with engine_server.connect() as conn:
                if backend == "postgresql":
                    exists = conn.execute(
                        text("SELECT 1 FROM pg_database WHERE datname = :name"), 
                        {"name": db_name}
                    ).scalar()

                    if not exists:
                        logger.info(f"⚙️ Banco '{db_name}' não existe. Tentando criar...")
                        conn.execute(text(create_queries[backend]))
                        logger.info(f"✅ Banco '{db_name}' criado com sucesso.")
                    else:
                        logger.debug(f"✅ Banco '{db_name}' já existe.")
                        
                elif backend in create_queries:
                    logger.debug(f"⚙️ Executando verificação/criação ({backend})...")
                    conn.execute(text(create_queries[backend]))
                    logger.info(f"✅ Verificação/Criação do banco '{db_name}' concluída.")

        except sa.exc.OperationalError as e:
            logger.warning(f"⚠️ Sem acesso de rede ao banco admin para criar '{db_name}'. O banco destino precisa existir. Erro: {e}")
        except sa.exc.ProgrammingError as e:
            logger.warning(f"⚠️ Sem permissão CREATE DATABASE para '{db_name}'. Assumindo que já existe. Erro: {e}")
        except Exception as e:
            logger.exception(f"❌ Erro inesperado ao interagir com DB Admin: {e}")
        finally:
            if "engine_server" in locals():
                engine_server.dispose()
                logger.debug("Engine server (admin) liberada.")

    # --- ENGINE PRINCIPAL ---
    connect_args = {}
    if backend == "postgresql":
        connect_args["client_encoding"] = "utf8"
        
    logger.debug("Construindo engine principal da aplicação...")
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True,
        connect_args=connect_args
    )
    logger.info("✅ Engine principal conectada com sucesso.")
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
    return exists

def get_tables(engine: Engine, schema: Optional[str] = None) -> List[str]:
    logger.debug(f"Listando tabelas do schema '{schema or 'default'}'...")
    tables = sorted(inspect(engine).get_table_names(schema=schema))
    logger.debug(f"Tabelas encontradas: {len(tables)}")
    return tables

def get_table_info(engine: Engine, table: str, schema: Optional[str] = None) -> Dict[str, Any]:
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
            logger.debug(f"COUNT obtido: {count}")
            return count
    except Exception as e:
        logger.warning(f"⚠️ Erro ao calcular COUNT. Retornando fallback arbitrário (10000). Erro: {e}")
        return 10000

def get_user_schemas(engine: Engine) -> List[str]:
    logger.debug("Buscando schemas de usuário disponíveis...")
    db_type = get_db_type(engine)
    insp = inspect(engine)
    ignored = (
        {"information_schema", "pg_catalog", "pg_toast"}
        if db_type == "postgresql" else {"information_schema"}
    )
    schemas = sorted([s for s in insp.get_schema_names() if s not in ignored and not s.startswith("pg_")])
    logger.debug(f"Schemas validados: {schemas}")
    return schemas

# ==================================================
# SCHEMA
# ==================================================
def copy_schema(src_engine: Engine, dst_engine: Engine, schema: Optional[str] = None):
    logger.info(f"🔄 Iniciando cópia estrutural do schema '{schema or 'default'}'...")
    
    if schema and get_db_type(dst_engine) == "postgresql":
        try:
            with dst_engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        except Exception as e:
            logger.warning(f"⚠️ Sem permissão/Falha ao criar schema '{schema}'. Prosseguindo... Erro: {e}")
            
    meta = MetaData()
    logger.debug("Refletindo tabelas da origem...")
    with src_engine.connect() as conn:
        meta.reflect(bind=conn, schema=schema, resolve_fks=False)
        
    tables_to_copy = meta.sorted_tables
    logger.info(f"Tabelas mapeadas para replicação: {len(tables_to_copy)}")
    
    with dst_engine.begin() as conn:
        for table in tables_to_copy:
            try:
                table.schema = schema
                for col in table.columns:
                    col.server_default = None # Evita conflito de sequências/defaults dependentes
                table.create(bind=conn, checkfirst=True)
                logger.debug(f"Estrutura da tabela {table.name} criada/verificada.")
            except Exception as e:
                logger.exception(f"⚠️ Falha ao refletir estrutura da tabela {schema or 'default'}.{table.name}: {e}")
                
    logger.info("✅ Cópia de estrutura finalizada.")

# ==================================================
# STREAMING SEGURO
# ==================================================
def fetch_rows_streaming(
    engine: Engine,
    table: str,
    schema: Optional[str] = None,
    chunk_size: int = 1000,
    order_by: Optional[str] = None
) -> Generator[List[Dict[str, Any]], None, None]:
    
    logger.info(f"📥 Iniciando streaming via fetchmany na tabela {schema or 'default'}.{table} (Lotes de {chunk_size})")
    meta = MetaData()
    tbl = Table(table, meta, autoload_with=engine, schema=schema)
    stmt = sa.select(tbl)

    # Lógica de Ordenação Estrita
    if order_by:
        stmt = stmt.order_by(sa.text(order_by))
        logger.debug(f"Ordenação manual aplicada: {order_by}")
    else:
        pks = list(tbl.primary_key.columns)
        if pks:
            stmt = stmt.order_by(*pks)
            logger.debug(f"Ordenando pelas Chaves Primárias: {[pk.name for pk in pks]}")
        else:
            logger.warning(f"⚠️ Tabela {table} sem Primary Key. Tentando ordenação de contingência por colunas escalares.")
            fallback_cols = []
            for col in tbl.columns:
                # Ignora tipos complexos que crasham ordenação
                if not isinstance(col.type, (sa.LargeBinary, sa.JSON, sa.ARRAY)):
                   # Evita Postgres JSONB string representation bugs
                   if not str(col.type).upper().startswith("JSON"):
                       fallback_cols.append(col)
                   
            if fallback_cols:
                 stmt = stmt.order_by(*fallback_cols)
                 logger.debug(f"Contingência: Ordenando por {len(fallback_cols)} colunas compatíveis.")
            else:
                 logger.error(f"❌ Impossível ordenar a tabela {table} (Apenas tipos complexos). Lendo em ordem de disco natural (Pode causar duplicidade em interrupções).")

    with engine.connect() as conn:
        logger.debug("Disparando Query Streamada (Server-side cursor simulado)...")
        result = conn.execution_options(stream_results=True).execute(stmt)
        
        chunks_yielded, total_rows = 0, 0
        
        while rows := result.mappings().fetchmany(chunk_size):
            safe_rows = []
            for row in rows:
                fixed = {k: safe_decode(v) for k, v in row.items()}
                safe_rows.append(fixed)
            
            chunks_yielded += 1
            total_rows += len(safe_rows)
            logger.debug(f"Processado Chunk #{chunks_yielded} -> {len(safe_rows)} linhas (Total Acumulado: {total_rows}).")
            yield safe_rows
            
    logger.info(f"✅ Streaming total concluído para {table}. Registros: {total_rows}.")

# ==================================================
# SANITIZAÇÃO
# ==================================================
def _sanitize_row_data(table: Table, rows: List[Dict]) -> List[Dict]:
    """Garante que as strings não quebrem o limite de varchar da coluna do destino."""
    safe_rows = []
    for row in rows:
        new_row = {}
        for col in table.columns:
            val = row.get(col.name)
            
            # Checagem de Estouro de Limite (Evita DataError)
            if isinstance(val, str) and getattr(col.type, "length", None):
                if len(val) > col.type.length:
                    logger.debug(f"Aviso de Truncagem: Coluna '{col.name}' limite={col.type.length}, recebido={len(val)}. Cortando string.")
                    val = val[:col.type.length]
                    
            new_row[col.name] = val
        safe_rows.append(new_row)
        
    return safe_rows

# ==================================================
# INSERT RESILIENTE
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
        logger.debug("Lote vazio repassado para inserção. Ignorando.")
        return

    key = f"{engine.url}_{schema or 'default'}_{table_name}"
    
    # Gestão de Cache de MetaData para otimização
    if key not in _TABLE_CACHE:
        logger.debug(f"Mapeando metadata destino de {table_name} na memória...")
        _TABLE_CACHE[key] = Table(table_name, MetaData(), autoload_with=engine, schema=schema)

    table = _TABLE_CACHE[key]
    db_type = engine.dialect.name

    for attempt in range(max_retries):
        try:
            safe_rows = _sanitize_row_data(table, rows)
            if not safe_rows: return
            
            with engine.begin() as conn:
                # Estratégias de Conflito Baseada no Dialeto
                if ignore_conflicts:
                    if db_type == "postgresql":
                        stmt = pg_insert(table).values(safe_rows).on_conflict_do_nothing()
                    elif db_type == "mysql":
                        # MySQL On Duplicate exige saber o que atualizar, ou usar dummy update.
                        # Para emular DO NOTHING de forma segura com Bulk Insert:
                        insert_stmt = mysql_insert(table).values(safe_rows)
                        update_dict = {c.name: c for c in insert_stmt.inserted if c.name != table.primary_key.columns.keys()[0]} if table.primary_key else {c.name: c for c in insert_stmt.inserted}
                        if update_dict:
                            stmt = insert_stmt.on_duplicate_key_update(**update_dict)
                        else:
                            stmt = table.insert().values(safe_rows) # Fallback se não der pra mapear
                    elif db_type == "sqlite":
                        stmt = sqlite_insert(table).values(safe_rows).on_conflict_do_nothing()
                    else:
                        stmt = table.insert().values(safe_rows)
                else:
                    stmt = table.insert().values(safe_rows)

                conn.execute(stmt)

            logger.info(f"✅ Inserção de {len(rows)} linhas concluída com sucesso em {table_name}.")
            return

        except sa.exc.IntegrityError as e:
            if ignore_conflicts:
                logger.warning(f"⚠️ Conflito de integridade ignorado com sucesso em {table_name}.")
                return
            logger.exception(f"⚠️ Erro de Integridade [Tentativa {attempt + 1}/{max_retries}] em {table_name}")
            if attempt == max_retries - 1:
                raise e 
            time.sleep(1.5)

        except Exception as e:
            logger.exception(f"⚠️ Erro de execução [Tentativa {attempt + 1}/{max_retries}] em {table_name}")
            # Em caso de erro fatal, remove a tabela do cache pois a estrutura no DB pode ter sido alterada
            _TABLE_CACHE.pop(key, None) 
            
            if attempt == max_retries - 1:
                logger.error(f"❌ Falha crítica ao inserir dados após {max_retries} tentativas. Abortando lote.")
                raise e 
            time.sleep(1.5)

# ==================================================
# TRUNCATE
# ==================================================
def truncate_table(engine: Engine, table_name: str, schema: Optional[str] = None):
    logger.info(f"🧹 Disparando TRUNCATE para a tabela {schema or 'default'}.{table_name}...")

    if not table_exists(engine, schema, table_name):
        logger.warning(f"⚠️ Tabela {table_name} inexistente para TRUNCATE. Operação ignorada.")
        return

    table_ref = format_table_name(engine, schema, table_name)

    with engine.begin() as conn:
        try:
            if get_db_type(engine) == "postgresql":
                conn.execute(text(f"TRUNCATE TABLE {table_ref} CASCADE"))
                logger.debug("Executado TRUNCATE CASCADE exclusivo para Postgres.")
            else:
                conn.execute(text(f"TRUNCATE TABLE {table_ref}"))
                logger.debug("Executado TRUNCATE padrão ANSI.")
                
            logger.info(f"✅ Tabela {table_ref} truncada com sucesso.")

        except Exception as e:
            logger.warning(f"⚠️ TRUNCATE falhou para {table_ref}. Possível erro de FK. Tentando DELETE absoluto... Erro original: {e}")
            try:
                conn.execute(text(f"DELETE FROM {table_ref}"))
                logger.info(f"✅ Executado DELETE total como fallback em {table_ref}.")
            except Exception as delete_e:
                logger.exception(f"❌ Impossível limpar a tabela {table_ref} (Nem TRUNCATE nem DELETE funcionaram).")

# ==================================================
# DEPENDÊNCIAS DE INTEGRIDADE (GRAFO TOPOLÓGICO)
# ==================================================
def build_dependency_graph(engine: Engine, tables: List[str], schema: Optional[str] = None) -> List[str]:
    logger.info(f"🏗️ Mapeando grafo topológico de dependências entre {len(tables)} tabelas...")
    insp = inspect(engine)
    deps = defaultdict(set)
    in_degree = {t: 0 for t in tables}

    for table in tables:
        for fk in insp.get_foreign_keys(table, schema=schema):
            ref = fk.get("referred_table")
            if ref in tables and ref != table: # Evita auto-referência direta no grau
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

    # Concatena tabelas que caíram em referência circular / isoladas na base
    final_order = ordered + [t for t in tables if t not in ordered]
    logger.debug(f"Grafo de Inserção gerado: {' -> '.join(final_order)}")
    return final_order

# ==================================================
# REPLICATION ROLE
# ==================================================
def set_replication_mode(engine: Engine, mode: str = "replica"):
    logger.info(f"⚙️ Ajustando 'session_replication_role' para '{mode}'...")
    
    if get_db_type(engine) != "postgresql":
        logger.debug("Dialeto não é Postgres. Ação ignorada com sucesso.")
        return

    try:
        with engine.begin() as conn:
            conn.execute(
                text("SET session_replication_role = :mode"),
                {"mode": mode}
            )
            logger.info(f"✅ Replication Role validada no estado: {mode.upper()}")

    except sa.exc.DBAPIError as e:
        error_msg = str(e).lower()
        if any(term in error_msg for term in ["permission denied", "insufficientprivilege", "session_replication_role"]):
            logger.warning("⚠️ Usuário do Banco não possui privilégio SUPERUSER para desativar triggers/FKs temporalmente. O pipeline prosseguirá normalmente, mas gatilhos serão acionados.")
        else:
            logger.exception("❌ Erro sistêmico ao definir o role de replicação.")
            raise