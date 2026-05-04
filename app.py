import streamlit as st
import anonymizer
import re
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
def process_chunk_parallel(rows, modo, anon_geo, pre_decisions=None):
    import anonymizer
    import re
    import random  # Adicionado para gerar o ruído do GPS

    # ==================================================
    # SCRUB SECUNDÁRIO (Regex Textual)
    # ==================================================
    sub_scrub = re.compile(
        r"\b("
        r"[A-ZÀ-Ü][a-zà-ü']+(?:\s+[A-ZÀ-Ü][a-zà-ü']+){1,3}"  # Regra 1: Title Case
        r"|"
        r"[A-ZÀ-Ü]{2,}(?:\s+[A-ZÀ-Ü]{2,}){1,3}"             # Regra 2: ALL CAPS (mínimo 2 letras)
        r")\b"
    )

    # ==================================================
    # REGEX DINÂMICOS PARA DETECÇÃO DE GPS
    # ==================================================
    # Formato Par (ex: "-23.550520, -46.633308")
    gps_pair_regex = re.compile(r"^\s*(-?\d{1,3}\.\d{4,})\s*,\s*(-?\d{1,3}\.\d{4,})\s*$")
    
    # Formato Único (ex: "-23.550520")
    gps_single_regex = re.compile(r"^\s*(-?\d{1,3}\.\d{4,})\s*$")

    # Função interna para aplicar o ruído (~500 metros)
    def apply_gps_jitter(coord_str):
        try:
            c = float(coord_str)
            jitter = random.uniform(-0.005, 0.005)
            return f"{c + jitter:.6f}"
        except:
            return coord_str

    if modo != "🛡️ Anonimização Total" or not rows:
        return rows

    processed = []
    column_decisions = pre_decisions if pre_decisions is not None else {}

    # ==================================================
    # FASE 1 e 2 — PERFILAMENTO E DECISÃO
    # ==================================================
    if not column_decisions:
        sample_size = min(50, len(rows))
        
        FREE_TEXT_THRESHOLD = 40 # Se qualquer string passar disso, é texto livre

        for col in rows[0].keys():
            vals = []
            max_length = 0
            has_string = False

            # Avalia o comportamento dos dados na amostra
            for r in rows[:sample_size]:
                v = r.get(col)

                if v is None or isinstance(v, bool) or type(v).__name__ in ['date', 'datetime', 'Timestamp']:
                    continue

                if isinstance(v, (int, float)):
                    vals.append(str(v).strip())
                    continue

                if isinstance(v, str):
                    val_str = v.strip()
                    current_len = len(val_str)
                    
                    if current_len > max_length:
                        max_length = current_len
                        
                    vals.append(val_str)
                    has_string = True
                else:
                    vals.append(str(v).strip())

            # DECISÃO 1: PERFILAMENTO DINÂMICO
            is_free_text = has_string and (max_length >= FREE_TEXT_THRESHOLD)

            if is_free_text:
                column_decisions[col] = True
                try:
                    anonymizer.debug_log(f"[APP COLUMN DECISION] {col} -> True (Dinâmico: Texto Livre | MaxLen={max_length})")
                except Exception:
                    pass
                continue

            # DECISÃO 2: AVALIAÇÃO DE IA / SCORE TRADICIONAL
            try:
                column_decisions[col] = anonymizer.should_anonymize_column(col, vals)
                anonymizer.debug_log(f"[APP COLUMN DECISION] {col} -> {column_decisions[col]}")
            except Exception as e:
                column_decisions[col] = True
                try:
                    anonymizer.debug_log(f"[APP COLUMN SCORE ERROR] Col={col} -> Fallback True")
                except Exception:
                    pass

    # ==================================================
    # FASE 3 — PROCESSAMENTO DAS LINHAS
    # ==================================================
    for r in rows:
        row_dict = dict(r)

        for col, old in row_dict.items():
            if old is None or type(old).__name__ in ['date', 'datetime', 'Timestamp']:
                continue

            old_str = str(old).strip()

            # --------------------------------------------------
            # NOVO: TRATAMENTO DINÂMICO DE GPS (Sem depender de nome de coluna)
            # --------------------------------------------------
            if anon_geo:
                # Tenta identificar par de coordenadas
                match_pair = gps_pair_regex.match(old_str)
                if match_pair:
                    lat, lon = match_pair.groups()
                    row_dict[col] = f"{apply_gps_jitter(lat)}, {apply_gps_jitter(lon)}"
                    continue  # Pula o restante do processamento para esta coluna

                # Tenta identificar coordenada única
                match_single = gps_single_regex.match(old_str)
                if match_single:
                    try:
                        val_float = float(old_str)
                        if -180 <= val_float <= 180:  # Valida se está na escala global
                            row_dict[col] = apply_gps_jitter(old_str)
                            continue  # Pula o restante do processamento para esta coluna
                    except ValueError:
                        pass

            # --------------------------------------------------
            # Ignora números comuns (IDs, preços, etc) que não foram pegos pelo Regex do GPS
            # --------------------------------------------------
            if isinstance(old, (int, float)):
                continue

            # --------------------------------------------------
            # Ignora colunas que a IA/Perfilamento marcaram como seguras
            # --------------------------------------------------
            if not column_decisions.get(col, True):
                continue

            # --------------------------------------------------
            # ANONIMIZAÇÃO PRINCIPAL (IA + Scrub Secundário)
            # --------------------------------------------------
            try:
                new, flag = anonymizer.anonymize_value(col, old, anon_location=anon_geo)

                if flag == "TEXT" and isinstance(new, str):
                    new = sub_scrub.sub(lambda m: anonymizer._get_fake(m.group(1), "PER"), new)

                row_dict[col] = new
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
    anon_geo = st.toggle("Mascara De GPS", value=True)

    super_proc = st.toggle("🚀 Multi CPU", value=False)
    n_cores = st.slider("CPU", 1, db_utils.get_cpu_info(), db_utils.get_cpu_info()) if super_proc else 1

    start_btn = st.button("INICIAR", use_container_width=True)


# ==================================================
# PIPELINE
# ==================================================
st.title("🛡️ Pipeline De Proteção De Dados")
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