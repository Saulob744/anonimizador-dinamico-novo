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

logger = logging.getLogger(__name__)

# ==================================================
# RELOAD (DEV)
# ==================================================
importlib.reload(db_utils)
importlib.reload(anonymizer)

# ==================================================
# CONFIG UI
# ==================================================
st.set_page_config(
    page_title="🛡️ Aegis Anonymizer Pro",
    page_icon="🛡️",
    layout="wide"
)

st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.8rem; color: #00ffcc; }
    .stProgress .st-at { background-color: #00ffcc; }
    </style>
""", unsafe_allow_html=True)

# ==================================================
# HARD SCRUB (CAMADA FINAL DE SEGURANÇA)
# ==================================================
def hard_scrub(text: str) -> str:
    if not isinstance(text, str):
        return text

    pattern = re.compile(
        r"\b([A-ZÀ-Ü][a-zà-ü']+(?:\s+[A-ZÀ-Ü][a-zà-ü']+){1,3})\b"
    )

    def repl(m):
        name = m.group(1)
        return anonymizer._get_fake(name, "PER")

    return pattern.sub(repl, text)

# ==================================================
# HELPERS DB
# ==================================================
def get_sqlserver_driver():
    drivers = pyodbc.drivers()
    for d in reversed(drivers):
        if "SQL Server" in d:
            return d
    raise Exception("Nenhum driver ODBC encontrado")


def get_localdb_pipe(instance="MSSQLLocalDB"):
    try:
        result = subprocess.run(
            ["sqllocaldb", "info", instance],
            capture_output=True,
            text=True
        )
        for line in result.stdout.splitlines():
            if "Instance pipe name" in line:
                return line.split(":", 1)[1].strip()
    except:
        pass
    return None


def build_url(db_type, user, password, host, port, db):
    if db_type == "mssql":
        driver = get_sqlserver_driver()

        if host and "localdb" in host.lower():
            pipe = get_localdb_pipe()
            if not pipe:
                raise Exception("LocalDB não encontrado")

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
    st.session_state.stats = {
        "total_rows": 0
    }

# ==================================================
# SIDEBAR
# ==================================================
with st.sidebar:
    st.title("🛡️ Aegis Control")

    db_type = st.selectbox("Tipo do Banco", ["postgresql", "mysql", "mssql"])

    st.subheader("🔴 CONEXÃO")
    src_host = st.text_input("Host", "localhost")
    src_user = st.text_input("Usuário", "")
    src_pass = st.text_input("Senha", type="password")
    src_port = st.text_input("Porta", "5432")

    c1, c2 = st.columns(2)
    src_db = c1.text_input("Origem")
    dst_db = c2.text_input("Destino")

    st.divider()

    modo = st.selectbox(
        "Modo",
        ["🛡️ Anonimização Total", "⚡ Cópia Direta"]
    )

    chunk_size = st.number_input("Chunk Size", value=1000, step=500)
    filter_tables = st.text_input("Filtrar Tabelas (separadas por vírgula)")

    # 🚀 NOVO: BOTÃO DE CONTROLE DE LOCALIZAÇÃO AQUI NA SIDEBAR
    st.divider()
    st.subheader("⚙️ Configurações Especiais")
    anon_geo = st.toggle("Desfocar Localização (GPS)", value=True, help="Se ativo, reduz a precisão de coordenadas para proteger a residência exata.")

    st.divider()
    start_btn = st.button("🚀 INICIAR PIPELINE", use_container_width=True)

# ==================================================
# UI
# ==================================================
st.title("🛡️ Pipeline de Proteção de Dados")

progress_bar = st.progress(0)
status = st.empty()

# ==================================================
# PIPELINE
# ==================================================
def run_pipeline():
    process = psutil.Process(os.getpid())
    t0_global = time.time()

    anonymizer.fake.seed_instance(42)

    src_engine = db_utils.connect(
        build_url(db_type, src_user, src_pass, src_host, src_port, src_db)
    )

    dst_engine = db_utils.connect(
        build_url(db_type, src_user, src_pass, src_host, src_port, dst_db)
    )

    db_utils.set_replication_mode(dst_engine, "replica")

    schemas = db_utils.get_user_schemas(src_engine)
    allowed = [t.strip() for t in filter_tables.split(",")] if filter_tables else []

    for s in schemas:
        db_utils.copy_schema(src_engine, dst_engine, s)

    tables = []

    for s in schemas:
        raw = db_utils.get_tables(src_engine, s)
        filtered = [t for t in raw if not allowed or t in allowed]
        ordered = db_utils.build_dependency_graph(src_engine, filtered, s)
        tables += [(s, t) for t in ordered]

    # ============================
    # ESTIMATIVA TOTAL
    # ============================
    estimated = 0
    for s, t in tables:
        try:
            estimated += db_utils.get_table_count(src_engine, t, s)
        except:
            estimated += 1000

    total_rows = 0
    metric_placeholder = st.empty()

    status.info("🧹 Limpando tabelas de destino (Evitando quebra de Foreign Keys)...")
    for schema, table in reversed(tables):
        try:
            db_utils.truncate_table(dst_engine, table, schema)
        except Exception as e:
            logger.warning(f"Aviso ao limpar {schema}.{table}: {e}")

    # ============================
    # LOOP PRINCIPAL
    # ============================
    for i, (schema, table) in enumerate(tables):
        t0_table = time.time()
        status.info(f"📦 Processando: {schema}.{table}")

        for chunk in db_utils.fetch_rows_streaming(src_engine, table, schema, chunk_size):
            rows = [dict(r) for r in chunk]

            if modo == "🛡️ Anonimização Total":
                for r in rows:
                    for col in list(r.keys()):
                        old = r[col]
                        if old is None:
                            continue

                        if isinstance(old, (int, float, bool)) or type(old).__name__ in ['date', 'datetime', 'Timestamp']:
                            continue

                        try:
                            # 🚀 NOVO: PASSANDO O VALOR DO BOTÃO 'anon_geo' PARA O CÉREBRO
                            new, flag = anonymizer.anonymize_value(col, old, anon_location=anon_geo)
                            
                            if flag == "TEXT" and isinstance(new, str):
                                new = hard_scrub(new)

                            if new != old:
                                r[col] = new
                                st.session_state.stats["total_rows"] += 1
                        except Exception as e:
                            logger.error(f"Erro ao anonimizar coluna {col}: {e}")
                            continue

            try:
                db_utils.insert_rows(dst_engine, table, schema, rows)
            except Exception as e:
                status.warning(f"⚠️ FALHA NO INSERT {schema}.{table}: {e}")

            total_rows += len(rows)

            # ============================
            # 📊 MÉTRICAS EM TEMPO REAL
            # ============================
            elapsed = time.time() - t0_global
            speed = total_rows / elapsed if elapsed > 0 else 0
            progress = total_rows / max(estimated, 1)
            eta = (elapsed / progress - elapsed) if progress > 0 else 0
            ram = process.memory_info().rss / (1024 ** 2)

            metric_placeholder.markdown(f"""
            ### 📊 Métricas em tempo real
            - 🧮 Linhas processadas: **{total_rows:,} / {estimated:,}**
            - ⚡ Velocidade: **{speed:,.0f} linhas/s**
            - ⏱️ Tempo decorrido: **{elapsed:.1f}s**
            - ⏳ ETA: **{eta:.1f}s**
            - 💾 RAM: **{ram:.0f} MB**
            """)

            progress_bar.progress(min(progress, 1.0))

        t1_table = time.time()
        st.info(f"⏱️ {schema}.{table} concluída em {t1_table - t0_table:.1f}s")

    db_utils.set_replication_mode(dst_engine, "origin")
    total_time = time.time() - t0_global
    status.success(f"✅ FINALIZADO em {total_time:.2f}s 🚀")


# ==================================================
# EXEC
# ==================================================
if start_btn:
    try:
        run_pipeline()
        st.balloons()
    except Exception as e:
        status.error(f"❌ ERRO CRÍTICO: {e}")