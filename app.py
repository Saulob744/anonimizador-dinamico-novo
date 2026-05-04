import streamlit as st
import logging
import warnings
import importlib
import urllib.parse
import pyodbc
import time
import re
import psutil
import os

from concurrent.futures import ProcessPoolExecutor

import sources
import db_utils
import anonymizer

# =========================================================
# CONFIG
# =========================================================

logging.getLogger(
    "streamlit.runtime.scriptrunner_utils.script_run_context"
).setLevel(logging.ERROR)

warnings.filterwarnings("ignore", category=UserWarning)

importlib.reload(db_utils)
importlib.reload(anonymizer)

st.set_page_config(
    page_title="Aegis Anonymizer",
    page_icon="🛡️",
    layout="wide"
)

# =========================================================
# STYLE
# =========================================================

st.markdown("""
<style>

.block-container{
    padding-top: 1.5rem;
}

/* ======================================================
   SIDEBAR
====================================================== */

[data-testid="stSidebar"]{
    background: #0f172a;
    border-right: 1px solid #1e293b;
}

[data-testid="stSidebar"] *{
    color: #f8fafc;
}

/* ======================================================
   BOTÕES
====================================================== */

.stButton>button{
    border-radius: 12px;
    height: 46px;
    border: none;
    font-weight: 600;
}

.stDownloadButton>button{
    border-radius: 12px;
    height: 46px;
    border: none;
    font-weight: 600;
    width: 100%;
}

/* ======================================================
   INPUTS / TEXTAREA
====================================================== */

textarea,
input{

    border-radius: 12px !important;

    background: white !important;

    color: black !important;
}

/* placeholder */

textarea::placeholder,
input::placeholder{
    color: #555 !important;
}

/* selectbox */

.stSelectbox div[data-baseweb="select"] > div{

    background: white !important;

    color: black !important;

    border-radius: 12px !important;
}

/* number input */

.stNumberInput input{
    background: white !important;
    color: black !important;
}

/* text area */

.stTextArea textarea{
    background: white !important;
    color: black !important;
}

</style>
""", unsafe_allow_html=True)

# =========================================================
# HELPERS
# =========================================================

def build_url(db_type, user, password, host, port, db):

    if db_type == "mssql":

        driver = [
            d for d in pyodbc.drivers()
            if "SQL Server" in d
        ][-1]

        odbc_str = (
            rf"DRIVER={{{driver}}};"
            rf"SERVER={host};"
            rf"DATABASE={db};"
            rf"UID={user};PWD={password};"
        )

        return (
            "mssql+pyodbc:///?odbc_connect="
            + urllib.parse.quote_plus(odbc_str)
        )

    prefix = (
        "postgresql+psycopg2"
        if db_type == "postgresql"
        else "mysql+pymysql"
    )

    return (
        f"{prefix}://"
        f"{urllib.parse.quote_plus(user)}:"
        f"{urllib.parse.quote_plus(password)}@"
        f"{host}:{port}/{db}"
    )

# =========================================================
# PROCESSAMENTO
# =========================================================

def process_chunk_parallel(rows, modo, anon_geo):

    if modo != "Anonimização Total" or not rows:
        return rows

    sub_scrub = re.compile(
        r"\b([A-ZÀ-Ü][a-zà-ü']+(?:\s+[A-ZÀ-Ü][a-zà-ü']+){1,3})\b"
    )

    processed = []

    sample_size = min(100, len(rows))
    cols = rows[0].keys()

    column_samples = {}

    for col in cols:

        vals = []

        for r in rows[:sample_size]:

            v = r.get(col)

            if (
                v is not None and
                type(v).__name__
                not in ['date', 'datetime', 'Timestamp', 'bool']
            ):
                vals.append(str(v))

        column_samples[col] = vals

    column_decisions = {
        col: anonymizer.should_anonymize_column(col, samples)
        for col, samples in column_samples.items()
    }

    for r in rows:

        row_dict = dict(r)

        for col, old in row_dict.items():

            if old is None:
                continue

            col_clean = str(col).lower()

            is_geo_col = any(
                x in col_clean
                for x in [
                    "lat",
                    "latitude",
                    "lon",
                    "long",
                    "longitude",
                    "coord",
                    "gps"
                ]
            )

            if isinstance(old, (int, float)) and not is_geo_col:
                continue

            if (
                not column_decisions.get(col, True)
                and not is_geo_col
            ):
                continue

            try:

                new, flag = anonymizer.anonymize_value(
                    col,
                    old,
                    anon_location=anon_geo
                )

                if (
                    flag == "TEXT"
                    and isinstance(new, str)
                    and not is_geo_col
                ):
                    new = sub_scrub.sub(
                        lambda m: anonymizer._get_fake(
                            m.group(1),
                            "PER"
                        ),
                        new
                    )

                row_dict[col] = new

            except:
                pass

        processed.append(row_dict)

    return processed

# =========================================================
# SIDEBAR
# =========================================================

with st.sidebar:

    st.markdown("## 🛡️ Aegis")

    operation_mode = st.radio(
        "Modo",
        [
            "🗄️ Banco",
            "📂 Arquivos",
            "📝 Texto"
        ]
    )

    st.markdown("---")

    if operation_mode == "🗄️ Banco":

        st.markdown("##### Banco de Dados")

        db_type = st.radio(
        "Banco de Dados",
        ["PostgreSQL", "MySQL", "MSSQL"],
        horizontal=True,
        label_visibility="visible"
    )

        tab_src, tab_dst = st.tabs(
            ["Origem", "Destino"]
        )

        with tab_src:

            src_cfg = {
                "host": st.text_input("Host", "localhost"),
                "port": st.text_input("Porta", "5432"),
                "db": st.text_input("Database"),
                "user": st.text_input("Usuário"),
                "password": st.text_input(
                    "Senha",
                    type="password"
                )
            }

        with tab_dst:

            dst_cfg = {
                "host": st.text_input("Host ", "localhost"),
                "port": st.text_input("Porta ", "5432"),
                "db": st.text_input("Database "),
                "user": st.text_input("Usuário "),
                "password": st.text_input(
                    "Senha ",
                    type="password"
                )
            }

    st.markdown("---")

    modo = st.selectbox(
        "Processamento",
        [
            "Anonimização Total",
            "Cópia Direta"
        ]
    )

    chunk_size = st.number_input(
        "Chunk Size",
        5000,
        50000,
        10000
    )

    anon_geo = st.toggle(
        "Anonimizar GPS",
        True
    )

    super_proc = st.toggle(
        "Multi-Core",
        False
    )

    n_cores = (
        st.slider(
            "Cores",
            1,
            os.cpu_count(),
            2
        )
        if super_proc else 1
    )

    if operation_mode == "🗄️ Banco":

        start_btn = st.button(
            "Iniciar Pipeline",
            use_container_width=True
        )

# =========================================================
# PIPELINE
# =========================================================

def run_pipeline():

    proc = psutil.Process(os.getpid())

    t0_global = time.time()

    status = st.empty()
    progress_bar = st.progress(0)
    metrics = st.empty()

    status.info("Conectando bancos...")

    src_engine = db_utils.connect(
        build_url(db_type, **src_cfg)
    )

    dst_engine = db_utils.connect(
        build_url(db_type, **dst_cfg)
    )

    db_utils.set_replication_mode(
        dst_engine,
        "replica"
    )

    status.info("Mapeando estruturas...")

    schemas = db_utils.get_user_schemas(src_engine)

    work_list = []
    total_estimated = 0

    for s in schemas:

        db_utils.copy_schema(
            src_engine,
            dst_engine,
            s
        )

        tables = db_utils.get_tables(src_engine, s)

        ordered = db_utils.build_dependency_graph(
            src_engine,
            tables,
            s
        )

        for t in ordered:

            count = db_utils.get_table_count(
                src_engine,
                t,
                s
            )

            if count > 0:

                work_list.append((s, t, count))
                total_estimated += count

    total_rows_processed = 0
    speed_samples = []

    with ProcessPoolExecutor(
        max_workers=n_cores
    ) as executor:

        for s, t, _ in work_list:

            db_utils.truncate_table(
                dst_engine,
                t,
                s
            )

            for chunk in db_utils.fetch_rows_streaming(
                src_engine,
                t,
                s,
                chunk_size
            ):

                t_start = time.time()

                rows = [dict(r) for r in chunk]

                if n_cores > 1 and len(rows) > 100:

                    sub_sz = max(
                        1,
                        len(rows) // n_cores
                    )

                    futures = [
                        executor.submit(
                            process_chunk_parallel,
                            rows[i:i+sub_sz],
                            modo,
                            anon_geo
                        )
                        for i in range(
                            0,
                            len(rows),
                            sub_sz
                        )
                    ]

                    rows = []

                    for f in futures:
                        rows.extend(f.result())

                else:

                    rows = process_chunk_parallel(
                        rows,
                        modo,
                        anon_geo
                    )

                db_utils.insert_rows(
                    dst_engine,
                    t,
                    s,
                    rows
                )

                total_rows_processed += len(rows)

                elapsed = time.time() - t_start

                speed = len(rows) / max(elapsed, 0.001)

                speed_samples.append(speed)

                if len(speed_samples) > 20:
                    speed_samples.pop(0)

                avg_speed = (
                    sum(speed_samples)
                    / len(speed_samples)
                )

                remaining = (
                    total_estimated
                    - total_rows_processed
                )

                eta = (
                    remaining / avg_speed
                    if avg_speed > 0 else 0
                )

                progress = min(
                    total_rows_processed
                    / total_estimated,
                    1.0
                )

                progress_bar.progress(progress)

                metrics.markdown(f"""
### 📊 Pipeline

| Métrica | Valor |
|---|---|
| Tabela | `{s}.{t}` |
| Velocidade | `{avg_speed:,.0f} linhas/s` |
| ETA | `{time.strftime("%Hh %Mm %Ss", time.gmtime(eta))}` |
| Progresso | `{progress*100:.1f}%` |
| RAM | `{proc.memory_info().rss / 1024**2:.0f} MB` |

""")

    db_utils.set_replication_mode(
        dst_engine,
        "origin"
    )

    st.success(
        f"Pipeline finalizado • "
        f"{total_rows_processed:,} linhas "
        f"em {time.time()-t0_global:.1f}s"
    )

# =========================================================
# MODO ARQUIVOS
# =========================================================

if operation_mode == "📂 Arquivos":

    st.markdown("# 📂 Arquivos")

    uploaded_csv = st.file_uploader(
        "Selecione um CSV",
        type=["csv"]
    )

    if uploaded_csv:

        input_path = uploaded_csv.name
        output_path = f"anon_{uploaded_csv.name}"

        with open(input_path, "wb") as f:
            f.write(uploaded_csv.getbuffer())

        if st.button(
            "Anonimizar CSV",
            use_container_width=True
        ):

            progress = st.progress(0)
            status = st.empty()

            source = sources.load_source(
                input_path,
                chunk_size=chunk_size
            )

            first_chunk = True

            for idx, chunk in enumerate(source):

                processed = process_chunk_parallel(
                    chunk,
                    modo,
                    anon_geo
                )

                sources.write_csv_streaming(
                    processed,
                    output_path,
                    first_chunk
                )

                first_chunk = False

                progress.progress(
                    min((idx + 1) / 20, 1.0)
                )

                status.info(
                    f"Chunk {idx+1} processado"
                )

            status.success("CSV anonimizado!")

            with open(output_path, "rb") as f:

                st.download_button(
                    "Download CSV",
                    f,
                    file_name=output_path,
                    mime="text/csv"
                )

# =========================================================
# MODO TEXO
# =========================================================

elif operation_mode == "📝 Texto":

    st.markdown("# 📝 Texto")

    input_text = st.text_area(
        "Cole um texto",
        height=300,
        placeholder="Cole relatórios, documentos, logs..."
    )

    col1, col2 = st.columns(2)

    with col1:

        anonymize_btn = st.button(
            "Anonimizar",
            use_container_width=True
        )

    with col2:

        clear_btn = st.button(
            "Limpar",
            use_container_width=True
        )

    if clear_btn:
        st.session_state["anon_text"] = ""

    if anonymize_btn:

        st.session_state["anon_text"] = (
            anonymizer.anonymize_text(
                input_text,
                anon_loc=anon_geo
            )
        )

    st.text_area(
        "Resultado",
        value=st.session_state.get(
            "anon_text",
            ""
        ),
        height=320
    )

# =========================================================
# EXECUÇÃO
# =========================================================

if operation_mode == "🗄️ Banco" and start_btn:
    run_pipeline()