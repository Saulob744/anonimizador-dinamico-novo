import streamlit as st
import re
import random
import string
import logging
import warnings
import urllib.parse
import time
import psutil
import os
import pyodbc
from concurrent.futures import ProcessPoolExecutor
import db_utils
import anonymizer
import json
import threading

# ==================================================
# SISTEMA DE MEMÓRIA 
# ==================================================
STATUS_FILE = "pipeline_progress.json"

def save_progress(fase, t_atual, t_total, l_atual, l_total, velocidade, tempo, finalizado=False):
    data = {
        "fase": fase,
        "tabelas_processadas": t_atual,
        "tabelas_total": t_total,
        "linhas_processadas": l_atual,
        "linhas_total": l_total,
        "velocidade": velocidade,
        "tempo_decorrido": tempo,
        "finalizado": finalizado
    }
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        pass

def load_progress():
    if os.path.exists(STATUS_FILE):
        for _ in range(5):
            try:
                with open(STATUS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                time.sleep(0.1)
    return None

def limpar_sessao():
    if os.path.exists(STATUS_FILE):
        try:
            os.remove(STATUS_FILE)
        except Exception:
            pass
    for key in ["analise_concluida", "todas_colunas_disponiveis", "colunas_selecionadas_finais"]:
        if key in st.session_state:
            del st.session_state[key]

# ==================================================
# CONFIGURAÇÕES INICIAIS
# ==================================================
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - [APP]: %(message)s')
logger = logging.getLogger(__name__)

logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", category=UserWarning, message=".*resume_download.*")

st.set_page_config(page_title="🛡️ Aegis Anonymizer Pro", page_icon="🛡️", layout="wide")
st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.8rem; color: #00ffcc; font-weight: bold; }
    .stProgress .st-at { background-color: #00ffcc; }
    .debug-box { border: 1px solid #ff4444; padding: 10px; border-radius: 5px; color: #ff4444; background-color: #ffe6e9; }
    </style>
""", unsafe_allow_html=True)

# ==================================================
# FUNÇÕES AUXILIARES
# ==================================================
def apply_gps_jitter(coord_str):
    try:
        c = float(coord_str)
        return f"{c + random.uniform(-0.003, 0.003):.6f}"
    except ValueError:
        return coord_str

def split_text_into_chunks(text, max_tokens=300):
    words = text.split()
    if len(words) <= max_tokens:
        return [text]
    return [" ".join(words[i : i + max_tokens]) for i in range(0, len(words), max_tokens)]

def build_url(db_type, user, password, host, port, db):
    url = None
    
    if db_type == "mssql":
        try:
            driver = [d for d in pyodbc.drivers() if "SQL Server" in d][-1]
        except IndexError:
            driver = "ODBC Driver 17 for SQL Server"
            
        if host and "localdb" in host.lower():
            odbc_str = rf"DRIVER={{{driver}}};SERVER=(localdb)\MSSQLLocalDB;DATABASE={db};Trusted_Connection=yes;"
            url = f"mssql+pyodbc:///?odbc_connect={urllib.parse.quote_plus(odbc_str)}"
        elif user and password:
            url = f"mssql+pyodbc://{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password)}@{host}:{port}/{db}?driver={urllib.parse.quote_plus(driver)}"
        else:
            url = f"mssql+pyodbc://@{host}:{port}/{db}?driver={urllib.parse.quote_plus(driver)}&trusted_connection=yes"
            
    elif db_type == "postgresql":
        url = f"postgresql+psycopg2://{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password)}@{host}:{port}/{db}?client_encoding=utf8"
        
    elif db_type == "mysql":
        url = f"mysql+pymysql://{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password)}@{host}:{port}/{db}?charset=utf8mb4"

    if url:
        logger.debug(f"🔌 Tentando conexão -> Tipo: {db_type} | Host: {host} | Banco: {db} | Usuário: {user}")

    return url

# ==================================================
# PROCESSAMENTO CENTRAL DAS LINHAS
# ==================================================
def process_chunk_parallel(rows, modo, anon_geo, target_columns):
    if modo != "🛡️ Anonimização Total" or not rows:
        return rows

    logger.debug(f"Trabalhando em chunk de {len(rows)} linhas...")

    colunas_da_tabela = list(rows[0].keys())
    colunas_alvo_reais = [c for c in colunas_da_tabela if c in target_columns]

    if colunas_alvo_reais:
        anonymizer.setup_column_policies(rows, colunas_alvo_reais)

    gps_pair = re.compile(r"^\s*(-?\d{1,3}\.\d{4,})\s*,\s*(-?\d{1,3}\.\d{4,})\s*$")
    gps_single = re.compile(r"^\s*(-?\d{1,3}\.\d{4,})\s*$")
    uuid_regex = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    html_regex = re.compile(r"<[^>]+>|&[a-zA-Z0-9#]+;")

    processed = []
    for r in rows:
        row_dict = dict(r)

        for col, old in row_dict.items():
            if not target_columns or col not in target_columns:
                continue

            if old is None or type(old).__name__ in ['date', 'datetime', 'Timestamp', 'bool', 'int', 'float']:
                continue

            old_str = str(old).strip()

            if uuid_regex.match(old_str):
                continue

            if anon_geo:
                if m := gps_pair.match(old_str):
                    row_dict[col] = f"{apply_gps_jitter(m.group(1))}, {apply_gps_jitter(m.group(2))}"
                    continue 
                if gps_single.match(old_str):
                    try:
                        if -180 <= float(old_str) <= 180:
                            row_dict[col] = apply_gps_jitter(old_str)
                            continue
                    except ValueError:
                        pass

            try:
                vault = {}
                safe_text = old_str
                
                if "<" in safe_text or "&" in safe_text:
                    def hide(match):
                        token = f" __SHLD{len(vault)}__ "
                        vault[token.strip()] = match.group(0)
                        return token
                    
                    safe_text = html_regex.sub(hide, safe_text)
                    safe_text = re.sub(r'\s+', ' ', safe_text).strip()
                
                chunks = split_text_into_chunks(safe_text)
                anon_chunks = []
                
                for chunk in chunks:
                    new_val, _ = anonymizer.anonymize_value(col, chunk, anon_location=anon_geo)
                    anon_chunks.append(str(new_val))
                
                final_text = " ".join(anon_chunks)

                if vault:
                    for token, original in vault.items():
                        final_text = final_text.replace(f" {token} ", original).replace(token, original)

                row_dict[col] = final_text

            except Exception as e:
                logger.error(f"Falha ao processar texto na coluna '{col}': {e}", exc_info=True)
                row_dict[col] = old_str 

        processed.append(row_dict)

    return processed

# ==================================================
# O TRABALHADOR FANTASMA
# ==================================================
def run_pipeline_background(db_type, src_cfg, dst_cfg, filter_tables, n_cores, chunk_size, modo, anon_geo, target_cols):
    t0_global = time.time()
    try:
        save_progress("Conectando aos bancos de dados...", 0, 0, 0, 0, 0, 0)
        src_engine = db_utils.connect(build_url(db_type, **src_cfg))
        dst_engine = db_utils.connect(build_url(db_type, **dst_cfg))
        db_utils.set_replication_mode(dst_engine, "replica")

        save_progress("Mapeando estruturas...", 0, 0, 0, 0, 0, time.time() - t0_global)
        
        schemas = db_utils.get_user_schemas(src_engine)
        allowed = [t.strip() for t in filter_tables.split(",")] if filter_tables else []
        work_list = []
        total_estimated, total_tables = 0, 0

        for s in schemas:
            db_utils.copy_schema(src_engine, dst_engine, s)
            tables = [t for t in db_utils.get_tables(src_engine, s) if not allowed or t in allowed]
            ordered = db_utils.build_dependency_graph(src_engine, tables, s)
            for t in ordered:
                count = db_utils.get_table_count(src_engine, t, s)
                work_list.append((s, t, count))
                total_estimated += count
                total_tables += 1

        if total_estimated == 0:
            save_progress("Erro: Nenhuma linha encontrada nas tabelas-alvo.", 0, 0, 0, 0, 0, 0, finalizado=True)
            return

        save_progress("Limpando destino (Truncate)...", 0, total_tables, 0, total_estimated, 0, time.time() - t0_global)
        for s, t, _ in reversed(work_list):
            db_utils.truncate_table(dst_engine, t, s)

        save_progress("Iniciando Processamento...", 0, total_tables, 0, total_estimated, 0, time.time() - t0_global)
        
        total_rows, processed_tables, last_json_update = 0, 0, 0
        weighted_speed_samples = []

        with ProcessPoolExecutor(max_workers=n_cores) as executor:
            for s, t, t_count in work_list:
                processed_tables += 1
                
                prefixo = f"{t}."
                cols_da_tabela = [c.replace(prefixo, "", 1) for c in target_cols if c.startswith(prefixo)]

                for chunk in db_utils.fetch_rows_streaming(src_engine, t, s, chunk_size):
                    chunk_start = time.time()
                    rows = [dict(r) for r in chunk]

                    if modo == "🛡️ Anonimização Total":
                        if n_cores > 1:
                            sub_sz = max(1, len(rows) // n_cores)
                            sub_chunks = [rows[i:i + sub_sz] for i in range(0, len(rows), sub_sz)]
                            
                            futures = [
                                executor.submit(anonymizer.process_chunk_parallel, sub_chunk, modo, anon_geo, cols_da_tabela)
                                for sub_chunk in sub_chunks
                            ]
                            
                            rows = []
                            for original_chunk, f in zip(sub_chunks, futures):
                                try: 
                                    result = f.result()
                                    rows.extend(result if result else original_chunk)
                                except Exception as e: 
                                    logger.error(f"🚨 CPU worker falhou. Erro: {e}")
                                    rows.extend(original_chunk)
                        else:
                            try:
                                res = anonymizer.process_chunk_parallel(rows, modo, anon_geo, cols_da_tabela)
                                if res: rows = res
                            except Exception as e:
                                logger.error(f"🚨 Erro no Single Core. Erro: {e}")

                    if rows:
                        db_utils.insert_rows(dst_engine, t, s, rows)

                    inserted_count = len(rows)
                    total_rows += inserted_count

                    chunk_speed = inserted_count / max(time.time() - chunk_start, 0.001)
                    weighted_speed_samples.append(chunk_speed)
                    if len(weighted_speed_samples) > 30:
                        weighted_speed_samples.pop(0)

                    now = time.time()
                    if now - last_json_update > 2.5:
                        elapsed = now - t0_global
                        stable_speed = sum(weighted_speed_samples) / len(weighted_speed_samples) if weighted_speed_samples else 0
                        
                        save_progress(
                            fase=f"Processando: {t}",
                            t_atual=processed_tables, t_total=total_tables,
                            l_atual=total_rows, l_total=total_estimated,
                            velocidade=stable_speed, tempo=elapsed, finalizado=False
                        )
                        last_json_update = now

        db_utils.set_replication_mode(dst_engine, "origin")
        save_progress("Concluído", processed_tables, total_tables, total_rows, total_estimated, 0, time.time() - t0_global, finalizado=True)
        logger.info("✅ Thread em background finalizada com sucesso!")

    except Exception as e:
        logger.error(f"🚨 Erro Fatal no Background: {e}", exc_info=True)
        save_progress(f"Erro Fatal: {e}", 0, 0, 0, 0, 0, 0, finalizado=True)

# ==================================================
# INTERFACE E MENU LATERAL
# ==================================================
if "analise_concluida" not in st.session_state:
    st.session_state.analise_concluida = False
if "todas_colunas_disponiveis" not in st.session_state:
    st.session_state.todas_colunas_disponiveis = []
if "colunas_selecionadas_finais" not in st.session_state:
    st.session_state.colunas_selecionadas_finais = []

start_btn = False

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

    with aba_origem: src_cfg = render_db_form("origem")
    with aba_destino: dst_cfg = render_db_form("destino")

    modo = st.selectbox("Modo", ["🛡️ Anonimização Total", "⚡ Cópia Direta"])
    chunk_size = st.number_input("Chunk", value=1000, step=1000) 
    filter_tables = st.text_input("Filtrar tabelas (separadas por vírgula)")
    anon_geo = st.toggle("Mascara De GPS", value=True)

    super_proc = st.toggle("🚀 Multi CPU (Atenção)", value=False)
    n_cores = st.slider("CPU", 1, db_utils.get_cpu_info(), db_utils.get_cpu_info()) if super_proc else 1

    st.divider()
    btn_analisar = st.button("1. Analisar Estrutura", use_container_width=True)
    
    if btn_analisar:
        try:
            src_engine = db_utils.connect(build_url(db_type, **src_cfg))
            schemas = db_utils.get_user_schemas(src_engine)
            allowed = [t.strip() for t in filter_tables.split(",")] if filter_tables else []
            todas_set = set()
            
            if schemas:
                for schema in schemas:
                    tables = db_utils.get_tables(src_engine, schema)
                    valid_tables = [t for t in tables if not allowed or t in allowed]
                    
                    for table in valid_tables:
                        info_tabela = db_utils.get_table_info(src_engine, table, schema)
                        for col_info in info_tabela.get("columns", []):
                            todas_set.add(f"{table}.{col_info['name']}")
            
            todas = sorted(list(todas_set))
            st.session_state.todas_colunas_disponiveis = todas if todas else []
            st.session_state.analise_concluida = True
            st.success(f"✅ Análise concluída! {len(todas)} colunas mapeadas no banco.")
        except Exception as e:
            st.error(f"Erro ao analisar banco: {e}")

    if st.session_state.analise_concluida:
        st.markdown("### Selecione as Exceções")
        st.info("⚠️ Todas as colunas textuais serão anonimizadas. Escolha as que devem ser IGNORADAS.")
        
        opcoes_validas = st.session_state.todas_colunas_disponiveis
        colunas_ignoradas = st.multiselect("Ignorar colunas:", options=opcoes_validas, default=[])
        colunas_finais = [col for col in opcoes_validas if col not in colunas_ignoradas]
        
        start_btn = st.button("2. INICIAR PROCESSAMENTO", type="primary", use_container_width=True)
        if start_btn:
            st.session_state.colunas_selecionadas_finais = colunas_finais

# ==================================================
# FRONTEND: MONITOR DE PROGRESSO
# ==================================================
st.title("🛡️ Pipeline De Proteção De Dados")

if start_btn:
    save_progress("Iniciando Thread Fantasma...", 0, 1, 0, 1, 0, 0, finalizado=False)
    target_cols = st.session_state.colunas_selecionadas_finais
    
    thread = threading.Thread(
        target=run_pipeline_background, 
        args=(db_type, src_cfg, dst_cfg, filter_tables, n_cores, chunk_size, modo, anon_geo, target_cols)
    )
    thread.daemon = True 
    thread.start()
    
    time.sleep(2.5) 
    st.rerun()   

# --- MONITOR FLUIDO EM TEMPO REAL ---
estado_atual = load_progress()

if estado_atual:
    if not estado_atual.get("finalizado", True):
        st.info("🔄 O processo está rodando no servidor. Você pode minimizar a tela ou usar o botão abaixo para atualizar.")

        if st.button("🔄 Atualizar Progresso (Reload)", type="primary"):
            st.rerun()
            
        tempo_sem_atualizar = time.time() - os.path.getmtime(STATUS_FILE)
        
        if tempo_sem_atualizar > 15:
            st.warning("⏳ A IA está processando um lote mais pesado. Aguarde, o processo continua rodando...") 
                
        # EXTRAÇÃO DOS DADOS
        linhas_proc = estado_atual.get("linhas_processadas", 0)
        linhas_totais = estado_atual.get("linhas_total", 0)
        velocidade = estado_atual.get("velocidade", 0)
        
        # CÁLCULO DE TEMPO RESTANTE (ETA)
        linhas_restantes = max(0, linhas_totais - linhas_proc)
        if velocidade > 0:
            segundos_restantes = linhas_restantes / velocidade
            minutos, segundos = divmod(int(segundos_restantes), 60)
            horas, minutos = divmod(minutos, 60)
            tempo_restante_str = f"{horas}h {minutos}m {segundos}s" if horas > 0 else f"{minutos}m {segundos}s"
        else:
            tempo_restante_str = "Calculando..."
            
        # ATUALIZAÇÃO VISUAL DA TELA
        total_linhas_calc = max(linhas_totais, 1)
        progresso = min(0.25 + ((linhas_proc / total_linhas_calc) * 0.75), 1.0) if linhas_proc > 0 else 0.1
        
        st.progress(progresso)
        st.info(f"🔷 Fase atual: {estado_atual['fase']}")
        
        st.markdown(f"""
        ### 📊 Progresso do Pipeline
        - 📂 Tabelas concluídas: **{estado_atual['tabelas_processadas']} / {estado_atual['tabelas_total']}**
        - 🧮 Registros processados: **{linhas_proc:,} / {linhas_totais:,}**
        - 📉 **Faltam processar:** **<span style="color:#ff4444">{linhas_restantes:,} linhas</span>**
        - ⚡ Velocidade média: **{velocidade:,.0f} linhas/s**
        - ⏱️ Tempo decorrido: **{estado_atual['tempo_decorrido']:.1f}s**
        - ⏳ **Tempo Restante (ETA):** **<span style="color:#ffcc00">{tempo_restante_str}</span>**
        """, unsafe_allow_html=True)
        
        time.sleep(2) 
        st.rerun()
    else:
        if "Erro" in estado_atual.get("fase", ""):
            st.error(f"🚨 O processo parou devido a um erro fatal: {estado_atual['fase']}")
        else:
            st.progress(1.0)
            st.success("✅ Pipeline concluído com sucesso!")
            st.balloons()
            
        if st.button("Limpar e Iniciar Nova Sessão"):
            limpar_sessao()
            st.rerun()