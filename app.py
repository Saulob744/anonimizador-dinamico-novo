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
import csv
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
FILE_STATUS_FILE = "file_pipeline_progress.json"

if "CACHE_RESULTADOS_ARQUIVOS" not in st.session_state:
    st.session_state.CACHE_RESULTADOS_ARQUIVOS = None
if "file_process_started" not in st.session_state:
    st.session_state.file_process_started = False

def save_progress(arquivo_alvo, fase, t_atual, t_total, l_atual, l_total, velocidade, tempo, finalizado=False):
    data = {
        "fase": fase, "tabelas_processadas": t_atual, "tabelas_total": t_total,
        "linhas_processadas": l_atual, "linhas_total": l_total,
        "velocidade": velocidade, "tempo_decorrido": tempo, "finalizado": finalizado
    }
    try:
        with open(arquivo_alvo, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass

def load_progress(arquivo_alvo):
    if os.path.exists(arquivo_alvo):
        for _ in range(5):
            try:
                with open(arquivo_alvo, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                time.sleep(0.1)
    return None

def limpar_sessao():
    for f in [STATUS_FILE, FILE_STATUS_FILE, "resultado_lote.zip"]:
        if os.path.exists(f):
            try: os.remove(f)
            except Exception: pass
    for key in ["analise_concluida", "todas_colunas_disponiveis", "colunas_selecionadas_finais", "CACHE_RESULTADOS_ARQUIVOS", "file_process_started"]:
        if key in st.session_state:
            del st.session_state[key]

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
# O TRABALHADOR FANTASMA
# ==================================================
def run_pipeline_background(db_type, src_cfg, dst_cfg, filter_tables, n_cores, chunk_size, modo, regras_mascara, target_cols):
    t0_global = time.time()
    try:
        save_progress(STATUS_FILE, "Conectando aos bancos de dados...", 0, 0, 0, 0, 0, 0)
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
            save_progress(STATUS_FILE, "Erro: Nenhuma linha encontrada.", 0, 0, 0, 0, 0, 0, finalizado=True)
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
                        save_progress(STATUS_FILE, f"Processando: {t}", processed_tables, total_tables, total_rows, total_estimated, stable_speed, now - t0_global, False)
                        last_json_update = now

        db_utils.set_replication_mode(dst_engine, "origin")
        save_progress(STATUS_FILE, "Concluído", processed_tables, total_tables, total_rows, total_estimated, 0, time.time() - t0_global, finalizado=True)

    except Exception as e:
        save_progress(STATUS_FILE, f"Erro Fatal: {e}", 0, 0, 0, 0, 0, 0, finalizado=True)


# ==================================================
# O TRABALHADOR FANTASMA
# ==================================================
def run_files_pipeline_background(arquivos_payload, formato_saida, regras_mascara, colunas_ignoradas_csv, chunk_size, n_cores):
    t0_global = time.time()
    total_arquivos = len(arquivos_payload)
    arquivos_prontos = []
    
    try:
        save_progress(FILE_STATUS_FILE, "Iniciando Esteira de Arquivos...", 0, total_arquivos, 0, 100, 0, 0, False)
        
        for idx, arq in enumerate(arquivos_payload):
            nome_base = arq["name"].rsplit('.', 1)[0]
            extensao = arq["ext"]
            bytes_in = arq["bytes"]
            
            save_progress(FILE_STATUS_FILE, f"Processando: {arq['name']}", idx, total_arquivos, 0, 100, 0, time.time()-t0_global, False)

            if extensao in ["txt", "pdf"]:
                texto_bruto = ""
                if extensao == "txt":
                    texto_bruto = bytes_in.decode("utf-8", errors='ignore')
                elif extensao == "pdf":
                    pdf_reader = PyPDF2.PdfReader(io.BytesIO(bytes_in))
                    for pagina in pdf_reader.pages:
                        txt_pg = pagina.extract_text()
                        if txt_pg: texto_bruto += txt_pg + "\n\n"
                
                resultado_limpo = anonymizer.process_raw_text(texto_bruto, regras_mascara)
                fmt_atual = extensao if formato_saida == "Manter Original" else formato_saida
                
                if fmt_atual == "txt":
                    arquivos_prontos.append({"name": f"SEGURO_{nome_base}.txt", "data": resultado_limpo.encode('utf-8')})
                elif fmt_atual == "pdf":
                    pdf_out = FPDF()
                    pdf_out.add_page(); pdf_out.set_font("Arial", size=11)
                    pdf_out.multi_cell(0, 5, resultado_limpo.encode('latin-1', 'replace').decode('latin-1'))
                    arquivos_prontos.append({"name": f"SEGURO_{nome_base}.pdf", "data": pdf_out.output(dest='S').encode('latin-1')})

            elif extensao == "csv":
                buffer_in = io.BytesIO(bytes_in)
                buffer_out = io.BytesIO()
                
                amostra = buffer_in.read(4096).decode('utf-8', errors='ignore')
                buffer_in.seek(0)
                try: sep_detectado = csv.Sniffer().sniff(amostra).delimiter
                except: sep_detectado = ','

                df_header = pd.read_csv(io.BytesIO(bytes_in), sep=sep_detectado, encoding_errors='replace', on_bad_lines='skip', nrows=0)
                cols_alvo = [c for c in df_header.columns if c not in colunas_ignoradas_csv]
                
                chunk_iter = pd.read_csv(buffer_in, sep=sep_detectado, encoding_errors='replace', on_bad_lines='skip', chunksize=chunk_size)
                
                first_chunk = True
                linhas_processadas_total = 0
                
                with ProcessPoolExecutor(max_workers=n_cores) as executor:
                    for chunk in chunk_iter:
                        lote_start = time.time()
                        linhas = chunk.to_dict('records')
                        
                        if n_cores > 1:
                            sub_sz = max(1, len(linhas) // n_cores)
                            sub_chunks = [linhas[i:i + sub_sz] for i in range(0, len(linhas), sub_sz)]
                            futures = [executor.submit(anonymizer.process_chunk_parallel, sub_chunk, "🛡️ Anonimização Total", regras_mascara, cols_alvo) for sub_chunk in sub_chunks]
                            linhas_limpas = []
                            for sub, f in zip(sub_chunks, futures):
                                try: linhas_limpas.extend(f.result() or sub)
                                except Exception: linhas_limpas.extend(sub)
                        else:
                            try: linhas_limpas = anonymizer.process_chunk_parallel(linhas, "🛡️ Anonimização Total", regras_mascara, cols_alvo) or linhas
                            except Exception: linhas_limpas = linhas

                        df_out = pd.DataFrame(linhas_limpas)
                        df_out.to_csv(buffer_out, index=False, mode='w' if first_chunk else 'a', header=first_chunk)
                        first_chunk = False
                        
                        linhas_processadas_total += len(linhas_limpas)
                        vel_chunk = len(linhas_limpas) / max(time.time() - lote_start, 0.001)
                        save_progress(FILE_STATUS_FILE, f"Lote CSV: {arq['name']} ({linhas_processadas_total} linhas)", idx, total_arquivos, linhas_processadas_total, linhas_processadas_total+1000, vel_chunk, time.time()-t0_global, False)
                
                arquivos_prontos.append({"name": f"SEGURO_{nome_base}.csv", "data": buffer_out.getvalue()})

        if len(arquivos_prontos) > 0:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for pf in arquivos_prontos:
                    zip_file.writestr(pf["name"], pf["data"])
            
            with open("resultado_lote.zip", "wb") as f:
                f.write(zip_buffer.getvalue())

        save_progress(FILE_STATUS_FILE, "Concluído", total_arquivos, total_arquivos, 100, 100, 0, time.time() - t0_global, finalizado=True)

    except Exception as e:
        logger.error(f"Erro Arquivos Background: {e}", exc_info=True)
        save_progress(FILE_STATUS_FILE, f"Erro Fatal: {e}", 0, 0, 0, 0, 0, 0, finalizado=True)


# ==================================================
# 🌐 ESTRUTURA PRINCIPAL 
# ==================================================
st.title("🔒 Pipeline de Proteção de Dados")
st.markdown("Proteção de PIIs em escala para fluxos operacionais e de inteligência.")

if "view_mode" not in st.session_state: st.session_state.view_mode = "Bancos de Dados"

with st.sidebar:
    st.markdown("### Selecione o Modo")
    modo_selecionado = st.radio("Menu de Navegação", ["🗄️ Bancos de Dados", "📄 Arquivos (.pdf, .csv, .txt)", "📝 Texto Livre"], label_visibility="collapsed")
    
    if modo_selecionado.replace("🗄️ ", "").replace("📄 ", "").replace("📝 ", "") != st.session_state.view_mode:
        limpar_sessao()
        st.session_state.view_mode = modo_selecionado.replace("🗄️ ", "").replace("📄 ", "").replace("📝 ", "")
        st.rerun()

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

        st.markdown("#### Parâmetros de Motor")
        modo = st.selectbox("Modo BD", ["🛡️ Anonimização Total", "⚡ Cópia Direta"])
        chunk_size_db = st.number_input("Lote DB (Rows)", value=1000, step=1000) 
        filter_tables = st.text_input("Filtrar tabelas (vírgula)")
        super_proc_db = st.toggle("🚀 Multi CPU BD", value=False)
        n_cores_db = st.slider("CPU BD", 1, psutil.cpu_count(logical=True), psutil.cpu_count(logical=True)) if super_proc_db else 1

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

        start_btn_db = False
        if st.session_state.get("analise_concluida", False):
            st.markdown("#### Mapeamento de Exceções")
            colunas_ignoradas_db = st.multiselect("Ignorar colunas BD:", options=st.session_state.todas_colunas_disponiveis, default=[])
            start_btn_db = st.button("Iniciar Pipeline de Banco", type="primary", use_container_width=True)
            if start_btn_db: 
                st.session_state.colunas_selecionadas_finais = [col for col in st.session_state.todas_colunas_disponiveis if col not in colunas_ignoradas_db]

    elif st.session_state.view_mode == "Arquivos (.pdf, .csv, .txt)":
        st.markdown("#### Parâmetros de Motor (Arquivos)")
        chunk_size_file = st.number_input("Lote de Memória CSV (Rows)", value=5000, step=1000, help="Quantidade de linhas carregadas na memória por vez.")
        super_proc_file = st.toggle("🚀 Multi CPU Arquivos", value=True)
        n_cores_file = st.slider("CPU Arquivos", 1, psutil.cpu_count(logical=True), max(1, psutil.cpu_count(logical=True)-1)) if super_proc_file else 1


# --------------------------------------------------
# MÓDULO 1: BANCOS DE DADOS
# --------------------------------------------------
if st.session_state.view_mode == "Bancos de Dados":
    st.markdown("### Monitoramento do Pipeline Estruturado")
    
    if start_btn_db:
        save_progress(STATUS_FILE, "Iniciando Thread Fantasma...", 0, 1, 0, 1, 0, 0, finalizado=False)
        threading.Thread(target=run_pipeline_background, args=(db_type, src_cfg, dst_cfg, filter_tables, n_cores_db, chunk_size_db, modo, dicionario_regras, st.session_state.colunas_selecionadas_finais), daemon=True).start()
        time.sleep(2.5); st.rerun()   

    estado_atual = load_progress(STATUS_FILE)
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
# MÓDULO 2: ARQUIVOS
# --------------------------------------------------
elif st.session_state.view_mode == "Arquivos (.pdf, .csv, .txt)":
    st.markdown("### Processamento de Arquivos em Lote Desacoplado")
    st.markdown("Evita bloqueios na interface e estouros de RAM através de processamento em background (Fatiamento de Chunks).")
    
    if not st.session_state.file_process_started:
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
                        colunas_ignoradas_csv = st.multiselect("Ignorar colunas:", options=sorted(list(todas_colunas_unicas)), default=[])
                except Exception as e:
                    st.warning(f"Não foi possível ler as colunas prévias ({e}).")

            if st.button("🚀 Iniciar Esteira de Blindagem em Lote", type="primary", use_container_width=True):
                st.session_state.file_process_started = True
                payload = [{"name": a.name, "ext": a.name.split('.')[-1].lower(), "bytes": a.getvalue()} for a in arquivos]
                
                save_progress(FILE_STATUS_FILE, "Iniciando Thread Fantasma...", 0, len(payload), 0, 100, 0, 0, False)
                threading.Thread(
                    target=run_files_pipeline_background, 
                    args=(payload, formato_saida, dicionario_regras, colunas_ignoradas_csv, chunk_size_file, n_cores_file), 
                    daemon=True
                ).start()
                time.sleep(1)
                st.rerun()

    if st.session_state.file_process_started:
        estado_arquivos = load_progress(FILE_STATUS_FILE)
        if estado_arquivos:
            if not estado_arquivos.get("finalizado", True):
                st.info("🔄 A esteira de arquivos está rodando em background. A interface está liberada.")
                if st.button("🔄 Atualizar Progresso", type="primary"): st.rerun()
                
                progresso_arq = (estado_arquivos['tabelas_processadas'] / max(estado_arquivos['tabelas_total'], 1))
                st.progress(progresso_arq if progresso_arq > 0 else 0.05)
                
                c1, c2, c3 = st.columns(3)
                c1.metric("Status Atual", estado_arquivos['fase'])
                c2.metric("Arquivos Concluídos", f"{estado_arquivos['tabelas_processadas']}/{estado_arquivos['tabelas_total']}")
                c3.metric("Velocidade (CSV)", f"{estado_arquivos['velocidade']:,.0f} reg/s" if estado_arquivos['velocidade'] > 0 else "-")
                
                time.sleep(2); st.rerun()
            else:
                if "Erro" in estado_arquivos.get("fase", ""): 
                    st.error(f"🚨 Falha no Processamento: {estado_arquivos['fase']}")
                else: 
                    st.success("✅ Esteira de Arquivos Concluída com Sucesso!")
                    
                    if os.path.exists("resultado_lote.zip"):
                        with open("resultado_lote.zip", "rb") as f:
                            zip_data = f.read()
                            
                        st.download_button(
                            label="📦 Baixar Lote Blindado (.ZIP)",
                            data=zip_data,
                            file_name="Arquivos_Seguros_Aegis.zip",
                            mime="application/zip",
                            type="primary",
                            use_container_width=True
                        )
                
                if st.button("🧹 Limpar Painel e Processar Novo Lote"): 
                    limpar_sessao(); st.rerun()

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
        with st.spinner("Varrendo PIIs..."):
            texto_limpo = anonymizer.process_raw_text(texto_original, dicionario_regras)
            placeholder_txt.markdown(f'<div class="texto-seguro">{html.escape(texto_limpo)}</div>', unsafe_allow_html=True)