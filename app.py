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
import zipfile
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
# SISTEMA DE MEMÓRIA E ESTADO
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
    for key in ["analise_concluida", "todas_colunas_disponiveis", "colunas_selecionadas_finais", "processed_files"]:
        if key in st.session_state:
            del st.session_state[key]

# ==================================================
# ⏱️ HELPER: PROCESSAMENTO DE TEXTO COM ETA
# ==================================================
def process_text_with_eta(texto_bruto, regras_mascara, progress_bar, status_text):
    if not texto_bruto.strip(): return texto_bruto
    
    blocos = re.split(r'(\n+)', texto_bruto)
    chunks = []
    current_chunk = ""
    
    for bloco in blocos:
        current_chunk += bloco
        if len(current_chunk) > 1000 and '\n' in bloco:
            chunks.append(current_chunk)
            current_chunk = ""
    if current_chunk: chunks.append(current_chunk)
        
    total = len(chunks)
    resultado = ""
    start_time = time.time()
    
    for i, chunk in enumerate(chunks):
        resultado += anonymizer.process_raw_text(chunk, regras_mascara)
        
        processed = i + 1
        elapsed = time.time() - start_time
        speed = processed / elapsed if elapsed > 0 else 0
        eta = (total - processed) / speed if speed > 0 else 0
        
        if progress_bar: progress_bar.progress(processed / total)
        if status_text: status_text.markdown(f"⏳ **Lendo fragmentos:** {processed}/{total} | ⚡ Vel: {speed:.1f} frag/s | ⏱️ ETA: {int(eta)}s")
        
    return resultado

# ==================================================
# CONFIGURAÇÕES DA TELA
# ==================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
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
# O TRABALHADOR FANTASMA (BD)
# ==================================================
def run_pipeline_background(db_type, src_cfg, dst_cfg, filter_tables, n_cores, chunk_size, modo, regras_mascara, target_cols):
    t0_global = time.time()
    try:
        save_progress("Conectando aos bancos de dados...", 0, 0, 0, 0, 0, 0)
        src_engine = db_utils.connect(build_url(db_type, **src_cfg))
        dst_engine = db_utils.connect(build_url(db_type, **dst_cfg))
        db_utils.set_replication_mode(dst_engine, "replica")

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
            save_progress("Erro: Nenhuma linha encontrada.", 0, 0, 0, 0, 0, 0, finalizado=True)
            return

        for s, t, _ in reversed(work_list): db_utils.truncate_table(dst_engine, t, s)
        
        total_rows, processed_tables, last_json_update = 0, 0, 0
        weighted_speed_samples = []

        with ProcessPoolExecutor(max_workers=n_cores) as executor:
            for s, t, t_count in work_list:
                processed_tables += 1
                cols_da_tabela = [c.replace(f"{t}.", "", 1) for c in target_cols if c.startswith(f"{t}.")]

                for chunk in db_utils.fetch_rows_streaming(src_engine, t, s, chunk_size):
                    chunk_start = time.time()
                    rows = [dict(r) for r in chunk]

                    if modo == "🛡️ Anonimização Total":
                        if n_cores > 1:
                            sub_sz = max(1, len(rows) // n_cores)
                            sub_chunks = [rows[i:i + sub_sz] for i in range(0, len(rows), sub_sz)]
                            futures = [executor.submit(anonymizer.process_chunk_parallel, sub_chunk, modo, regras_mascara, cols_da_tabela) for sub_chunk in sub_chunks]
                            rows = []
                            for original_chunk, f in zip(sub_chunks, futures):
                                try: rows.extend(f.result() or original_chunk)
                                except Exception: rows.extend(original_chunk)
                        else:
                            try: rows = anonymizer.process_chunk_parallel(rows, modo, regras_mascara, cols_da_tabela) or rows
                            except Exception: pass

                    if rows: db_utils.insert_rows(dst_engine, t, s, rows)
                    total_rows += len(rows)
                    
                    chunk_speed = len(rows) / max(time.time() - chunk_start, 0.001)
                    weighted_speed_samples.append(chunk_speed)
                    if len(weighted_speed_samples) > 30: weighted_speed_samples.pop(0)

                    now = time.time()
                    if now - last_json_update > 2.5:
                        stable_speed = sum(weighted_speed_samples) / len(weighted_speed_samples) if weighted_speed_samples else 0
                        save_progress(f"Processando: {t}", processed_tables, total_tables, total_rows, total_estimated, stable_speed, now - t0_global, False)
                        last_json_update = now

        db_utils.set_replication_mode(dst_engine, "origin")
        save_progress("Concluído", processed_tables, total_tables, total_rows, total_estimated, 0, time.time() - t0_global, finalizado=True)

    except Exception as e:
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

    st.markdown("### 🛡️ Políticas de Proteção")
    with st.expander("Configurar Tipos de Dados", expanded=False):
        st.markdown("<small>Desmarque para manter o dado original:</small>", unsafe_allow_html=True)
        mask_cpf_rg = st.checkbox("CPF e RG", value=True)
        mask_names  = st.checkbox("Nomes (IA Neural)", value=True)
        mask_phone  = st.checkbox("Telefones", value=True)
        mask_email  = st.checkbox("E-mails", value=True)
        mask_ip     = st.checkbox("Endereços IP", value=True)
        mask_plate  = st.checkbox("Placas e Chassis", value=True)
        mask_geo    = st.checkbox("Coordenadas GPS", value=True)
        mask_date   = st.checkbox("Datas e Horas", value=True)

    dicionario_regras = {
        "CPF": mask_cpf_rg, "RG": mask_cpf_rg, "NOMES_IA": mask_names,
        "PHONE": mask_phone, "EMAIL": mask_email, "IP": mask_ip,
        "PLATE": mask_plate, "CHASSI": mask_plate, "COORD": mask_geo,
        "COORD_SINGLE": mask_geo, "DATE_TIME": mask_date
    }
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
                        for table in [t for t in tables if not allowed or t in allowed]:
                            info_tabela = db_utils.get_table_info(src_engine, table, schema)
                            for col_info in info_tabela.get("columns", []):
                                todas_set.add(f"{table}.{col_info['name']}")
                
                st.session_state.todas_colunas_disponiveis = sorted(list(todas_set))
                st.session_state.analise_concluida = True
                st.success(f"✅ {len(st.session_state.todas_colunas_disponiveis)} colunas mapeadas.")
            except Exception as e: st.error(f"Erro ao analisar: {e}")

        if st.session_state.analise_concluida:
            st.markdown("#### Mapeamento de Exceções")
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
        threading.Thread(target=run_pipeline_background, args=(db_type, src_cfg, dst_cfg, filter_tables, n_cores, chunk_size, modo, dicionario_regras, st.session_state.colunas_selecionadas_finais), daemon=True).start()
        time.sleep(2.5); st.rerun()   

    estado_atual = load_progress()
    if estado_atual:
        if not estado_atual.get("finalizado", True):
            st.info("🔄 O processo de banco de dados está rodando em background.")
            if st.button("🔄 Atualizar Monitor", type="primary"): st.rerun()
                
            l_proc, l_tot, vel = estado_atual.get("linhas_processadas", 0), estado_atual.get("linhas_total", 0), estado_atual.get("velocidade", 0)
            eta = f"{divmod(divmod(int(max(0, l_tot - l_proc) / vel), 60)[0], 60)[0]}h {divmod(int(max(0, l_tot - l_proc) / vel), 60)[0] % 60}m {int(max(0, l_tot - l_proc) / vel) % 60}s" if vel > 0 else "..."
                
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
# MÓDULO 2: ARQUIVOS (LOTE / BATCH)
# --------------------------------------------------
elif st.session_state.view_mode == "Arquivos (.pdf, .csv, .txt)":
    st.markdown("### Processamento de Arquivos em Lote (Batch)")
    st.markdown("Faça upload de um ou mais documentos. A esteira processará todos em sequência na memória RAM.")
    
    arquivos = st.file_uploader("Upload de Documentos", type=["txt", "csv", "pdf"], accept_multiple_files=True, label_visibility="collapsed")
    
    if arquivos:
        tem_pdf_txt = any(a.name.lower().endswith(('pdf', 'txt')) for a in arquivos)
        tem_csv = any(a.name.lower().endswith('csv') for a in arquivos)

        formato_saida = "Manter Original"
        if tem_pdf_txt:
            escolha = st.radio("Formato de saída para PDFs e Textos:", ["Manter Formato Original", "Converter para PDF (.pdf)", "Extrair como Texto Simples (.txt)"], horizontal=True)
            if escolha == "Converter para PDF (.pdf)": formato_saida = "pdf"
            elif escolha == "Extrair como Texto Simples (.txt)": formato_saida = "txt"

        colunas_ignoradas_csv = []
        if tem_csv:
            try:
                todas_colunas_unicas = set()
                for arq in arquivos:
                    if arq.name.lower().endswith('.csv'):
                        arq.seek(0)
                        df_header = pd.read_csv(arq, sep=None, engine='python', encoding_errors='replace', on_bad_lines='skip', nrows=0)
                        todas_colunas_unicas.update(df_header.columns)
                
                if todas_colunas_unicas:
                    st.markdown("#### Mapeamento Global de Exceções (Tabelas CSV)")
                    st.info("⚠️ Selecione as colunas que NÃO devem ser alteradas pela IA em **nenhuma** das tabelas carregadas.")
                    colunas_ignoradas_csv = st.multiselect(
                        "Ignorar colunas (União de todos os CSVs):", 
                        options=sorted(list(todas_colunas_unicas)), 
                        default=[]
                    )
            except Exception as e:
                st.warning(f"Aviso: Não foi possível ler as colunas prévias dos CSVs ({e}). O motor atuará em todas as colunas.")

        if st.button("🚀 Iniciar Esteira de Blindagem", type="primary", use_container_width=True):
            st.session_state.processed_files = []
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            total_arquivos = len(arquivos)

            try:
                for idx, arquivo in enumerate(arquivos):
                    extensao = arquivo.name.split('.')[-1].lower()
                    nome_base = arquivo.name.rsplit('.', 1)[0]
                    status_text.markdown(f"⏳ **Processando [{idx+1}/{total_arquivos}]:** `{arquivo.name}`...")

                    if extensao in ["txt", "pdf"]:
                        texto_bruto = ""
                        if extensao == "txt":
                            texto_bruto = arquivo.getvalue().decode("utf-8")
                        elif extensao == "pdf":
                            if not PyPDF2: st.error("PyPDF2 ausente."); continue
                            pdf_reader = PyPDF2.PdfReader(arquivo)
                            for pagina in pdf_reader.pages:
                                txt_pg = pagina.extract_text()
                                if txt_pg: texto_bruto += txt_pg + "\n\n"
                        
                        resultado_limpo = anonymizer.process_raw_text(texto_bruto, dicionario_regras)
                        fmt_atual = extensao if formato_saida == "Manter Original" else formato_saida
                        
                        if fmt_atual == "txt":
                            st.session_state.processed_files.append({"name": f"SEGURO_{nome_base}.txt", "data": resultado_limpo.encode('utf-8'), "mime": "text/plain"})
                        elif fmt_atual == "pdf":
                            if not FPDF: st.error("FPDF ausente."); continue
                            pdf_out = FPDF()
                            pdf_out.add_page(); pdf_out.set_font("Arial", size=11)
                            pdf_out.multi_cell(0, 5, resultado_limpo.encode('latin-1', 'replace').decode('latin-1'))
                            st.session_state.processed_files.append({"name": f"SEGURO_{nome_base}.pdf", "data": pdf_out.output(dest='S').encode('latin-1'), "mime": "application/pdf"})

                    elif extensao == "csv":
                        if not pd: st.error("Pandas ausente."); continue
                        arquivo.seek(0)
                        df = pd.read_csv(arquivo, sep=None, engine='python', encoding_errors='replace', on_bad_lines='skip')
                        
                        todas_cols_arquivo = list(df.columns)
                        cols_alvo = [c for c in todas_cols_arquivo if c not in colunas_ignoradas_csv]
                        
                        linhas = df.to_dict('records')
                        linhas_processadas = anonymizer.process_chunk_parallel(linhas, "🛡️ Anonimização Total", dicionario_regras, cols_alvo)
                        st.session_state.processed_files.append({"name": f"SEGURO_{nome_base}.csv", "data": pd.DataFrame(linhas_processadas).to_csv(index=False).encode('utf-8'), "mime": "text/csv"})

                    progress_bar.progress((idx + 1) / total_arquivos)

                status_text.success(f"✅ {total_arquivos} arquivo(s) blindado(s) com sucesso!")
            except Exception as e:
                st.error(f"Erro na esteira de processamento: {e}")

        # ==========================================
        # DOWNLOAD DOS ARQUIVOS EM LOTE
        # ==========================================
        if "processed_files" in st.session_state and st.session_state.processed_files:
            st.markdown("### 📥 Arquivos Prontos")
            
            if len(st.session_state.processed_files) > 1:
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                    for pf in st.session_state.processed_files:
                        zip_file.writestr(pf["name"], pf["data"])
                
                st.download_button(
                    label="📦 Baixar Todos os Arquivos (.ZIP)",
                    data=zip_buffer.getvalue(),
                    file_name="Lote_Arquivos_Seguros.zip",
                    mime="application/zip",
                    type="primary",
                    use_container_width=True
                )
                st.divider()

            for idx, pf in enumerate(st.session_state.processed_files):
                st.download_button(
                    label=f"📄 Baixar {pf['name']}",
                    data=pf["data"],
                    file_name=pf["name"],
                    mime=pf["mime"],
                    key=f"btn_dl_{idx}"
                )

# --------------------------------------------------
# MÓDULO 3: TEXTO LIVRE
# --------------------------------------------------
elif st.session_state.view_mode == "Texto Livre":
    st.markdown("### Auditoria Rápida de Fragmentos (Copiar & Colar)")
    
    col1, col2 = st.columns(2)
    with col1:
        texto_original = st.text_area("Entrada (Dados Suspeitos)", height=400, placeholder="Cole o laudo policial aqui...")
        btn_txt = st.button("Injetar Máscaras no Texto", type="primary", use_container_width=True)
        
    with col2:
        st.markdown("**Saída (Dados Protegidos)**")
        placeholder_txt = st.empty()
        
    if btn_txt and texto_original:
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        texto_limpo = process_text_with_eta(texto_original, dicionario_regras, progress_bar, status_text)
        
        progress_bar.empty()
        status_text.empty()
        
        placeholder_txt.markdown(f'<div class="texto-seguro">{html.escape(texto_limpo)}</div>', unsafe_allow_html=True)
        st.success("✅ Texto analisado e mascarado com sucesso!")