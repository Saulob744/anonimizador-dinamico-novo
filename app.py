import streamlit as st
import logging
import warnings
import importlib
import urllib.parse
import pyodbc
import subprocess
import time
import re
import psutil
import os
from concurrent.futures import ProcessPoolExecutor

# ==================================================
# CONFIGURAÇÕES INICIAIS E SUPRESSÃO DE AVISOS
# ==================================================
logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", category=UserWarning, message=".*resume_download.*")

import db_utils
import anonymizer

importlib.reload(db_utils)
importlib.reload(anonymizer)

# ==================================================
# UI
# ==================================================
st.set_page_config(page_title="🛡️ Aegis Anonymizer Pro", page_icon="🛡️", layout="wide")

st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.8rem; color: #00ffcc; font-weight: bold; }
    .stProgress .st-at { background-color: #00ffcc; }
    </style>
""", unsafe_allow_html=True)

# ==================================================
# CORE
# ==================================================
# ==================================================
# CORE
# ==================================================
def process_chunk_parallel(rows, modo, anon_geo):
    import anonymizer
    import re

    sub_scrub = re.compile(
        r"\b([A-ZÀ-Ü][a-zà-ü']+(?:\s+[A-ZÀ-Ü][a-zà-ü']+){1,3})\b"
    )

    if modo != "🛡️ Anonimização Total":
        return rows

    processed = []

    # ==================================================
    # NOVO: cache por chunk para score de sensibilidade
    # Evita recalcular por linha
    # ==================================================
    column_samples = {}

    if rows:
        sample_size = min(50, len(rows))

        for col in rows[0].keys():
            vals = []

            for r in rows[:sample_size]:
                v = r.get(col)

                if (
                    v is not None
                    and not isinstance(v, (int, float, bool))
                    and type(v).__name__ not in ['date', 'datetime', 'Timestamp']
                ):
                    vals.append(str(v).strip())

            column_samples[col] = vals

    column_decisions = {}

    for col, samples in column_samples.items():
        try:
            column_decisions[col] = anonymizer.should_anonymize_column(
                col,
                samples
            )
        except Exception:
            column_decisions[col] = True

    # ==================================================
    # PROCESSAMENTO NORMAL
    # ==================================================
    for r in rows:
        row_dict = dict(r)

        for col, old in row_dict.items():

            # Ignora tipos seguros
            if old is None or isinstance(old, (int, float, bool)):
                continue

            if type(old).__name__ in ['date', 'datetime', 'Timestamp']:
                continue

            # ==================================================
            # NOVO: pula colunas classificadas como não sensíveis
            # ==================================================
            if not column_decisions.get(col, True):
                continue

            try:
                new, flag = anonymizer.anonymize_value(
                    col,
                    old,
                    anon_location=anon_geo
                )

                # ==================================================
                # Scrub secundário apenas se texto foi alterado
                # ==================================================
                if flag == "TEXT" and isinstance(new, str):
                    new = sub_scrub.sub(
                        lambda m: anonymizer._get_fake(
                            m.group(1),
                            "PER"
                        ),
                        new
                    )

                row_dict[col] = new

            except Exception as e:
                try:
                    anonymizer.debug_log(
                        f"[APP ERROR] Col={col} Value={str(old)[:80]} Error={e}"
                    )
                except Exception:
                    pass

        processed.append(row_dict)

    return processed


def build_url(db_type, user, password, host, port, db):
    if db_type == "mssql":
        driver = [d for d in pyodbc.drivers() if "SQL Server" in d][-1]
        if host and "localdb" in host.lower():
            odbc_str = rf"DRIVER={{{driver}}};SERVER=(localdb)\MSSQLLocalDB;DATABASE={db};Trusted_Connection=yes;"
            return f"mssql+pyodbc:///?odbc_connect={urllib.parse.quote_plus(odbc_str)}"

        return f"mssql+pyodbc://@{host}:{port}/{db}?driver={urllib.parse.quote_plus(driver)}&trusted_connection=yes"

    prefix = "postgresql+psycopg2" if db_type == "postgresql" else "mysql+pymysql"
    return f"{prefix}://{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password)}@{host}:{port}/{db}"


# ==================================================
# UI STATE
# ==================================================
with st.sidebar:
    st.title("🛡️ Aegis Control")
    db_type = st.selectbox("Tipo de Banco de Dados", ["postgresql", "mysql", "mssql"])

    aba_origem, aba_destino = st.tabs(["🔴 Banco Origem", "🟢 Banco Destino"])

    def render_db_form(prefix):
        return {
            "host": st.text_input("Host", value="localhost", key=f"{prefix}_host"),
            "port": st.text_input("Porta", key=f"{prefix}_port"),
            "db": st.text_input("Banco", key=f"{prefix}_db"),
            "user": st.text_input("Usuário", key=f"{prefix}_user"),
            "password": st.text_input("Senha", type="password", key=f"{prefix}_pass")
        }

    with aba_origem:
        src_cfg = render_db_form("origem")

    with aba_destino:
        dst_cfg = render_db_form("destino")

    modo = st.selectbox("Modo", ["🛡️ Anonimização Total", "⚡ Cópia Direta"])
    chunk_size = st.number_input("Chunk", value=10000, step=1000)
    filter_tables = st.text_input("Filtrar tabelas")
    anon_geo = st.toggle("GPS blur", value=True)

    super_proc = st.toggle("🚀 Multi CPU", value=False)
    n_cores = st.slider("CPU", 1, db_utils.get_cpu_info(), db_utils.get_cpu_info()) if super_proc else 1

    start_btn = st.button("INICIAR", use_container_width=True)


# ==================================================
# PIPELINE
# ==================================================
st.title("🛡️ Pipeline")
progress_bar = st.progress(0)
status = st.empty()
metric_placeholder = st.empty()


def run_pipeline():
    proc = psutil.Process(os.getpid())
    t0_global = time.time()

    phase_total = 4
    current_phase = 0

    def set_phase(title, subtitle=""):
        nonlocal current_phase
        current_phase += 1

        status.info(f"🔷 Fase {current_phase}/{phase_total} • {title}")

        metric_placeholder.markdown(f"""
        ### 📊 Pipeline em execução
        - 🔷 Fase: **{current_phase}/{phase_total}**
        - 🧭 Etapa: **{title}**
        - 🧾 Detalhe: {subtitle if subtitle else "processando..."}
        - ⏱️ Tempo: **{time.time() - t0_global:.1f}s**
        - 💾 RAM: **0 MB (inicializando)**
        """)

        # Reservar 25% para fases estruturais
        phase_progress = min((current_phase - 1) / phase_total * 0.25, 0.25)
        progress_bar.progress(phase_progress)

    # =========================
    # FASE 1
    # =========================
    set_phase("Conectando bancos de dados", "estabelecendo conexões")

    src_engine = db_utils.connect(build_url(db_type, **src_cfg))
    dst_engine = db_utils.connect(build_url(db_type, **dst_cfg))
    db_utils.set_replication_mode(dst_engine, "replica")

    # =========================
    # FASE 2
    # =========================
    set_phase("Mapeando estruturas", "schemas e tabelas")

    if modo == "🛡️ Anonimização Total":
        try:
            anonymizer.get_gliner()
            status.success("🧠 IA carregada com sucesso")
        except:
            status.warning("🧠 Modo fallback (regex ativo)")

    schemas = db_utils.get_user_schemas(src_engine)
    allowed = [t.strip() for t in filter_tables.split(",")] if filter_tables else []

    work_list = []
    total_estimated = 0
    total_tables = 0

    for s in schemas:
        db_utils.copy_schema(src_engine, dst_engine, s)

        tables = [
            t for t in db_utils.get_tables(src_engine, s)
            if not allowed or t in allowed
        ]

        ordered = db_utils.build_dependency_graph(src_engine, tables, s)

        for t in ordered:
            count = db_utils.get_table_count(src_engine, t, s)
            work_list.append((s, t, count))
            total_estimated += count
            total_tables += 1

    # =========================
    # FASE 3
    # =========================
    set_phase("Preparando destino", "limpeza de tabelas")

    for s, t, _ in reversed(work_list):
        db_utils.truncate_table(dst_engine, t, s)

    # =========================
    # FASE 4
    # =========================
    set_phase("Executando pipeline", "processamento e carga")

    total_rows = 0
    last_ui_update = 0

    processed_tables = 0

    # Velocidade estável baseada em múltiplos chunks
    weighted_speed_samples = []
    max_speed_samples = 30

    with ProcessPoolExecutor(max_workers=n_cores) as executor:
        for s, t, t_count in work_list:

            processed_tables += 1
            table_rows_processed = 0

            status.info(
                f"📦 Processando: {s}.{t} ({t_count:,} registros) • "
                f"Tabela {processed_tables}/{total_tables}"
            )

            for chunk in db_utils.fetch_rows_streaming(
                src_engine,
                t,
                s,
                chunk_size
            ):

                chunk_start = time.time()

                rows = [dict(r) for r in chunk]

                # =========================
                # ANONIMIZAÇÃO
                # =========================
                if modo == "🛡️ Anonimização Total":
                    if n_cores > 1:
                        sub_sz = max(1, len(rows) // n_cores)

                        futures = [
                            executor.submit(
                                process_chunk_parallel,
                                rows[i:i + sub_sz],
                                modo,
                                anon_geo
                            )
                            for i in range(0, len(rows), sub_sz)
                        ]

                        rows = []

                        for f in futures:
                            try:
                                rows.extend(f.result())
                            except:
                                pass
                    else:
                        rows = process_chunk_parallel(
                            rows,
                            modo,
                            anon_geo
                        )

                # =========================
                # INSERT REAL
                # =========================
                db_utils.insert_rows(dst_engine, t, s, rows)

                inserted_count = len(rows)
                total_rows += inserted_count
                table_rows_processed += inserted_count

                # =========================
                # VELOCIDADE REAL DE PROCESSAMENTO
                # Inclui fetch + anonimização + insert
                # =========================
                chunk_elapsed = max(time.time() - chunk_start, 0.001)
                chunk_speed = inserted_count / chunk_elapsed

                weighted_speed_samples.append(chunk_speed)

                if len(weighted_speed_samples) > max_speed_samples:
                    weighted_speed_samples.pop(0)

                now = time.time()

                if now - last_ui_update > 1:
                    elapsed = now - t0_global

                    global_speed = (
                        total_rows / elapsed
                        if elapsed > 0 else 0
                    )

                    stable_speed = (
                        sum(weighted_speed_samples) /
                        len(weighted_speed_samples)
                        if weighted_speed_samples
                        else global_speed
                    )

                    progress_data = total_rows / max(total_estimated, 1)

                    # 25% preparação + 75% execução
                    total_progress = min(
                        0.25 + (progress_data * 0.75),
                        1.0
                    )

                    cpu = psutil.cpu_percent(interval=0.1)

                    # =========================
                    # ETA MAIS FIEL
                    # =========================
                    remaining_rows = max(
                        total_estimated - total_rows,
                        0
                    )

                    eta_str = "Calculando..."

                    if stable_speed > 0:
                        eta_seconds = remaining_rows / stable_speed

                        eta_h, rem = divmod(
                            int(eta_seconds),
                            3600
                        )
                        eta_m, eta_s = divmod(rem, 60)

                        if eta_h > 0:
                            eta_str = (
                                f"{eta_h:02d}h "
                                f"{eta_m:02d}m "
                                f"{eta_s:02d}s"
                            )
                        else:
                            eta_str = (
                                f"{eta_m:02d}m "
                                f"{eta_s:02d}s"
                            )

                    table_progress = (
                        table_rows_processed / max(t_count, 1)
                        if t_count > 0 else 1.0
                    )

                    metric_placeholder.markdown(f"""
                    ### 📊 Progresso do Pipeline
                    - 🔷 Fase: **4/4**
                    - 📂 Tabela: **{processed_tables}/{total_tables}**
                    - 📦 Progresso tabela: **{table_progress * 100:.1f}%**
                    - 🧮 Registros: **{total_rows:,} / {total_estimated:,}**
                    - ⚡ Velocidade real: **{stable_speed:,.0f} linhas/s**
                    - ⏳ ETA estável: **{eta_str}**
                    - ⏱️ Tempo: **{elapsed:.1f}s**
                    - 💾 RAM: **{proc.memory_info().rss / (1024**2):.0f} MB**
                    - 🔥 CPU: **{cpu:.0f}%**
                    """)

                    progress_bar.progress(
                        min(total_progress, 1.0)
                    )

                    last_ui_update = now

    # =========================
    # FINALIZAÇÃO
    # =========================
    db_utils.set_replication_mode(dst_engine, "origin")

    progress_bar.progress(1.0)

    total_time = time.time() - t0_global

    status.success(
        f"✅ Pipeline concluído com sucesso • "
        f"{total_rows:,} linhas em {total_time:.2f}s"
    )

if start_btn:
    try:
        run_pipeline()
        st.balloons()
    except Exception as e:
        st.error(f"Erro: {e}")