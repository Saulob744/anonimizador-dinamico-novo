import streamlit as st
import db_utils
import anonymizer
import importlib
import urllib.parse
import pyodbc
import subprocess

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
# HELPERS MSSQL
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
    except Exception:
        pass

    return None


def create_db_localdb_if_not_exists(db_name):
    try:
        subprocess.run(
            [
                "sqlcmd",
                "-S", r"(localdb)\MSSQLLocalDB",
                "-Q", f"IF DB_ID('{db_name}') IS NULL CREATE DATABASE [{db_name}]"
            ],
            capture_output=True,
            text=True,
            check=True
        )
    except Exception as e:
        raise Exception(f"Erro ao criar banco no LocalDB: {e}")


# ==================================================
# BUILD URL
# ==================================================
def build_url(db_type, user, password, host, port, db):

    if db_type == "mssql":
        driver = get_sqlserver_driver()

        if host and "localdb" in host.lower():
            pipe = get_localdb_pipe()

            if not pipe:
                raise Exception("LocalDB não encontrado")

            odbc_str = (
                f"DRIVER={{{driver}}};"
                f"SERVER={pipe};"
                f"DATABASE={db};"
                "Trusted_Connection=yes;"
            )

            params = urllib.parse.quote_plus(odbc_str)
            return f"mssql+pyodbc:///?odbc_connect={params}"

        else:
            driver_encoded = urllib.parse.quote_plus(driver)

            return (
                f"mssql+pyodbc://@{host}:{port}/{db}"
                f"?driver={driver_encoded}&trusted_connection=yes"
            )

    elif db_type == "postgresql":
        safe_user = urllib.parse.quote_plus(user)
        safe_password = urllib.parse.quote_plus(password)

        # ✔ CORREÇÃO PRINCIPAL AQUI
        return f"postgresql+psycopg2://{safe_user}:{safe_password}@{host}:{port}/{db}"

    elif db_type == "mysql":
        safe_user = urllib.parse.quote_plus(user)
        safe_password = urllib.parse.quote_plus(password)
        return f"mysql+pymysql://{safe_user}:{safe_password}@{host}:{port}/{db}"

    else:
        raise ValueError(f"Banco não suportado: {db_type}")


# ==================================================
# CLASSIFICAÇÃO
# ==================================================
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

    db_type = st.selectbox(
        "Tipo do Banco",
        ["postgresql", "mysql", "mssql"]
    )

    st.subheader("🔴 ORIGEM")
    src_host = st.text_input("Host", "localhost")
    src_user = st.text_input("Usuário", "")
    src_pass = st.text_input("Senha", type="password")
    src_port = st.text_input("Porta", "5432")
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

    if not src_db or not dst_db:
        st.error("❌ Informe os bancos origem e destino")
        st.stop()

    if src_db.strip() == dst_db.strip():
        st.error("❌ Banco origem e destino NÃO podem ser iguais")
        st.stop()

    try:
        src_url = build_url(db_type, src_user, src_pass, src_host, src_port, src_db)
        dst_url = build_url(db_type, src_user, src_pass, src_host, src_port, dst_db)

        status.info("🔧 Preparando ambiente...")

        if db_type == "mssql" and src_host and "localdb" in src_host.lower():
            create_db_localdb_if_not_exists(dst_db)

        status.info("🔌 Conectando...")

        src_engine = db_utils.connect(src_url)
        dst_engine = db_utils.connect(dst_url)

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

                for chunk in db_utils.fetch_rows_streaming(src_engine, table, schema, chunk_size):
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

            db_utils.fix_sequences(dst_engine, schema)

        db_utils.set_replication_mode(dst_engine, "origin")

        status.success("✅ FINALIZADO")
        st.balloons()

    except Exception as e:
        status.error(f"❌ Erro: {e}")

        try:
            db_utils.set_replication_mode(dst_engine, "origin")
        except Exception:
            pass