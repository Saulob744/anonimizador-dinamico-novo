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
from collections import Counter
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

def generate_dynamic_code(original_str):
    rng = random.Random(original_str)
    return "".join(
        rng.choice(string.ascii_uppercase) if c.isupper() else
        rng.choice(string.ascii_lowercase) if c.islower() else
        rng.choice(string.digits) if c.isdigit() else c
        for c in original_str
    )

def split_text_into_chunks(text, max_tokens=300):
    words = text.split()
    if len(words) <= max_tokens:
        return [text]
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
def process_chunk_parallel(rows, modo, anon_geo, target_columns, pre_decisions=None):
    if modo != "🛡️ Anonimização Total" or not rows:
        return rows

    # Regex Utilitárias Fixas
    gps_pair = re.compile(r"^\s*(-?\d{1,3}\.\d{4,})\s*,\s*(-?\d{1,3}\.\d{4,})\s*$")
    gps_single = re.compile(r"^\s*(-?\d{1,3}\.\d{4,})\s*$")
    uuid_regex = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

    # Regex para o Escudo Dinâmico
    html_regex = re.compile(r"<[^>]+>|&[a-zA-Z0-9#]+;")

    processed = []
    for r in rows:
        row_dict = dict(r)

        for col, old in row_dict.items():
            if old is None or type(old).__name__ in ['date', 'datetime', 'Timestamp', 'bool', 'int', 'float']:
                continue

            old_str = str(old).strip()

            if uuid_regex.match(old_str):
                continue 

            # VERIFICAÇÃO PRINCIPAL: Se não está nas colunas alvo, pula!
            if not target_columns or col not in target_columns:
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
                # --- 🛡️ INÍCIO DO ESCUDO (COM ESPAÇAMENTOS) ---
                vault = {}
                def hide(match):
                    token = f"__SHLD{len(vault)}__"
                    vault[token] = match.group(0)
                    # O SEGREDO: Adicionamos espaços antes e depois do token!
                    return f" {token} "

                safe_text = old_str
                safe_text = html_regex.sub(hide, safe_text)
                
                # Limpa espaços duplos criados pelo escudo para a IA ler perfeitamente
                safe_text = re.sub(r'\s+', ' ', safe_text).strip()
                
                # --- 🤖 IA PROCESSA APENAS O TEXTO SEGURO ---
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
                                    print(f"🕵️ [TROCA IA | {col}] {termo_original} ➡️ {termo_falso}")
                    
                    anon_chunks.append(str_new_val)
                
                final_text = " ".join(anon_chunks)

                # --- 🔓 DESATIVA O ESCUDO ---
                for token, original in vault.items():
                    # Tenta remover com os espaços de segurança primeiro, se não achar, tira só o token
                    final_text = final_text.replace(f" {token} ", original).replace(token, original)

                row_dict[col] = final_text

            except Exception as e:
                print(f"[DEBUG GLINER] Falha ao processar texto na coluna '{col}': {e}")
                row_dict[col] = old_str 

        processed.append(row_dict)

    return processed

# ==================================================
# ESTADO DA UI & MENU LATERAL
# ==================================================
if "colunas_para_anonimizar" not in st.session_state:
    st.session_state.colunas_para_anonimizar = []
if "analise_concluida" not in st.session_state:
    st.session_state.analise_concluida = False
if "colunas_selecionadas_finais" not in st.session_state:
    st.session_state.colunas_selecionadas_finais = []
if "todas_colunas_disponiveis" not in st.session_state:
    st.session_state.todas_colunas_disponiveis = []

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
            
            sugeridas_set = set()
            todas_set = set()
            
            if schemas:
                for schema in schemas: # Percorre TODOS os schemas
                    tables = db_utils.get_tables(src_engine, schema)
                    valid_tables = [t for t in tables if not allowed or t in allowed]
                    
                    for table in valid_tables: # Percorre TODAS as tabelas permitidas
                        chunk_gen = db_utils.fetch_rows_streaming(src_engine, table, schema, 200)
                        primeiro_lote = next(chunk_gen, [])
                        
                        if primeiro_lote:
                            rows_dict = [dict(r) for r in primeiro_lote]
                            colunas_tabela = list(rows_dict[0].keys())
                            
                            for col in colunas_tabela:
                                todas_set.add(col)
                                valores = [str(r.get(col, "")) for r in rows_dict if r.get(col) is not None]
                                if anonymizer.should_anonymize_column(col, valores):
                                    sugeridas_set.add(col)
            
            todas = sorted(list(todas_set))
            sugeridas = sorted(list(sugeridas_set))
            
            st.session_state.todas_colunas_disponiveis = todas if todas else []
            st.session_state.colunas_para_anonimizar = sugeridas if sugeridas else []
            st.session_state.analise_concluida = True
            st.success(f"✅ Análise concluída! Encontradas {len(todas)} colunas distintas no banco.")
        except Exception as e:
            st.error(f"Erro ao analisar banco: {e}")

    if st.session_state.analise_concluida:
        st.markdown("### Selecione as Exceções")
        st.info("⚠️ Todas as colunas textuais serão anonimizadas por padrão. Escolha abaixo apenas as que devem ser **IGNORADAS** pelo sistema.")
        
        opcoes_validas = st.session_state.todas_colunas_disponiveis
        
        # O usuário agora escolhe o que NÃO vai anonimizar
        colunas_ignoradas = st.multiselect(
            "Colunas que NÃO serão alteradas:",
            options=opcoes_validas, 
            default=[] # Por padrão, não ignora nada
        )
        
        # A matemática da inversão: O que vai pro pipeline é Tudo MENOS o que foi ignorado
        colunas_finais = [col for col in opcoes_validas if col not in colunas_ignoradas]
        
        start_btn = st.button("2. INICIAR PROCESSAMENTO", type="primary", use_container_width=True)
        if start_btn:
            st.session_state.colunas_selecionadas_finais = colunas_finais

# ==================================================
# PIPELINE
# ==================================================
st.title("🛡️ Pipeline De Proteção De Dados")
debug_box = st.empty() 
progress_bar = st.progress(0)
status = st.empty()
metric_placeholder = st.empty()

def run_pipeline():
    proc = psutil.Process(os.getpid())
    t0_global = time.time()
    current_phase = 0
    phase_total = 4
    
    target_cols = st.session_state.colunas_selecionadas_finais

    def set_phase(title, subtitle=""):
        nonlocal current_phase
        current_phase += 1
        status.info(f"🔷 Fase {current_phase}/{phase_total} • {title}")
        progress_bar.progress(min((current_phase - 1) / phase_total * 0.25, 0.25))

    set_phase("Conectando bancos de dados", "estabelecendo conexões")
    src_engine = db_utils.connect(build_url(db_type, **src_cfg))
    dst_engine = db_utils.connect(build_url(db_type, **dst_cfg))
    db_utils.set_replication_mode(dst_engine, "replica")

    set_phase("Mapeando estruturas", "schemas e tabelas")
    if modo == "🛡️ Anonimização Total":
        try:
            anonymizer.get_gliner() # Se você estiver usando o GLiNER
            status.success("🧠 IA carregada com sucesso")
        except Exception as e:
            debug_box.error(f"🚨 Falha ao carregar IA: {e}")

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
        debug_box.warning("⚠️ Nenhuma linha encontrada nas tabelas de origem! Verifique o banco fonte.")
        return

    set_phase("Preparando destino", "limpeza de tabelas")
    for s, t, _ in reversed(work_list):
        db_utils.truncate_table(dst_engine, t, s)

    set_phase("Executando pipeline", "processamento e carga")
    total_rows, processed_tables, last_ui_update = 0, 0, 0
    weighted_speed_samples = []

    with ProcessPoolExecutor(max_workers=n_cores) as executor:
        for s, t, t_count in work_list:
            processed_tables += 1
            table_rows_processed = 0

            # --- 🛡️ AJUSTE PARA ORDENAÇÃO EXATA ---
            # Identifica a PK para forçar o ORDER BY no banco
            info = db_utils.get_table_info(src_engine, t, s)
            pks = info.get("primary_keys", [])
            pk_col = pks[0] if pks else None 
            # -------------------------------------

            # Passamos o pk_col para o streaming
            for chunk in db_utils.fetch_rows_streaming(src_engine, t, s, chunk_size, order_by=pk_col):
                chunk_start = time.time()
                rows = [dict(r) for r in chunk]
                
                if modo == "🛡️ Anonimização Total":
                    if n_cores > 1:
                        sub_sz = max(1, len(rows) // n_cores)
                        sub_chunks = [rows[i:i + sub_sz] for i in range(0, len(rows), sub_sz)]
                        
                        futures = [
                            executor.submit(process_chunk_parallel, sub_chunk, modo, anon_geo, target_cols)
                            for sub_chunk in sub_chunks
                        ]
                        
                        rows = []
                        for original_chunk, f in zip(sub_chunks, futures):
                            try: 
                                result = f.result()
                                if result: rows.extend(result)
                                else: rows.extend(original_chunk)
                            except Exception as e: 
                                debug_box.error(f"🚨 Erro crítico no worker. Erro: {e}")
                                rows.extend(original_chunk)
                    else:
                        try:
                            res = process_chunk_parallel(rows, modo, anon_geo, target_cols)
                            if res: rows = res
                        except Exception as e:
                            debug_box.error(f"🚨 Erro na IA. Erro: {e}")

                if rows:
                    db_utils.insert_rows(dst_engine, t, s, rows)

                inserted_count = len(rows)
                total_rows += inserted_count
                table_rows_processed += inserted_count

                # Atualização de métricas (mantido o original)
                chunk_speed = inserted_count / max(time.time() - chunk_start, 0.001)
                weighted_speed_samples.append(chunk_speed)
                if len(weighted_speed_samples) > 30: weighted_speed_samples.pop(0)

                now = time.time()
                if now - last_ui_update > 1:
                    elapsed = now - t0_global
                    stable_speed = sum(weighted_speed_samples) / len(weighted_speed_samples) if weighted_speed_samples else 0
                    total_progress = min(0.25 + ((total_rows / max(total_estimated, 1)) * 0.75), 1.0)
                    
                    metric_placeholder.markdown(f"""
                    ### 📊 Progresso do Pipeline
                    - 📂 Tabela: **{processed_tables}/{total_tables}**
                    - 🧮 Registros: **{total_rows:,} / {total_estimated:,}**
                    - ⚡ Velocidade: **{stable_speed:,.0f} linhas/s**
                    - ⏱️ Tempo: **{elapsed:.1f}s**
                    """)
                    progress_bar.progress(total_progress)
                    last_ui_update = now

    db_utils.set_replication_mode(dst_engine, "origin")
    progress_bar.progress(1.0)
    status.success(f"✅ Pipeline concluído • {total_rows:,} linhas em {time.time() - t0_global:.2f}s")

if start_btn:
    try:
        run_pipeline()
        st.balloons()
    except Exception as e:
        debug_box.error(f"🚨 Erro Fatal no Pipeline: {e}")