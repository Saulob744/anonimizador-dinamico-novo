import streamlit as st
import sqlalchemy as sa
import pandas as pd
import time
import db_utils
import anonymizer
from datetime import datetime

# ========================
# CONFIG
# ========================
st.set_page_config(
    page_title="🛡️ Aegis Anonymizer Pro",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ========================
# CSS
# ========================
st.markdown("""
<style>
.stApp { background-color: #0f172a; color: #ffffff; }
section[data-testid="stSidebar"] {
    background-color: #1e293b !important;
    border-right: 2px solid #3b82f6;
}
.stTextInput input {
    background-color: #0f172a !important;
    color: #00ff00 !important;
    border: 1px solid #3b82f6 !important;
    font-family: monospace;
}
</style>
""", unsafe_allow_html=True)

# ========================
# HELPERS
# ========================
def build_url(user, password, host, port, db):
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"

#  CLASSIFICAÇÃO INTELIGENTE DE COLUNAS
def classify_columns(info: dict) -> dict:
    treatments = {}
    pks = info.get("primary_keys", [])

    for col in info["columns"]:
        name = col["name"]
        lname = name.lower()
        ctype = str(col["type"]).lower()

        if name in pks:
            treatments[name] = "SKIP"

        #  PRIORIDADE POR NOME
        elif any(x in lname for x in ["cpf", "documento", "doc", "rg"]):
            treatments[name] = "DOC"

        elif any(x in lname for x in ["id", "codigo", "cod"]):
            treatments[name] = "ID"

        # 🔥 NUMÉRICO REAL
        elif any(t in ctype for t in ["int", "bigint", "numeric", "double", "real"]):
            treatments[name] = "NUMERIC"

        elif any(t in ctype for t in ["date", "time", "timestamp", "bool"]):
            treatments[name] = "SKIP"

        else:
            treatments[name] = "TEXT"

    return treatments

# ========================
# STATE
# ========================
if 'stats' not in st.session_state:
    st.session_state.stats = {
        "PER": 0,
        "DOCS": 0,
        "CONTACTS": 0,
        "total_rows": 0
    }

if 'logs' not in st.session_state:
    st.session_state.logs = []

def add_log(msg):
    st.session_state.logs.append(
        f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    )

# ========================
# SIDEBAR
# ========================
with st.sidebar:
    st.title("🛡️ Aegis Control")

    st.subheader("🔴 ORIGEM")
    src_host = st.text_input("Host", "host.docker.internal")
    src_user = st.text_input("Usuário", "postgres")
    src_port = st.text_input("Porta", "5432")
    src_pass = st.text_input("Senha", type="password")
    src_db = st.text_input("Banco")

    st.subheader("🟢 DESTINO")
    dst_host = st.text_input("Host", "host.docker.internal", key="h2")
    dst_user = st.text_input("Usuário", "postgres", key="u2")
    dst_port = st.text_input("Porta", "5432", key="p2")
    dst_pass = st.text_input("Senha", type="password", key="s2")
    dst_db = st.text_input("Novo Banco", key="d2")

    st.subheader("⚙️ CONFIG")
    modo = st.selectbox("Modo", ["Anonimização", "Cópia"])
    chunk_size = st.number_input("Chunk", value=1000)

    btn = st.button("🚀 EXECUTAR")

# ========================
# UI
# ========================
m1, m2, m3, m4 = st.columns(4)
m1.metric("Linhas", st.session_state.stats['total_rows'])
m2.metric("Nomes", st.session_state.stats['PER'])
m3.metric("Docs", st.session_state.stats['DOCS'])
m4.metric("Contatos", st.session_state.stats['CONTACTS'])

log_area = st.empty()
progress = st.progress(0)

# ========================
# EXECUÇÃO
# ========================
if btn:
    try:
        add_log("Conectando...")

        src_engine = db_utils.connect(
            build_url(src_user, src_pass, src_host, src_port, src_db)
        )

        admin_url = build_url(dst_user, dst_pass, dst_host, dst_port, "postgres")
        db_utils.recreate_database_if_not_exists(admin_url, dst_db)

        dst_engine = db_utils.connect(
            build_url(dst_user, dst_pass, dst_host, dst_port, dst_db)
        )

        db_utils.set_replication_mode(dst_engine, 'replica')

        schemas = db_utils.get_user_schemas(src_engine)

        for schema in schemas:
            add_log(f"Schema: {schema}")

            tables = db_utils.build_dependency_graph(
                src_engine,
                db_utils.get_tables(src_engine, schema),
                schema
            )

            db_utils.copy_schema(src_engine, dst_engine, schema)

            for table in tables:
                add_log(f"Tabela: {table}")

                info = db_utils.get_table_info(src_engine, table, schema)
                treatments = classify_columns(info)

                db_utils.truncate_table(dst_engine, table, schema)

                total = src_engine.execute(
                    sa.text(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
                ).scalar()

                processed = 0

                for chunk in db_utils.fetch_rows_streaming(
                    src_engine, table, schema, chunk_size
                ):
                    rows = [dict(r) for r in chunk]

                    if modo == "Anonimização":
                        for r in rows:
                            for col, treat in treatments.items():

                                if r[col] is None or treat == "SKIP":
                                    continue

                                original = r[col]

                                new_val, cat = anonymizer.anonymize_value(
                                    col,
                                    r[col],
                                    col_type=treat
                                )

                                r[col] = new_val

                                if new_val != original and cat:
                                    st.session_state.stats[cat] += 1

                    db_utils.insert_rows(dst_engine, table, schema, rows)

                    processed += len(rows)
                    st.session_state.stats['total_rows'] += len(rows)

                    progress.progress(min(processed / total, 1.0))
                    log_area.code("\n".join(st.session_state.logs[-15:]))

        db_utils.set_replication_mode(dst_engine, 'origin')

        st.success("✅ Finalizado com sucesso!")
        st.balloons()

    except Exception as e:
        add_log(f"ERRO: {e}")
        st.error(str(e))