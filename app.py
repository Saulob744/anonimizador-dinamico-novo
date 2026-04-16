import streamlit as st
import db_utils
import anonymizer
import importlib
import urllib.parse

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

# ==================================================
# HELPERS
# ==================================================
def build_url(user, password, host, port, db):
    safe_user = urllib.parse.quote_plus(user)
    safe_password = urllib.parse.quote_plus(password)
    return f"postgresql://{safe_user}:{safe_password}@{host}:{port}/{db}"


def classify_columns(info: dict) -> dict:
    treatments = {}
    pks = info.get("primary_keys", [])

    for col in info["columns"]:
        name = col["name"]
        ctype = str(col["type"]).lower()

        if name in pks:
            treatments[name] = "SKIP"
        elif any(t in ctype for t in ["int", "bigint", "numeric", "double", "real"]):
            treatments[name] = "NUMERIC"
        elif any(t in ctype for t in ["date", "time", "timestamp", "bool"]):
            treatments[name] = "SKIP"
        else:
            treatments[name] = "TEXT"

    return treatments


# ==================================================
# STATE
# ==================================================
if "stats" not in st.session_state:
    st.session_state.stats = {
        "PER": 0,
        "DOCS": 0,
        "CONTACTS": 0,
        "TEXT": 0,
        "total_rows": 0,
    }

# ==================================================
# SIDEBAR
# ==================================================
with st.sidebar:
    st.title("🛡️ Aegis Control")

    st.subheader("🔴 ORIGEM")
    src_host = st.text_input("Host", "localhost")
    src_user = st.text_input("Usuário", "postgres")
    src_pass = st.text_input("Senha", type="password")
    src_db = st.text_input("Banco Origem")

    st.divider()

    st.subheader("🟢 DESTINO")
    dst_db = st.text_input("Banco Destino")

    st.divider()

    modo = st.selectbox("Modo", ["🛡️ Anonimização Total", "⚡ Cópia Direta"])
    chunk_size = st.number_input("Chunk Size", value=1000, step=500)

    btn_iniciar = st.button("🚀 INICIAR", use_container_width=True)

# ==================================================
# DASHBOARD
# ==================================================
st.title("🛡️ Aegis Pipeline")

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Total", f"{st.session_state.stats['total_rows']:,}")
m2.metric("Pessoas", st.session_state.stats["PER"])
m3.metric("Docs", st.session_state.stats["DOCS"])
m4.metric("Contatos", st.session_state.stats["CONTACTS"])
m5.metric("Textos", st.session_state.stats["TEXT"])

status = st.empty()
progress = st.progress(0)

# ==================================================
# EXECUÇÃO
# ==================================================
if btn_iniciar:

    if src_db.strip() == dst_db.strip():
        st.error("❌ Banco origem e destino NÃO podem ser iguais")
        st.stop()

    try:
        src_url = build_url(src_user, src_pass, src_host, "5432", src_db)
        dst_url = build_url(src_user, src_pass, src_host, "5432", dst_db)
        admin_url = build_url(src_user, src_pass, src_host, "5432", "postgres")

        status.info("🔌 Conectando...")

        src_engine = db_utils.connect(src_url)
        db_utils.recreate_database_if_not_exists(admin_url, dst_db)
        dst_engine = db_utils.connect(dst_url)

        # 🔥 desativa FK
        db_utils.set_replication_mode(dst_engine, "replica")

        schemas = db_utils.get_user_schemas(src_engine)

        total_tables = sum(len(db_utils.get_tables(src_engine, s)) for s in schemas)
        processed_tables = 0

        for schema in schemas:
            status.warning(f"📂 Schema: {schema}")

            db_utils.copy_schema(src_engine, dst_engine, schema)

            raw_tables = db_utils.get_tables(src_engine, schema)
            tables = db_utils.build_dependency_graph(src_engine, raw_tables, schema)

            for table in tables:
                status.write(f"⚙️ {schema}.{table}")

                info = db_utils.get_table_info(src_engine, table, schema)
                treatments = classify_columns(info)

                db_utils.truncate_table(dst_engine, table, schema)

                for chunk in db_utils.fetch_rows_streaming(
                    src_engine, table, schema, chunk_size
                ):
                    rows = [dict(r) for r in chunk]

                    if "Anonimização" in modo:
                        for r in rows:
                            for col, treat in treatments.items():
                                if r[col] is None or treat == "SKIP":
                                    continue

                                val_orig = r[col]

                                res_val, cat = anonymizer.anonymize_value(
                                    col, val_orig, is_numeric=(treat == "NUMERIC")
                                )

                                r[col] = res_val

                                if res_val != val_orig:
                                    st.session_state.stats["total_rows"] += 1
                                    if cat in st.session_state.stats:
                                        st.session_state.stats[cat] += 1

                    db_utils.insert_rows(dst_engine, table, schema, rows)

                processed_tables += 1
                progress.progress(processed_tables / total_tables)

                st.toast(f"{table} OK", icon="✅")

            # 🔥 ajustar sequences
            db_utils.fix_sequences(dst_engine, schema)

        # 🔥 reativa FK
        db_utils.set_replication_mode(dst_engine, "origin")

        status.success("✅ FINALIZADO")
        st.balloons()

    except Exception as e:
        status.error(f"❌ Erro: {e}")

        try:
            db_utils.set_replication_mode(dst_engine, "origin")
        except Exception:
            pass