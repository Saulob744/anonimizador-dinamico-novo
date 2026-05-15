import streamlit as st
import re
import random
import string
import logging
import warnings
import importlib
import urllib.parse
import time
import psutil
import os
import pyodbc
from concurrent.futures import ProcessPoolExecutor
import difflib

import db_utils
import anonymizer

# ==================================================
# CONFIGURAÇÕES INICIAIS
# ==================================================
logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", category=UserWarning, message=".*resume_download.*")

importlib.reload(db_utils)
importlib.reload(anonymizer)

st.set_page_config(page_title="🛡️ Aegis Anonymizer Pro", page_icon="🛡️", layout="wide")
st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.8rem; color: #00ffcc; font-weight: bold; }
    .stProgress .st-at { background-color: #00ffcc; }
    .debug-box { border: 1px solid #ff4444; padding: 10px; border-radius: 5px; color: #ff4444; background-color: #ffe6e6; }
    .var-display { background-color: #1e1e2e; padding: 15px; border-radius: 8px; margin-bottom: 20px; border-left: 5px solid #00ffcc; }
    </style>
""", unsafe_allow_html=True)

# ==================================================
# FUNÇÕES AUXILIARES
# ==================================================
def apply_gps_jitter(coord_str):
    try:
        coord_str = str(coord_str).strip()
        if "." in coord_str or "," in coord_str:
            c = float(coord_str.replace(",", "."))
            return f"{c + random.uniform(-0.003, 0.003):.6f}"
        else:
            c = int(coord_str)
            return str(c + random.randint(-300, 300)) 
    except ValueError:
        return coord_str

def split_text_into_chunks(text, max_tokens=300):
    words = text.split()
    if len(words) <= max_tokens: return [text]
    return [" ".join(words[i : i + max_tokens]) for i in range(0, len(words), max_tokens)]

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
# PROCESSAMENTO CENTRAL
# ==================================================
def process_chunk_parallel(rows, modo, anon_geo, target_columns, col_profiles=None):
    if modo != "🛡️ Anonimização Total" or not rows: return rows
    if col_profiles is None: col_profiles = {}

    uuid_regex = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    html_regex = re.compile(r"<[^>]+>|&[a-zA-Z0-9#]+;")

    processed = []
    for r in rows:
        row_dict = dict(r)

        for col, old in row_dict.items():
            if old is None or type(old).__name__ in ['date', 'datetime', 'Timestamp', 'bool']:
                continue

            old_str = str(old).strip()
            if not old_str or uuid_regex.match(old_str): continue 
            if not target_columns or col not in target_columns: continue
                        
            tipo_perfil = col_profiles.get(col, "DESCONHECIDO")

            try:
                # ⚡ DESVIO EXPRESSO (FAST-PATH)
                if tipo_perfil in ["GPS", "GPS_SINGLE"]:
                    if not anon_geo: continue 
                    if "," in old_str:
                        partes = old_str.split(",")
                        if len(partes) >= 2: row_dict[col] = f"{apply_gps_jitter(partes[0])}, {apply_gps_jitter(partes[1])}"
                    else:
                        row_dict[col] = apply_gps_jitter(old_str)
                    print(f"⚡ [FAST-PATH | {col}] GPS Ocultado")
                    continue
                    
                elif tipo_perfil in ["CPF", "RG", "PLACA", "EMAIL", "NOME_SOLTO", "RENAVAM", "MATRICULA", "PHONE"]:
                    faker_key = tipo_perfil
                    if tipo_perfil == "PLACA": faker_key = "PLATE"
                    elif tipo_perfil == "NOME_SOLTO": faker_key = "PER"

                    row_dict[col] = anonymizer._get_fake(old_str, faker_key)
                    print(f"⚡ [FAST-PATH | {col}] {old_str} ➡️ {row_dict[col]}")
                    continue

                # 🛡️ TEXTO LIVRE E DESCONHECIDOS (PENTE FINO COM IA)
                vault = {}
                def hide(match):
                    token = f"__SHLD{len(vault)}__"
                    vault[token] = match.group(0)
                    return f" {token} "

                safe_text = html_regex.sub(hide, old_str)
                safe_text = re.sub(r'\s+', ' ', safe_text).strip()
                
                chunks = split_text_into_chunks(safe_text)
                anon_chunks = []
                
                for chunk in chunks:
                    new_val, flag = anonymizer.anonymize_value(col, chunk, anon_location=anon_geo)
                    str_new_val = str(new_val)
                    
                    if str_new_val != chunk:
                        seq = difflib.SequenceMatcher(None, chunk.split(), str_new_val.split())
                        for tag, i1, i2, j1, j2 in seq.get_opcodes():
                            if tag == 'replace':
                                termo_original = " ".join(chunk.split()[i1:i2])
                                termo_falso = " ".join(str_new_val.split()[j1:j2])
                                if "__SHLD" not in termo_original:
                                    print(f"🕵️ [IA | {col}] {termo_original} ➡️ {termo_falso}")
                    anon_chunks.append(str_new_val)
                
                final_text = " ".join(anon_chunks)
                for token, original in vault.items():
                    final_text = final_text.replace(f" {token} ", original).replace(token, original)

                row_dict[col] = final_text

            except Exception as e:
                # ⚡ AJUSTE: Não engolir mais erros fatais! Se a IA (anonymizer) colapsar, para tudo e avisa.
                raise RuntimeError(f"Falha Crítica ao processar coluna '{col}'. Detalhe do Erro: {e}")

        processed.append(row_dict)
    return processed

# ==================================================
# ESTADO DA UI & MENU LATERAL
# ==================================================
if "analise_concluida" not in st.session_state: st.session_state.analise_concluida = False
if "todas_colunas_disponiveis" not in st.session_state: st.session_state.todas_colunas_disponiveis = []
if "colunas_ignoradas" not in st.session_state: st.session_state.colunas_ignoradas = []
if "colunas_perfiladas" not in st.session_state: st.session_state.colunas_perfiladas = {}
if "colunas_selecionadas_finais" not in st.session_state: st.session_state.colunas_selecionadas_finais = []

if "ultimo_progresso" not in st.session_state: st.session_state.ultimo_progresso = 0.0
if "ultimo_status_msg" not in st.session_state: st.session_state.ultimo_status_msg = ""
if "ultimo_status_tipo" not in st.session_state: st.session_state.ultimo_status_tipo = "info"
if "ultima_metrica" not in st.session_state: st.session_state.ultima_metrica = ""

start_btn = False

with st.sidebar:
    st.title("🛡️ Aegis Control")
    db_type = st.selectbox("Banco de Dados", ["postgresql", "mysql", "mssql"])

    aba_origem, aba_destino = st.tabs(["🔴 Origem", "🟢 Destino"])
    
    def render_db_form(prefix):
        return {
            "host": st.text_input("Host", value="localhost", key=f"{prefix}_host", autocomplete="off"),
            "port": st.text_input("Porta", key=f"{prefix}_port", placeholder="Ex: 5432", autocomplete="off"),
            "db": st.text_input("Banco", key=f"{prefix}_db", placeholder="Nome do banco", autocomplete="off"),
            "user": st.text_input("Usuário", key=f"{prefix}_user", autocomplete="new-password"),
            "password": st.text_input("Senha", type="password", key=f"{prefix}_pass", autocomplete="new-password")
        }

    with aba_origem: src_cfg = render_db_form("origem")
    with aba_destino: dst_cfg = render_db_form("destino")

    modo = st.selectbox("Modo", ["🛡️ Anonimização Total", "⚡ Cópia Direta"])
    chunk_size = st.number_input("Chunk", value=1000, step=1000) 
    filter_tables = st.text_input("Filtrar tabelas (vírgula)")
    anon_geo = st.toggle("Mascara De GPS", value=True)
    ignorar_duplicatas = st.toggle("🔄 Ignorar Duplicatas", value=True)
    super_proc = st.toggle("🚀 Multi CPU", value=False)
    n_cores = st.slider("CPU", 1, db_utils.get_cpu_info(), db_utils.get_cpu_info()) if super_proc else 1

    st.divider()
    btn_analisar = st.button("1. Analisar Estrutura", use_container_width=True)
    
    if btn_analisar:
        with st.spinner("Analisando banco e perfilando colunas com IA..."):
            try:
                src_engine = db_utils.connect(build_url(db_type, **src_cfg))
                schemas = db_utils.get_user_schemas(src_engine)
                allowed = [t.strip() for t in filter_tables.split(",")] if filter_tables else []
                
                todas_set = set()
                perfis_detectados = {}
                
                if schemas:
                    for schema in schemas:
                        tables = db_utils.get_tables(src_engine, schema)
                        valid_tables = [t for t in tables if not allowed or t in allowed]
                        
                        for table in valid_tables:
                            chunk_gen = db_utils.fetch_rows_streaming(src_engine, table, schema, 100)
                            primeiro_lote = next(chunk_gen, [])
                            
                            if primeiro_lote:
                                rows_dict = [dict(r) for r in primeiro_lote]
                                colunas_tabela = list(rows_dict[0].keys())
                                
                                for col in colunas_tabela:
                                    todas_set.add(col)
                                    valores = [r.get(col) for r in rows_dict if r.get(col) is not None]
                                    
                                    tipo_dado = anonymizer.profile_column_type(col, valores)
                                    
                                    if tipo_dado != "IGNORAR":
                                        perfis_detectados[col] = tipo_dado
                
                todas = sorted(list(todas_set))
                st.session_state.todas_colunas_disponiveis = todas if todas else []
                st.session_state.colunas_perfiladas = perfis_detectados
                st.session_state.analise_concluida = True
                st.success(f"✅ Encontradas {len(todas)} colunas. {len(perfis_detectados)} alvos mapeados.")
            except Exception as e:
                # ⚡ AJUSTE: O erro de análise agora sobe em um balão de erro formatado
                st.error(f"🚨 Falha na Fase de Análise!\nVerifique se o banco de origem está acessível.\nDetalhe do erro: {e}")

    if st.session_state.analise_concluida:
        st.markdown("### Selecione as Exceções")
        st.info("⚠️ Escolha apenas as colunas que devem ser **IGNORADAS** pelo sistema.")
        
        opcoes_validas = st.session_state.todas_colunas_disponiveis
        sugeridas_ignorar = [c for c in opcoes_validas if c not in st.session_state.colunas_perfiladas]
        
        colunas_ignoradas = st.multiselect("NÃO serão alteradas:", options=opcoes_validas, default=sugeridas_ignorar)
        st.session_state.colunas_ignoradas = colunas_ignoradas
        
        colunas_finais = [col for col in opcoes_validas if col not in colunas_ignoradas]
        
        start_btn = st.button("2. INICIAR PROCESSAMENTO", type="primary", use_container_width=True)
        if start_btn:
            st.session_state.colunas_selecionadas_finais = colunas_finais
            st.session_state.ultimo_progresso = 0.0
            st.session_state.ultimo_status_msg = ""
            st.session_state.ultima_metrica = ""

# ==================================================
# UI PRINCIPAL & PIPELINE
# ==================================================
st.title("🛡️ Pipeline De Proteção De Dados")

if st.session_state.analise_concluida:
    with st.expander("👁️ Ver Perfilamento da IA", expanded=True):
        st.markdown("<div class='var-display'>", unsafe_allow_html=True)
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Lidas", len(st.session_state.todas_colunas_disponiveis))
        col2.metric("Ignoradas", len(st.session_state.colunas_ignoradas))
        
        qtd_alvo = len(st.session_state.todas_colunas_disponiveis) - len(st.session_state.colunas_ignoradas)
        col3.metric("A Processar", qtd_alvo)
        
        st.markdown("**🎯 Tipos Detectados:**")
        if qtd_alvo > 0:
            for c in st.session_state.todas_colunas_disponiveis:
                tipo = st.session_state.colunas_perfiladas.get(c, "IGNORADO")
                cor = "green" if tipo in ["CPF", "RG", "NOME_SOLTO", "EMAIL", "PLACA", "RENAVAM", "MATRICULA", "GPS", "GPS_SINGLE", "PHONE"] else "orange" if tipo == "TEXTO_LIVRE" else "gray"
                st.markdown(f"- **{c}**: <span style='color:{cor}; font-weight:bold;'>[{tipo}]</span>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

debug_box = st.empty() 

progress_bar = st.progress(st.session_state.ultimo_progresso)

status = st.empty()
if st.session_state.ultimo_status_msg:
    if st.session_state.ultimo_status_tipo == "info":
        status.info(st.session_state.ultimo_status_msg)
    else:
        status.success(st.session_state.ultimo_status_msg)

metric_placeholder = st.empty()
if st.session_state.ultima_metrica:
    metric_placeholder.markdown(st.session_state.ultima_metrica)

def run_pipeline():
    proc = psutil.Process(os.getpid())
    t0_global = time.time()
    current_phase = 0
    phase_total = 4
    
    target_cols = st.session_state.colunas_selecionadas_finais
    perfis = st.session_state.colunas_perfiladas

    def set_phase(title, subtitle=""):
        nonlocal current_phase
        current_phase += 1
        msg = f"🔷 Fase {current_phase}/{phase_total} • {title} ({subtitle})"
        
        st.session_state.ultimo_status_msg = msg
        st.session_state.ultimo_status_tipo = "info"
        status.info(msg)
        
        novo_prog = min((current_phase - 1) / phase_total * 0.25, 0.25)
        st.session_state.ultimo_progresso = novo_prog
        progress_bar.progress(novo_prog)

    set_phase("Conectando bancos", "estabelecendo conexões")
    src_engine = db_utils.connect(build_url(db_type, **src_cfg))
    dst_engine = db_utils.connect(build_url(db_type, **dst_cfg))
    db_utils.set_replication_mode(dst_engine, "replica")

    set_phase("Mapeando estruturas", "schemas e tabelas")
    if modo == "🛡️ Anonimização Total": anonymizer.reset_memory() 

    schemas = db_utils.get_user_schemas(src_engine)
    allowed = [t.strip() for t in filter_tables.split(",")] if filter_tables else []
    work_list, total_estimated, total_tables = [], 0, 0

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
        debug_box.warning("⚠️ Nenhuma linha encontrada nas tabelas!")
        return

    set_phase("Preparando destino", "limpeza")
    for s, t, _ in reversed(work_list): db_utils.truncate_table(dst_engine, t, s)

    set_phase("Executando pipeline", "processamento e carga")
    total_rows, processed_tables, last_ui_update = 0, 0, 0
    weighted_speed_samples = []

    with ProcessPoolExecutor(max_workers=n_cores) as executor:
        for s, t, t_count in work_list:
            processed_tables += 1
            info = db_utils.get_table_info(src_engine, t, s)
            pk_col = info.get("primary_keys", [None])[0]

            for chunk in db_utils.fetch_rows_streaming(src_engine, t, s, chunk_size, order_by=pk_col):
                chunk_start = time.time()
                rows = [dict(r) for r in chunk]
                
                if modo == "🛡️ Anonimização Total":
                    if n_cores > 1:
                        sub_sz = max(1, len(rows) // n_cores)
                        sub_chunks = [rows[i:i + sub_sz] for i in range(0, len(rows), sub_sz)]
                        futures = [executor.submit(process_chunk_parallel, sub_chunk, modo, anon_geo, target_cols, perfis) for sub_chunk in sub_chunks]
                        rows = []
                        for original_chunk, f in zip(sub_chunks, futures):
                            try: 
                                rows.extend(f.result() or original_chunk)
                            except Exception as e: 
                                # ⚡ AJUSTE: Trabalhadores (workers) agora gritam o erro para parar o pipeline,
                                # em vez de inserir os dados sem anonimizar por debaixo dos panos.
                                raise RuntimeError(f"Quebra em Processamento Paralelo na tabela '{t}': {e}")
                    else:
                        rows = process_chunk_parallel(rows, modo, anon_geo, target_cols, perfis) or rows

                if rows: db_utils.insert_rows(dst_engine, t, s, rows, ignore_conflicts=ignorar_duplicatas)

                inserted_count = len(rows)
                total_rows += inserted_count

                chunk_speed = inserted_count / max(time.time() - chunk_start, 0.001)
                weighted_speed_samples.append(chunk_speed)
                if len(weighted_speed_samples) > 30: weighted_speed_samples.pop(0)

                now = time.time()
                if now - last_ui_update > 1:
                    elapsed = now - t0_global
                    stable_speed = sum(weighted_speed_samples) / len(weighted_speed_samples) if weighted_speed_samples else 0
                    total_progress = min(0.25 + ((total_rows / max(total_estimated, 1)) * 0.75), 1.0)
                    
                    msg_metrica = f"### 📊 Progresso\n- 📂 Tabela: **{processed_tables}/{total_tables}**\n- 🧮 Registros: **{total_rows:,} / {total_estimated:,}**\n- ⚡ Vel: **{stable_speed:,.0f} linhas/s**"
                    
                    st.session_state.ultima_metrica = msg_metrica
                    st.session_state.ultimo_progresso = total_progress
                    
                    metric_placeholder.markdown(msg_metrica)
                    progress_bar.progress(total_progress)
                    last_ui_update = now

    db_utils.set_replication_mode(dst_engine, "origin")
    
    st.session_state.ultimo_progresso = 1.0
    progress_bar.progress(1.0)
    
    msg_sucesso = f"✅ Pipeline concluído • {total_rows:,} linhas em {time.time() - t0_global:.2f}s"
    st.session_state.ultimo_status_msg = msg_sucesso
    st.session_state.ultimo_status_tipo = "success"
    status.success(msg_sucesso)

if start_btn:
    if src_cfg["db"] == src_cfg["user"] or dst_cfg["db"] == dst_cfg["user"]:
        st.warning("⚠️ Atenção: O nome do banco de dados está idêntico ao nome de usuário. O seu navegador pode ter preenchido isso automaticamente por engano. Por favor, verifique os campos 'Banco' nas abas Origem e Destino.")
    elif not src_cfg["db"] or not dst_cfg["db"]:
        st.warning("⚠️ Atenção: O campo 'Banco' não pode ficar vazio.")
    else:
        try:
            run_pipeline()
            st.balloons()
        except Exception as e:
            error_str = str(e).lower()
            
            if "pg_hba.conf rejects" in error_str:
                debug_box.error(
                    "🚨 Acesso Negado pelo Banco de Dados!\n\n"
                    "O servidor rejeitou a conexão. O usuário está sem permissão "
                    "ou o nome do 'Banco' digitado está errado (talvez preenchido sozinho pelo navegador)."
                )
            elif "database" in error_str and "does not exist" in error_str:
                debug_box.error(
                    "🚨 Banco de Dados não encontrado!\n\n"
                    "Verifique se você digitou o nome do Banco corretamente nas abas de conexão."
                )
            else:
                
                debug_box.error(f"🚨 INTERRUPÇÃO DO PIPELINE:\n\n{e}")