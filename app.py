import streamlit as st
import db_utils
import anonymizer
import importlib
import urllib.parse
import pyodbc
import subprocess
import time
import re

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

    # captura nomes completos comuns (2-4 tokens capitalizados)
    pattern = re.compile(
        r"\b([A-ZÀ-Ü][a-zà-ü']+(?:\s+[A-ZÀ-Ü][a-zà-ü']+){1,3})\b"
    )

    def repl(m):
        name = m.group(1)
        return anonymizer._get(name, "PER", lambda: anonymizer.fake.name().upper())

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
        "PER": 0,
        "CPF": 0,
        "RG": 0,
        "PHONE": 0,
        "EMAIL": 0,
        "TEXT": 0,
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

    filter_tables = st.text_input("Filtrar Tabelas")

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

    anonymizer._memory.clear()
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

    estimated = 0
    for s, t in tables:
        try:
            estimated += db_utils.get_table_count(src_engine, t, s)
        except:
            estimated += 1000

    total_rows = 0

    for i, (schema, table) in enumerate(tables):

        status.info(f"📦 {schema}.{table}")

        try:
            db_utils.truncate_table(dst_engine, table, schema)
        except:
            pass

        for chunk in db_utils.fetch_rows_streaming(src_engine, table, schema, chunk_size):

            rows = [dict(r) for r in chunk]

            # ==================================================
            # 🔥 ANONIMIZAÇÃO GLOBAL (SEM DEPENDER DE COLUNA)
            # ==================================================
            if modo == "🛡️ Anonimização Total":

                for r in rows:

                    for col in r.keys():

                        if r[col] is None:
                            continue

                        try:
                            old = r[col]

                            # PASSO 1: IA + regex
                            new = anonymizer.anonymize_text(str(old))

                            # PASSO 2: HARD SCRUB (corrige vazamentos)
                            new = hard_scrub(new)

                            r[col] = new

                            if new != old:
                                st.session_state.stats["total_rows"] += 1

                        except:
                            continue

            # INSERT
            try:
                db_utils.insert_rows(dst_engine, table, schema, rows)
            except Exception as e:
                status.warning(f"⚠️ INSERT FAIL {schema}.{table}: {e}")

            total_rows += len(rows)

            progress_bar.progress(min(total_rows / max(estimated, 1), 1.0))

    db_utils.set_replication_mode(dst_engine, "origin")

    status.success("✅ FINALIZADO COM SEGURANÇA REFORÇADA")


# ==================================================
# EXEC
# ==================================================
if start_btn:
    try:
        run_pipeline()
        st.balloons()
    except Exception as e:
        status.error(f"❌ ERRO: {e}")