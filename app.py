import streamlit as st
import db_utils
import anonymizer
import importlib
import urllib.parse
import pyodbc
import subprocess
import time
import re
import psutil
import os
import logging
from sqlalchemy import text

logger = logging.getLogger(__name__)

# ==================================================
# RELOAD (DEV)
# ==================================================
importlib.reload(db_utils)
importlib.reload(anonymizer)

# ==================================================
# CONFIG UI & ESTILOS
# ==================================================
st.set_page_config(page_title="🛡️ Aegis Anonymizer Pro", page_icon="🛡️", layout="wide")

st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.8rem; color: #00ffcc; font-weight: bold; }
    .stProgress .st-at { background-color: #00ffcc; }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; background-color: transparent; padding-bottom: 5px; }
    .stTabs [data-baseweb="tab"] { border-radius: 6px 6px 0 0; padding: 8px 16px; background-color: #2e2e3e; color: #a0a0a0; border: 1px solid transparent; }
    .stTabs [aria-selected="true"] { background-color: #00ffcc !important; color: #000000 !important; font-weight: 900 !important; box-shadow: 0px 4px 10px rgba(0, 255, 204, 0.4); border-bottom: none; }
    </style>
""", unsafe_allow_html=True)

# ==================================================
# HARD SCRUB 
# ==================================================
SCRUB_PATTERN = re.compile(r"\b([A-ZÀ-Ü][a-zà-ü']+(?:\s+[A-ZÀ-Ü][a-zà-ü']+){1,3})\b")

def hard_scrub(texto: str) -> str:
    if not isinstance(texto, str): return texto
    return SCRUB_PATTERN.sub(lambda m: anonymizer._get_fake(m.group(1), "PER"), texto)

# ==================================================
# HELPERS DB
# ==================================================
def get_sqlserver_driver():
    for d in reversed(pyodbc.drivers()):
        if "SQL Server" in d: return d
    raise Exception("Nenhum driver ODBC encontrado")

def get_localdb_pipe(instance="MSSQLLocalDB"):
    try:
        res = subprocess.run(["sqllocaldb", "info", instance], capture_output=True, text=True)
        for line in res.stdout.splitlines():
            if "Instance pipe name" in line: return line.split(":", 1)[1].strip()
    except: pass
    return None

def build_url(db_type, user, password, host, port, db):
    if db_type == "mssql":
        driver = get_sqlserver_driver()
        if host and "localdb" in host.lower():
            pipe = get_localdb_pipe()
            if not pipe: raise Exception("LocalDB não encontrado")
            odbc = f"DRIVER={{{driver}}};SERVER={pipe};DATABASE={db};Trusted_Connection=yes;"
            return f"mssql+pyodbc:///?odbc_connect={urllib.parse.quote_plus(odbc)}"
        return f"mssql+pyodbc://@{host}:{port}/{db}?driver={urllib.parse.quote_plus(driver)}&trusted_connection=yes"

    if db_type == "postgresql":
        return f"postgresql+psycopg2://{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password)}@{host}:{port}/{db}"
    if db_type == "mysql":
        return f"mysql+pymysql://{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password)}@{host}:{port}/{db}"
    return None

# ==================================================
# STATE
# ==================================================
if "stats" not in st.session_state:
    st.session_state.stats = {"total_rows": 0}

# ==================================================
# SIDEBAR
# ==================================================
with st.sidebar:
    st.title("🛡️ Aegis Control")

    db_type = st.selectbox("Tipo do Banco", ["postgresql", "mysql", "mssql"])
    
    tab_src, tab_dst = st.tabs(["🔴 Base Origem", "🟢 Base Destino"])

    with tab_src:
        src_host = st.text_input("Host Origem", "localhost", key="src_host")
        src_port = st.text_input("Porta", "5432", key="src_port")
        src_db   = st.text_input("Database", key="src_db")
        src_user = st.text_input("Usuário", key="src_user")
        src_pass = st.text_input("Senha", type="password", key="src_pass")

    with tab_dst:
        dst_host = st.text_input("Host Destino", "localhost", key="dst_host")
        dst_port = st.text_input("Porta", "5432", key="dst_port")
        dst_db   = st.text_input("Database", key="dst_db")
        dst_user = st.text_input("Usuário", key="dst_user")
        dst_pass = st.text_input("Senha", type="password", key="dst_pass")

    st.divider()
    modo = st.selectbox("Modo de Execução", ["🛡️ Anonimização Total", "⚡ Cópia Direta"])

    c1, c2 = st.columns(2)
    chunk_size = c1.number_input("Chunk Size", value=1000, step=500)
    filter_tables = c2.text_input("Filtrar Tabelas", placeholder="tb_1, tb_2")

    st.divider()
    anon_geo = st.toggle("Desfocar Localização (GPS)", value=True)

    st.divider()
    start_btn = st.button("🚀 INICIAR PIPELINE", use_container_width=True)

# ==================================================
# UI PRINCIPAL
# ==================================================
st.title("🛡️ Pipeline de Proteção de Dados")
progress_bar = st.progress(0)
status = st.empty()

# ==================================================
# MOTOR DO PIPELINE
# ==================================================
def run_pipeline():
    process = psutil.Process(os.getpid())
    t0_global = time.time()

    if hasattr(anonymizer, 'reset_memory'):
        anonymizer.reset_memory()
    else:
        anonymizer.fake.seed_instance(42)

    src_engine = db_utils.connect(build_url(db_type, src_user, src_pass, src_host, src_port, src_db))
    dst_engine = db_utils.connect(build_url(db_type, dst_user, dst_pass, dst_host, dst_port, dst_db))
    db_utils.set_replication_mode(dst_engine, "replica")

    schemas = db_utils.get_user_schemas(src_engine)
    allowed = [t.strip() for t in filter_tables.split(",")] if filter_tables else []

    # Copia a estrutura das tabelas da Origem pro Destino
    for s in schemas:
        db_utils.copy_schema(src_engine, dst_engine, s)

    # Filtra as tabelas que serão PROCESSADAS (Origem)
    tables_to_process = []
    for s in schemas:
        raw = db_utils.get_tables(src_engine, s)
        filtered = [t for t in raw if not allowed or t in allowed]
        tables_to_process += [(s, t) for t in db_utils.build_dependency_graph(src_engine, filtered, s)]

    estimated = sum((db_utils.get_table_count(src_engine, t, s) for s, t in tables_to_process)) or 1000
    total_rows = 0
    metric_placeholder = st.empty()

    status.info("🧹 Preparando ambiente de destino (Limpando tabelas)...")
    for schema, table in reversed(tables_to_process):
        db_utils.truncate_table(dst_engine, table, schema)

    # LOOP DE PROCESSAMENTO E ANONIMIZAÇÃO
    for i, (schema, table) in enumerate(tables_to_process):
        t0_table = time.time()
        status.info(f"📦 Processando: {schema}.{table}")

        for chunk in db_utils.fetch_rows_streaming(src_engine, table, schema, chunk_size):
            rows = [dict(r) for r in chunk]

            if modo == "🛡️ Anonimização Total":
                for r in rows:
                    for col, old in list(r.items()):
                        if old is None or isinstance(old, (int, float, bool)) or type(old).__name__ in ['date', 'datetime', 'Timestamp']:
                            continue
                        try:
                            new, flag = anonymizer.anonymize_value(col, old, anon_location=anon_geo)
                            if flag == "TEXT" and isinstance(new, str):
                                new = hard_scrub(new)
                            if new != old:
                                r[col] = new
                                st.session_state.stats["total_rows"] += 1
                        except Exception as e:
                            logger.error(f"Erro ao anonimizar coluna {col}: {e}")

            try:
                db_utils.insert_rows(dst_engine, table, schema, rows)
            except Exception as e:
                status.warning(f"⚠️ FALHA NO INSERT {schema}.{table}: {e}")

            total_rows += len(rows)

            elapsed = time.time() - t0_global
            speed = total_rows / elapsed if elapsed > 0 else 0
            progress = total_rows / max(estimated, 1)
            metric_placeholder.markdown(f"""
            ### 📊 Métricas em tempo real
            - 🧮 Linhas processadas: **{total_rows:,} / {estimated:,}**
            - ⚡ Velocidade: **{speed:,.0f} linhas/s**
            - ⏱️ Tempo decorrido: **{elapsed:.1f}s**
            - 💾 RAM: **{process.memory_info().rss / (1024 ** 2):.0f} MB**
            """)
            progress_bar.progress(min(progress, 1.0))

        st.info(f"⏱️ {schema}.{table} concluída em {time.time() - t0_table:.1f}s")

    # ==================================================
    # 🧹 CLEANUP FINAL ABSOLUTO (LÊ DIRETAMENTE DO DESTINO)
    # ==================================================
    status.info("🗑️ Realizando varredura e removendo tabelas vazias no destino...")
    drop_count = 0
    
    # busca as tabelas DE FATO existentes no destino (incluindo as não filtradas/public)
    dst_schemas = db_utils.get_user_schemas(dst_engine)
    
    for s in dst_schemas:
        dst_tables = db_utils.get_tables(dst_engine, s)
        ordered_dst_tables = db_utils.build_dependency_graph(dst_engine, dst_tables, s)
        
        for table in reversed(ordered_dst_tables):
            # Conta no destino. Se for exatos 0, apaga.
            count = db_utils.get_table_count(dst_engine, table, s)
            if count == 0:
                try:
                    table_ref = db_utils.format_table_name(dst_engine, s, table)
                    with dst_engine.begin() as conn:
                        if db_type == "postgresql":
                            conn.execute(text(f"DROP TABLE {table_ref} CASCADE"))
                        else:
                            conn.execute(text(f"DROP TABLE {table_ref}"))
                    drop_count += 1
                except Exception as e:
                    logger.warning(f"Não foi possível remover {s}.{table}: {e}")

    db_utils.set_replication_mode(dst_engine, "origin")
    
    if drop_count > 0:
        st.toast(f"🧹 Limpeza concluída: {drop_count} tabelas vazias apagadas!")
        
    status.success(f"✅ FINALIZADO em {time.time() - t0_global:.2f}s 🚀")

# ==================================================
# EXECUÇÃO
# ==================================================
if start_btn:
    try:
        run_pipeline()
        st.balloons()
    except Exception as e:
        status.error(f"❌ ERRO CRÍTICO: {e}")