import streamlit as st
import db_utils
import anonymizer
import importlib
import urllib.parse
import pyodbc
import subprocess
import time
from datetime import timedelta

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
# HELPERS
# ==================================================
def get_sqlserver_driver():
    drivers = pyodbc.drivers()
    for d in reversed(drivers):
        if "SQL Server" in d:
            return d
    raise Exception("Nenhum driver ODBC encontrado")

def get_localdb_pipe(instance="MSSQLLocalDB"):
    try:
        result = subprocess.run(["sqllocaldb", "info", instance], capture_output=True, text=True)
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
            odbc_str = f"DRIVER={{{driver}}};SERVER={pipe};DATABASE={db};Trusted_Connection=yes;"
            return f"mssql+pyodbc:///?odbc_connect={urllib.parse.quote_plus(odbc_str)}"
        else:
            return f"mssql+pyodbc://@{host}:{port}/{db}?driver={urllib.parse.quote_plus(driver)}&trusted_connection=yes"

    elif db_type == "postgresql":
        return f"postgresql+psycopg2://{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password)}@{host}:{port}/{db}"

    elif db_type == "mysql":
        return f"mysql+pymysql://{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password)}@{host}:{port}/{db}"

    return None

def classify_columns(info: dict) -> dict:
    treatments = {}
    pks = info.get("primary_keys", [])

    for col in info["columns"]:
        name = col["name"]
        name_lower = name.lower()
        ctype = str(col["type"]).lower()

        if name in pks or name_lower.endswith("_id"):
            treatments[name] = "SKIP"
            continue

        if any(t in ctype for t in ["int", "integer", "bigint"]):
            treatments[name] = "SKIP"
            continue

        if any(t in ctype for t in ["numeric", "decimal", "float"]):
            treatments[name] = "NUMERIC"
            continue

        if any(t in ctype for t in ["date", "time", "timestamp", "bool"]):
            treatments[name] = "SKIP"
            continue

        sensitive_keywords = ["cpf", "rg", "email", "telefone", "nome"]
        if any(k in name_lower for k in sensitive_keywords):
            treatments[name] = "SENSITIVE"
        else:
            treatments[name] = "TEXT"

    return treatments

# ==================================================
# STATE
# ==================================================
if "stats" not in st.session_state:
    st.session_state.stats = {
        "PER": 0, "CPF": 0, "RG": 0,
        "PHONE": 0, "EMAIL": 0,
        "CODE": 0, "TEXT": 0,
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

    col_db1, col_db2 = st.columns(2)
    src_db = col_db1.text_input("Origem")
    dst_db = col_db2.text_input("Destino")

    st.divider()

    modo = st.selectbox("Modo", ["🛡️ Anonimização Total", "⚡ Cópia Direta"])
    chunk_size = st.number_input("Chunk Size", value=1000, step=500)

    filter_tables = st.text_input("Filtrar Tabelas")

    btn_iniciar = st.button("🚀 INICIAR PIPELINE", use_container_width=True, type="primary")

# ==================================================
# DASHBOARD
# ==================================================
st.title("🛡️ Pipeline de Proteção de Dados")

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Alterações", f"{st.session_state.stats['total_rows']:,}")
m2.metric("Pessoas", st.session_state.stats["PER"])
m3.metric("Documentos", st.session_state.stats["CPF"] + st.session_state.stats["RG"])
m4.metric("Contatos", st.session_state.stats["PHONE"] + st.session_state.stats["EMAIL"])
m5.metric("Tempo Est.", "00:00:00")

status = st.empty()
progress = st.progress(0)

# ==================================================
# EXECUÇÃO
# ==================================================
if btn_iniciar:
    if not src_db or not dst_db:
        st.error("❌ Informe os bancos")
        st.stop()

    try:
        start_time = time.time()

        src_engine = db_utils.connect(build_url(db_type, src_user, src_pass, src_host, src_port, src_db))
        dst_engine = db_utils.connect(build_url(db_type, src_user, src_pass, src_host, src_port, dst_db))

        # 🔥 DESLIGA FK GLOBAL
        db_utils.set_replication_mode(dst_engine, "replica")

        schemas = db_utils.get_user_schemas(src_engine)
        allowed_list = [t.strip() for t in filter_tables.split(",")] if filter_tables else []

        tables_pipeline = []

        # 🔥 MONTA PIPELINE ORDENADO POR GRAFO
        for s in schemas:
            raw = db_utils.get_tables(src_engine, s)
            filtered = [t for t in raw if not allowed_list or t in allowed_list]

            ordered = db_utils.build_dependency_graph(src_engine, filtered, s)

            for t in ordered:
                tables_pipeline.append((s, t))

        total_tables = len(tables_pipeline)

        # 🔥 COPIA SCHEMA UMA VEZ
        for s in schemas:
            db_utils.copy_schema(src_engine, dst_engine, s)

        # ==================================================
        # PROCESSAMENTO
        # ==================================================
        for idx, (schema, table) in enumerate(tables_pipeline):

            status.write(f"⚙️ {schema}.{table}")

            info = db_utils.get_table_info(src_engine, table, schema)
            treatments = classify_columns(info)

            db_utils.truncate_table(dst_engine, table, schema)

            for chunk in db_utils.fetch_rows_streaming(src_engine, table, schema, chunk_size):

                rows = [dict(r) for r in chunk]

                if "Anonimização" in modo:
                    for r in rows:
                        for col, treat in treatments.items():
                            if r[col] is None or treat == "SKIP":
                                continue

                            val_orig = r[col]

                            try:
                                res_val, cat = anonymizer.anonymize_value(col, val_orig)
                            except:
                                res_val, cat = val_orig, None

                            r[col] = res_val

                            if res_val != val_orig:
                                st.session_state.stats["total_rows"] += 1
                                if cat and cat in st.session_state.stats:
                                    st.session_state.stats[cat] += 1

                db_utils.insert_rows(dst_engine, table, schema, rows)

            # progresso
            progress.progress((idx + 1) / total_tables)

            # estimativa
            elapsed = time.time() - start_time
            est_total = (elapsed / (idx + 1)) * total_tables
            remaining = str(timedelta(seconds=int(est_total - elapsed)))
            m5.metric("Tempo Rest.", remaining)

        # 🔥 REATIVA FK (VALIDAÇÃO FINAL)
        db_utils.set_replication_mode(dst_engine, "origin")

        status.success(f"✅ FINALIZADO em {str(timedelta(seconds=int(time.time()-start_time)))}")
        st.balloons()

    except Exception as e:
        status.error(f"❌ Erro: {e}")
        try:
            db_utils.set_replication_mode(dst_engine, "origin")
        except:
            pass