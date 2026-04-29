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

# Reload para desenvolvimento
importlib.reload(db_utils)
importlib.reload(anonymizer)

# ==================================================
# UI & ESTILOS
# ==================================================
st.set_page_config(page_title="🛡️ Aegis Anonymizer Pro", page_icon="🛡️", layout="wide")

st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.8rem; color: #00ffcc; font-weight: bold; }
    .stProgress .st-at { background-color: #00ffcc; }
    </style>
""", unsafe_allow_html=True)

# ==================================================
# FUNÇÕES CORE
# ==================================================
def process_chunk_parallel(rows, modo, anon_geo):
    """Função executada nos workers isolados."""
    import anonymizer
    import re
    
    SUB_SCRUB = re.compile(r"\b([A-ZÀ-Ü][a-zà-ü']+(?:\s+[A-ZÀ-Ü][a-zà-ü']+){1,3})\b")
    processed = []
    
    for r in rows:
        row_dict = dict(r)
        if modo == "🛡️ Anonimização Total":
            for col, old in list(row_dict.items()):
                if old is None or isinstance(old, (int, float, bool)) or type(old).__name__ in ['date', 'datetime', 'Timestamp']:
                    continue
                try:
                    new, flag = anonymizer.anonymize_value(col, old, anon_location=anon_geo)
                    if flag == "TEXT" and isinstance(new, str):
                        new = SUB_SCRUB.sub(lambda m: anonymizer._get_fake(m.group(1), "PER"), new)
                    row_dict[col] = new
                except: 
                    continue
        processed.append(row_dict)
    return processed

def build_url(db_type, user, password, host, port, db):
    """Gera a URL de conexão com base no banco selecionado."""
    if db_type == "mssql":
        driver = [d for d in pyodbc.drivers() if "SQL Server" in d][-1]
        if host and "localdb" in host.lower():
            # Uso de 'rf' (Raw String) para evitar SyntaxWarning com a barra invertida
            odbc_str = rf"DRIVER={{{driver}}};SERVER=(localdb)\MSSQLLocalDB;DATABASE={db};Trusted_Connection=yes;"
            return f"mssql+pyodbc:///?odbc_connect={urllib.parse.quote_plus(odbc_str)}"
        return f"mssql+pyodbc://@{host}:{port}/{db}?driver={urllib.parse.quote_plus(driver)}&trusted_connection=yes"
    
    prefix = "postgresql+psycopg2" if db_type == "postgresql" else "mysql+pymysql"
    return f"{prefix}://{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password)}@{host}:{port}/{db}"

# ==================================================
# BARRA LATERAL (CONFIGURAÇÕES AMIGÁVEIS)
# ==================================================
with st.sidebar:
    st.title("🛡️ Aegis Control")
    db_type = st.selectbox("Tipo de Banco de Dados", ["postgresql", "mysql", "mssql"])
    
    aba_origem, aba_destino = st.tabs(["🔴 Banco Origem", "🟢 Banco Destino"])
    
    def render_db_form(prefix):
        """Renderiza campos de DB para evitar código repetido."""
        return {
            "host": st.text_input("Host (Servidor)", value="localhost", key=f"{prefix}_host"),
            "port": st.text_input("Porta", key=f"{prefix}_port"),
            "db": st.text_input("Nome do Banco", key=f"{prefix}_db"),
            "user": st.text_input("Usuário", key=f"{prefix}_user"),
            "password": st.text_input("Senha", type="password", key=f"{prefix}_pass") 
        }

    with aba_origem: src_cfg = render_db_form("origem")
    with aba_destino: dst_cfg = render_db_form("destino")

    st.divider()
    modo = st.selectbox("Modo de Operação", ["🛡️ Anonimização Total", "⚡ Cópia Direta"])
    chunk_size = st.number_input("Tamanho do Lote (Chunk Size)", value=10000, step=1000)
    filter_tables = st.text_input("Filtrar Tabelas (Ex: tb_clientes, tb_vendas)")
    anon_geo = st.toggle("Desfocar Dados de GPS", value=True)
    
    st.divider()
    super_proc = st.toggle("🚀 Ativar Superprocessamento", value=False)
    n_cores = st.slider("Núcleos de CPU", 1, db_utils.get_cpu_info(), db_utils.get_cpu_info()) if super_proc else 1
    
    start_btn = st.button("🚀 INICIAR PIPELINE", use_container_width=True)

# ==================================================
# INTERFACE PRINCIPAL E PIPELINE
# ==================================================
st.title("🛡️ Pipeline de Proteção de Dados")
progress_bar = st.progress(0)
status = st.empty()
metric_placeholder = st.empty()

def run_pipeline():
    proc = psutil.Process(os.getpid())
    t0_global = time.time()
    
    # 1. Feedback inicial de que está pensando...
    metric_placeholder.markdown("""
    ### 📊 Métricas Iniciais
    - 🧮 Linhas: **Mapeando banco de dados...**
    - ⚡ Velocidade: **Aguardando...** | ⏳ ETA: **Calculando...**
    - ⏱️ Tempo Decorrido: **0.0s**
    """)
    status.info("🔌 Conectando aos bancos de dados...")
    
    # 2. Conexões
    src_engine = db_utils.connect(build_url(db_type, **src_cfg))
    dst_engine = db_utils.connect(build_url(db_type, **dst_cfg))
    db_utils.set_replication_mode(dst_engine, "replica")
    
    # 3. Mapeamento
    status.info("🔍 Inspecionando tabelas e dependências...")
    schemas = db_utils.get_user_schemas(src_engine)
    allowed = [t.strip() for t in filter_tables.split(",")] if filter_tables else []
    
    work_list = []
    total_estimated = 0
    for s in schemas:
        db_utils.copy_schema(src_engine, dst_engine, s)
        tables = [t for t in db_utils.get_tables(src_engine, s) if not allowed or t in allowed]
        ordered = db_utils.build_dependency_graph(src_engine, tables, s)
        for t in ordered:
            count = db_utils.get_table_count(src_engine, t, s)
            work_list.append((s, t, count))
            total_estimated += count

    # 4. Limpeza
    status.warning("🧹 Preparando destino (Limpando tabelas antigas)...")
    for s, t, _ in reversed(work_list):
        db_utils.truncate_table(dst_engine, t, s)

    # 5. Processamento Principal
    total_rows = 0
    with ProcessPoolExecutor(max_workers=n_cores) as executor:
        for s, t, t_count in work_list:
            status.info(f"📦 Transferindo: {s}.{t} ({t_count:,} linhas estimadas)")
            
            for chunk in db_utils.fetch_rows_streaming(src_engine, t, s, chunk_size):
                rows = [dict(r) for r in chunk]
                
                if modo == "🛡️ Anonimização Total":
                    if n_cores > 1:
                        sub_sz = max(1, len(rows) // n_cores)
                        futures = [executor.submit(process_chunk_parallel, rows[i:i+sub_sz], modo, anon_geo) 
                                   for i in range(0, len(rows), sub_sz)]
                        rows = [r for f in futures for r in f.result()]
                    else:
                        rows = process_chunk_parallel(rows, modo, anon_geo)

                db_utils.insert_rows(dst_engine, t, s, rows)
                total_rows += len(rows)

                # --- ATUALIZAÇÃO DO PAINEL EM TEMPO REAL ---
                elapsed = time.time() - t0_global
                speed = total_rows / elapsed if elapsed > 0 else 0
                progress = min(total_rows / max(total_estimated, 1), 1.0)
                
                eta_str = "Calculando..."
                if speed > 0:
                    rem_seconds = (total_estimated - total_rows) / speed
                    m, s_rem = divmod(int(rem_seconds), 60)
                    h, m = divmod(m, 60)
                    eta_str = f"{h:02d}h {m:02d}m {s_rem:02d}s" if h > 0 else f"{m:02d}m {s_rem:02d}s"

                metric_placeholder.markdown(f"""
                ### 📊 Métricas ({'🚀 Super-rápido' if n_cores > 1 else 'Padrão'})
                - 🧮 Linhas Processadas: **{total_rows:,} / {total_estimated:,}**
                - ⚡ Velocidade Média: **{speed:,.0f} linhas/s** | ⏳ Faltam: **{eta_str}**
                - ⏱️ Tempo Decorrido: **{elapsed:.1f}s** | 💾 Uso de RAM: **{proc.memory_info().rss / 1024**2:.0f} MB**
                """)
                progress_bar.progress(progress)

    db_utils.set_replication_mode(dst_engine, "origin")
    status.success(f"✅ Pipeline concluído com sucesso em {time.time() - t0_global:.2f}s!")

# Disparo da Ação
if start_btn:
    try:
        run_pipeline()
        st.balloons()
    except Exception as e:
        st.error(f"❌ Erro Crítico durante a execução: {e}")