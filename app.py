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
        "PER": 0,
        "CPF": 0,
        "RG": 0,
        "PHONE": 0,
        "EMAIL": 0,
        "CODE": 0,
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

    col_db1, col_db2 = st.columns(2)
    src_db = col_db1.text_input("Origem")
    dst_db = col_db2.text_input("Destino")

    st.divider()

    modo = st.selectbox("Modo", ["🛡️ Anonimização Total", "⚡ Cópia Direta"])
    chunk_size = st.number_input("Chunk Size", value=1000, step=500)

    filter_tables = st.text_input("Filtrar Tabelas")

    btn_iniciar = st.button("🚀 INICIAR PIPELINE", use_container_width=True, type="primary")

# ==================================================
# DASHBOARD FIXO
# ==================================================
st.title("🛡️ Pipeline de Proteção de Dados")

top_metrics = st.container()
progress_container = st.container()
log_container = st.container()

with top_metrics:
    m1, m2, m3, m4, m5 = st.columns(5)
    metric_total = m1.empty()
    metric_per = m2.empty()
    metric_doc = m3.empty()
    metric_cont = m4.empty()
    metric_eta = m5.empty()

with progress_container:
    progress_bar = st.progress(0)
    progress_info = st.empty()
    speed_info = st.empty()
    table_info = st.empty()

with log_container:
    status = st.empty()

# ==================================================
# EXECUÇÃO
# ==================================================
if btn_iniciar:
    anonymizer._memory.clear()

    if not src_db or not dst_db:
        st.error("❌ Informe os bancos")
        st.stop()

    try:
        start_time = time.time()
        last_update = time.time()

        total_rows_processed = 0
        total_rows_estimated = 0

        src_engine = db_utils.connect(build_url(db_type, src_user, src_pass, src_host, src_port, src_db))
        dst_engine = db_utils.connect(build_url(db_type, src_user, src_pass, src_host, src_port, dst_db))

        db_utils.set_replication_mode(dst_engine, "replica")

        schemas = db_utils.get_user_schemas(src_engine)
        allowed_list = [t.strip() for t in filter_tables.split(",")] if filter_tables else []

        tables_pipeline = []

        for s in schemas:
            raw = db_utils.get_tables(src_engine, s)
            filtered = [t for t in raw if not allowed_list or t in allowed_list]
            ordered = db_utils.build_dependency_graph(src_engine, filtered, s)

            for t in ordered:
                tables_pipeline.append((s, t))

        total_tables = len(tables_pipeline)

        status.info("🔎 Estimando volume...")

        for schema, table in tables_pipeline:
            try:
                total_rows_estimated += db_utils.get_table_count(src_engine, table, schema)
            except:
                total_rows_estimated += 1000

        for s in schemas:
            db_utils.copy_schema(src_engine, dst_engine, s)

        for idx, (schema, table) in enumerate(tables_pipeline):

            table_info.info(f"📦 {idx+1}/{total_tables} → {schema}.{table}")

            info = db_utils.get_table_info(src_engine, table, schema)
            treatments = classify_columns(info)

            db_utils.truncate_table(dst_engine, table, schema)

            for chunk in db_utils.fetch_rows_streaming(src_engine, table, schema, chunk_size):

                rows = [dict(r) for r in chunk]
                total_rows_processed += len(rows)

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
                                if cat in st.session_state.stats:
                                    st.session_state.stats[cat] += 1

                db_utils.insert_rows(dst_engine, table, schema, rows)

                now = time.time()

                if now - last_update > 0.3:
                    elapsed = now - start_time
                    speed = total_rows_processed / elapsed if elapsed > 0 else 0

                    progress_value = (
                        total_rows_processed / total_rows_estimated
                        if total_rows_estimated > 0 else 0
                    )

                    progress_bar.progress(min(progress_value, 1.0))

                    remaining_sec = (
                        (total_rows_estimated - total_rows_processed) / speed
                        if speed > 0 else 0
                    )

                    eta = str(timedelta(seconds=int(remaining_sec)))

                    metric_total.metric("Alterações", f"{st.session_state.stats['total_rows']:,}")
                    metric_per.metric("Pessoas", st.session_state.stats["PER"])
                    metric_doc.metric("Docs", st.session_state.stats["CPF"] + st.session_state.stats["RG"])
                    metric_cont.metric("Contatos", st.session_state.stats["PHONE"] + st.session_state.stats["EMAIL"])
                    metric_eta.metric("Tempo Rest.", eta)

                    progress_info.write(
                        f"📊 {total_rows_processed:,} / {total_rows_estimated:,} linhas"
                    )

                    speed_info.write(
                        f"⚡ {int(speed):,} linhas/s"
                    )

                    last_update = now

        db_utils.set_replication_mode(dst_engine, "origin")

        total_time = str(timedelta(seconds=int(time.time() - start_time)))

        status.success(f"✅ FINALIZADO em {total_time}")
        progress_bar.progress(1.0)
        st.balloons()

    except Exception as e:
        status.error(f"❌ Erro: {e}")
        try:
            db_utils.set_replication_mode(dst_engine, "origin")
        except:
            pass