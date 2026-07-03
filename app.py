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
import html
import pyodbc
import json
import threading
import io
from concurrent.futures import ProcessPoolExecutor 
import anonymizer
import db_utils

# ==================================================
# 📦 IMPORTAÇÕES CONDICIONAIS
# ==================================================
try: import pandas as pd
except ImportError: pd = None
try: import PyPDF2
except ImportError: PyPDF2 = None
try: import docx
except ImportError: docx = None
try: from fpdf import FPDF
except ImportError: FPDF = None

# ==================================================
# SISTEMA DE MEMÓRIA
# ==================================================
STATUS_FILE = "pipeline_progress.json"

def save_progress(fase, t_atual, t_total, l_atual, l_total, velocidade, tempo, finalizado=False):
    data = {
        "fase": fase, "tabelas_processadas": t_atual, "tabelas_total": t_total,
        "linhas_processadas": l_atual, "linhas_total": l_total,
        "velocidade": velocidade, "tempo_decorrido": tempo, "finalizado": finalizado
    }
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
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
        try: os.remove(STATUS_FILE)
        except Exception: pass
    for key in ["analise_concluida", "todas_colunas_disponiveis", "colunas_selecionadas_finais"]:
        if key in st.session_state:
            del st.session_state[key]

# ==================================================
# CONFIGURAÇÕES DA TELA
# ==================================================
logging.basicConfig(level=logging.ERROR, format='%(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context").setLevel(logging.ERROR)
warnings.filterwarnings("ignore")

st.set_page_config(page_title="Pipeline de Proteção", page_icon="🔒", layout="wide")
st.markdown("""
    <style>
    :root { --primary: #0ea5e9; --bg-dark: #0f172a; --text-light: #e2e8f0; }
    .block-container { padding-top: 2rem; padding-bottom: 2rem; }
    h1, h2, h3 { font-family: 'Inter', sans-serif; font-weight: 600; color: #f8fafc; }
    .stButton>button { border-radius: 6px; font-weight: 500; transition: all 0.2s; }
    .stButton>button[kind="primary"] { background-color: var(--primary); border: none; }
    .stButton>button[kind="primary"]:hover { background-color: #0284c7; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); }
    [data-testid="stMetricValue"] { font-size: 1.5rem; color: var(--primary); font-weight: 600; }
    .stProgress .st-at { background-color: var(--primary); }
    .texto-seguro { border-left: 4px solid var(--primary); padding: 16px; border-radius: 4px; background-color: #1e293b; color: #cbd5e1; white-space: pre-wrap; font-family: 'Fira Code', monospace; font-size: 0.9rem; line-height: 1.5; }
    .sidebar-section { margin-bottom: 1.5rem; padding-bottom: 1rem; border-bottom: 1px solid #334155; }
    div[data-testid="stSidebarNav"] { display: none; }
    </style>
""", unsafe_allow_html=True)

# ==================================================
# FUNÇÕES AUXILIARES DE BANCO DE DADOS
# ==================================================
def build_url(db_type, user, password, host, port, db):
    url = None
    if db_type == "mssql":
        try: driver = [d for d in pyodbc.drivers() if "SQL Server" in d][-1]
        except IndexError: driver = "ODBC Driver 17 for SQL Server"
            
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
    return url

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

                    if rows: db_utils.insert_rows(dst_engine, t, s, rows)
                    total_rows += len(rows)
                    chunk_speed = len(rows) / max(time.time() - chunk_start, 0.001)
                    weighted_speed_samples.append(chunk_speed)
                    if len(weighted_speed_samples) > 30: weighted_speed_samples.pop(0)

                    now = time.time()
                    if now - last_json_update > 2.5:
                        elapsed = now - t0_global
                        stable_speed = sum(weighted_speed_samples) / len(weighted_speed_samples) if weighted_speed_samples else 0
                        save_progress(f"Processando: {t}", processed_tables, total_tables, total_rows, total_estimated, stable_speed, elapsed, False)
                        last_json_update = now

        db_utils.set_replication_mode(dst_engine, "origin")
        save_progress("Concluído", processed_tables, total_tables, total_rows, total_estimated, 0, time.time() - t0_global, finalizado=True)

    except Exception as e:
        logger.error(f"🚨 Erro Fatal no Background: {e}", exc_info=True)
        save_progress(f"Erro Fatal: {e}", 0, 0, 0, 0, 0, 0, finalizado=True)


# ==================================================
# 🌐 ESTRUTURA PRINCIPAL 
# ==================================================
st.title("🔒 Pipeline de Proteção de Dados")
st.markdown("Proteção de PIIs em escala para fluxos operacionais e de inteligência.")

if "view_mode" not in st.session_state: st.session_state.view_mode = "Bancos de Dados"
if "analise_concluida" not in st.session_state: st.session_state.analise_concluida = False
if "todas_colunas_disponiveis" not in st.session_state: st.session_state.todas_colunas_disponiveis = []
if "colunas_selecionadas_finais" not in st.session_state: st.session_state.colunas_selecionadas_finais = []

start_btn = False

with st.sidebar:
    st.markdown("### Selecione o Modo")
    modo_selecionado = st.radio("Menu de Navegação", ["🗄️ Bancos de Dados", "📄 Arquivos (.pdf, .csv, .txt)", "📝 Texto Livre"], label_visibility="collapsed")
    st.session_state.view_mode = modo_selecionado.replace("🗄️ ", "").replace("📄 ", "").replace("📝 ", "")
    st.divider()

    if st.session_state.view_mode == "Bancos de Dados":
        st.markdown("<div class='sidebar-section'>", unsafe_allow_html=True)
        st.markdown("#### Configuração Conexão")
        db_type = st.selectbox("Motor", ["postgresql", "mysql", "mssql"])
        ab_o, ab_d = st.tabs(["Origem", "Destino"])

        def render_db_form(prefix):
            return {
                "host": st.text_input("Host", value="localhost", key=f"{prefix}_host"),
                "port": st.text_input("Porta", key=f"{prefix}_port"),
                "db": st.text_input("Banco", key=f"{prefix}_db"),
                "user": st.text_input("Usuário", key=f"{prefix}_user"),
                "password": st.text_input("Senha", type="password", key=f"{prefix}_pass")
            }

        with ab_o: src_cfg = render_db_form("origem")
        with ab_d: dst_cfg = render_db_form("destino")
        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown("#### Parâmetros de Motor")
        modo = st.selectbox("Modo", ["🛡️ Anonimização Total", "⚡ Cópia Direta"])
        chunk_size = st.number_input("Lote (Rows)", value=1000, step=1000) 
        filter_tables = st.text_input("Filtrar tabelas (vírgula)")
        anon_geo = st.toggle("Mascara De GPS", value=True)

        super_proc = st.toggle("🚀 Multi CPU", value=False)
        n_cores = st.slider("CPU", 1, psutil.cpu_count(logical=True), psutil.cpu_count(logical=True)) if super_proc else 1

        st.divider()
        btn_analisar = st.button("Analisar Estrutura", use_container_width=True)
        
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
                st.session_state.todas_colunas_disponiveis = todas
                st.session_state.analise_concluida = True
                st.success(f"✅ {len(todas)} colunas mapeadas.")
            except Exception as e:
                st.error(f"Erro ao analisar: {e}")

        if st.session_state.analise_concluida:
            st.markdown("#### Mapeamento de Exceções")
            st.info("⚠️ Marque as colunas que NÃO devem ser alteradas pela IA.")
            colunas_ignoradas = st.multiselect("Ignorar colunas:", options=st.session_state.todas_colunas_disponiveis, default=[])
            
            start_btn = st.button("Iniciar Pipeline de Banco", type="primary", use_container_width=True)
            if start_btn: 
                st.session_state.colunas_selecionadas_finais = [col for col in st.session_state.todas_colunas_disponiveis if col not in colunas_ignoradas]
# --------------------------------------------------
# MÓDULO 1: BANCOS DE DADOS
# --------------------------------------------------
if st.session_state.view_mode == "Bancos de Dados":
    st.markdown("### Monitoramento do Pipeline Estruturado")
    
    if start_btn:
        save_progress("Iniciando Thread Fantasma...", 0, 1, 0, 1, 0, 0, finalizado=False)
        threading.Thread(target=run_pipeline_background, args=(db_type, src_cfg, dst_cfg, filter_tables, n_cores, chunk_size, modo, anon_geo, st.session_state.colunas_selecionadas_finais), daemon=True).start()
        time.sleep(2.5) 
        st.rerun()   

    estado_atual = load_progress()
    if estado_atual:
        if not estado_atual.get("finalizado", True):
            st.info("🔄 O processo de banco de dados está rodando em background.")
            if st.button("🔄 Atualizar Monitor", type="primary", key="btn_reload_db"): st.rerun()
                
            l_proc, l_tot, vel = estado_atual.get("linhas_processadas", 0), estado_atual.get("linhas_total", 0), estado_atual.get("velocidade", 0)
            restante = max(0, l_tot - l_proc)
            if vel > 0:
                mins, secs = divmod(int(restante / vel), 60)
                hrs, mins = divmod(mins, 60)
                eta = f"{hrs}h {mins}m {secs}s" if hrs > 0 else f"{mins}m {secs}s"
            else: eta = "Calculando..."
                
            st.progress(min(0.25 + ((l_proc / max(l_tot, 1)) * 0.75), 1.0) if l_proc > 0 else 0.1)
            
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Status Atual", estado_atual['fase'])
            c2.metric("Tabelas Concluídas", f"{estado_atual['tabelas_processadas']}/{estado_atual['tabelas_total']}")
            c3.metric("Registros Processados", f"{l_proc:,}/{l_tot:,}")
            c4.metric("Mutação Neural", f"{vel:,.0f} reg/s" if vel > 0 else "-", delta=eta, delta_color="off")
            
            time.sleep(2); st.rerun()
        else:
            if "Erro" in estado_atual.get("fase", ""): st.error(f"🚨 Falha no Banco: {estado_atual['fase']}")
            else: st.success("✅ Cópia e Mascaramento de Banco de Dados Concluídos com Sucesso!")
            if st.button("Limpar Histórico de Banco"): limpar_sessao(); st.rerun()
    else:
        st.info("👈 Configure a conexão no menu lateral para iniciar a análise.")

# --------------------------------------------------
# MÓDULO 2: ARQUIVOS
# --------------------------------------------------
elif st.session_state.view_mode == "Arquivos (.pdf, .csv, .txt)":
    st.markdown("### Processamento de Arquivos Avulsos")
    st.markdown("A operação ocorre inteiramente na memória RAM. Os dados não tocam o disco rígido do servidor.")
    
    arquivo = st.file_uploader("Upload de Documento", type=["txt", "csv", "pdf"], label_visibility="collapsed")
    anon_geo_arq = st.checkbox("Proteger Coordenadas Geográficas (GPS)?", value=True)
    
    if arquivo:
        extensao = arquivo.name.split('.')[-1].lower()
        nome_base = arquivo.name.rsplit('.', 1)[0]
        
        formato_saida = extensao
        if extensao in ["pdf", "txt"]:
            escolha = st.radio("Como deseja receber o arquivo limpo?", ["Manter Formato Original", "Converter para PDF (.pdf)", "Extrair como Texto Simples (.txt)"], horizontal=True)
            if escolha == "Converter para PDF (.pdf)": formato_saida = "pdf"
            elif escolha == "Extrair como Texto Simples (.txt)": formato_saida = "txt"

        if st.button("Iniciar Blindagem do Documento", type="primary"):
            with st.spinner("Analisando estrutura e injetando máscaras..."):
                try:
                    if extensao in ["txt", "pdf"]:
                        texto_bruto = ""
                        if extensao == "txt":
                            texto_bruto = arquivo.getvalue().decode("utf-8")
                        elif extensao == "pdf":
                            if not PyPDF2: st.error("Biblioteca PyPDF2 ausente."); st.stop()
                            pdf_reader = PyPDF2.PdfReader(arquivo)
                            for pagina in pdf_reader.pages:
                                txt_pg = pagina.extract_text()
                                if txt_pg: texto_bruto += txt_pg + "\n\n"
                        
                        resultado_limpo = anonymizer.process_raw_text(texto_bruto, anon_geo_arq)
                        
                        if formato_saida == "txt":
                            st.session_state.payload_file = resultado_limpo
                            st.session_state.payload_name = f"SEGURO_{nome_base}.txt"
                            st.session_state.payload_mime = "text/plain"
                        elif formato_saida == "pdf":
                            if not FPDF: st.error("Biblioteca fpdf ausente."); st.stop()
                            pdf_out = FPDF()
                            pdf_out.add_page(); pdf_out.set_font("Arial", size=11)
                            texto_seguro = resultado_limpo.encode('latin-1', 'replace').decode('latin-1')
                            pdf_out.multi_cell(0, 5, texto_seguro)
                            
                            st.session_state.payload_file = pdf_out.output(dest='S').encode('latin-1')
                            st.session_state.payload_name = f"SEGURO_{nome_base}.pdf"
                            st.session_state.payload_mime = "application/pdf"

                    elif extensao == "csv":
                        if not pd: st.error("Biblioteca Pandas ausente."); st.stop()
                        df = pd.read_csv(arquivo)
                        linhas = df.to_dict('records')
                        linhas_processadas = anonymizer.process_chunk_parallel(linhas, "🛡️ Anonimização Total", anon_geo_arq, list(df.columns))
                        
                        st.session_state.payload_file = pd.DataFrame(linhas_processadas).to_csv(index=False).encode('utf-8')
                        st.session_state.payload_name = f"SEGURO_{nome_base}.csv"
                        st.session_state.payload_mime = "text/csv"

                    st.success("✅ Blindagem concluída com sucesso!")
                except Exception as e: st.error(f"Erro no processamento: {e}")

        if "payload_file" in st.session_state:
            st.download_button(label=f"📥 Fazer Download: {st.session_state.payload_name}", data=st.session_state.payload_file, file_name=st.session_state.payload_name, mime=st.session_state.payload_mime, type="primary")

# --------------------------------------------------
# MÓDULO 3: TEXTO LIVRE
# --------------------------------------------------
elif st.session_state.view_mode == "Texto Livre":
    st.markdown("### Auditoria Rápida de Fragmentos (Copiar & Colar)")
    
    col1, col2 = st.columns(2)
    with col1:
        texto_original = st.text_area("Entrada (Dados Suspeitos)", height=400, placeholder="Cole o laudo policial aqui...")
        anon_geo_txt = st.checkbox("Proteger Coordenadas Geográficas?", value=True)
        btn_txt = st.button("Injetar Máscaras no Texto", type="primary", use_container_width=True)
        
    with col2:
        st.markdown("**Saída (Dados Protegidos)**")
        placeholder_txt = st.empty()
        
    if btn_txt and texto_original:
        with st.spinner("Varrendo PIIs e analisando contexto..."):
            texto_limpo = anonymizer.process_raw_text(texto_original, anon_geo_txt)
            placeholder_txt.markdown(f'<div class="texto-seguro">{html.escape(texto_limpo)}</div>', unsafe_allow_html=True)