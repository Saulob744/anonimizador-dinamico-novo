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
import traceback
from concurrent.futures import ProcessPoolExecutor
import difflib
import pandas as pd

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
    .stProgress .st-at { background-color: #00ffcc; transition: width 0.5s ease-in-out; }
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
# PROCESSAMENTO CENTRAL BLINDADO
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
                # --- 1. APLICAÇÃO DA ANONIMIZAÇÃO ---
                if tipo_perfil in ["GPS", "GPS_SINGLE"]:
                    if not anon_geo: continue 
                    if "," in old_str:
                        partes = old_str.split(",")
                        if len(partes) >= 2: row_dict[col] = f"{apply_gps_jitter(partes[0])}, {apply_gps_jitter(partes[1])}"
                    else:
                        row_dict[col] = apply_gps_jitter(old_str)
                    continue 
                    
                elif tipo_perfil in ["CPF", "RG", "PLACA", "EMAIL", "NOME_SOLTO", "RENAVAM", "MATRICULA", "PHONE"]:
                    faker_key = tipo_perfil
                    if tipo_perfil == "PLACA": faker_key = "PLATE"
                    elif tipo_perfil == "NOME_SOLTO": faker_key = "PER"

                    row_dict[col] = str(anonymizer._get_fake(old_str, faker_key))

                else:
                    # TEXTO LIVRE E DESCONHECIDOS (PENTE FINO COM IA)
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
                        new_val, flag = anonymizer.anonymize_value(col, chunk, anon_location=anon_geo, usar_agente_revisor=True)
                        anon_chunks.append(str(new_val))
                    
                    final_text = " ".join(anon_chunks)
                    for token, original in vault.items():
                        final_text = final_text.replace(f" {token} ", original).replace(token, original)

                    row_dict[col] = final_text

                # --- 2. ESCUDO DE INTEGRIDADE GLOBAL (ANTI-QUEBRA DO BANCO) ---
                old_val_str = str(old).strip()
                new_val_str = str(row_dict[col])

                if re.match(r'^-?\d+$', old_val_str):
                    is_negative = old_val_str.startswith('-')
                    clean_num = re.sub(r'\D', '', new_val_str)
                    if not clean_num: clean_num = "0"
                    
                    max_len = len(re.sub(r'\D', '', old_val_str))
                    if len(clean_num) > max_len:
                        clean_num = clean_num[:max_len]
                        
                    final_str = f"-{clean_num}" if is_negative else clean_num
                    row_dict[col] = int(final_str) if isinstance(old, int) else final_str

                elif re.match(r'^-?\d+\.\d+$', old_val_str):
                    try:
                        val_float = float(new_val_str)
                        row_dict[col] = val_float if isinstance(old, float) else str(val_float)
                    except ValueError:
                        row_dict[col] = old

            except Exception as e:
                # REDE DE SEGURANÇA FINAL
                print(f"⚠️ [IGNORADO] Erro severo na coluna '{col}'. Dado original mantido. Motivo: {e}")
                row_dict[col] = old

        processed.append(row_dict)
    return processed

# ==================================================
# ESTADO DA UI & MENU LATERAL
# ==================================================
# --- Estados base ---
if "is_running" not in st.session_state: st.session_state.is_running = False
if "db_mapeado" not in st.session_state: st.session_state.db_mapeado = False
if "lista_schemas" not in st.session_state: st.session_state.lista_schemas = []
if "mapa_tabelas" not in st.session_state: st.session_state.mapa_tabelas = {}

# --- Estados da Análise ---
if "analise_concluida" not in st.session_state: st.session_state.analise_concluida = False
if "schema_alvo" not in st.session_state: st.session_state.schema_alvo = None
if "tabelas_alvo" not in st.session_state: st.session_state.tabelas_alvo = []
if "todas_colunas_disponiveis" not in st.session_state: st.session_state.todas_colunas_disponiveis = []
if "colunas_ignoradas" not in st.session_state: st.session_state.colunas_ignoradas = []
if "colunas_perfiladas" not in st.session_state: st.session_state.colunas_perfiladas = {}
if "amostra_crua" not in st.session_state: st.session_state.amostra_crua = []
if "colunas_selecionadas_finais" not in st.session_state: st.session_state.colunas_selecionadas_finais = []

# --- Estados do Pipeline ---
if "ultimo_progresso" not in st.session_state: st.session_state.ultimo_progresso = 0.0
if "ultimo_status_msg" not in st.session_state: st.session_state.ultimo_status_msg = ""
if "ultimo_status_tipo" not in st.session_state: st.session_state.ultimo_status_tipo = "info"
if "ultima_metrica" not in st.session_state: st.session_state.ultima_metrica = ""

with st.sidebar:
    st.title("🛡️ Aegis Control")
    db_type = st.selectbox("Banco de Dados", ["postgresql", "mysql", "mssql"], disabled=st.session_state.is_running)

    aba_origem, aba_destino = st.tabs(["🔴 Origem", "🟢 Destino"])
    # AGENTE IA
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 🤖 Modo Agente IA")
    usar_agente = st.sidebar.checkbox("Ativar Agente Revisor (Mais preciso, porém mais lento)", value=False)
    
    def render_db_form(prefix):
        return {
            "host": st.text_input("Host", value="localhost", key=f"{prefix}_host", autocomplete="off", disabled=st.session_state.is_running),
            "port": st.text_input("Porta", key=f"{prefix}_port", placeholder="Ex: 5432", autocomplete="off", disabled=st.session_state.is_running),
            "db": st.text_input("Banco", key=f"{prefix}_db", placeholder="Nome do banco", autocomplete="off", disabled=st.session_state.is_running),
            "user": st.text_input("Usuário", key=f"{prefix}_user", autocomplete="new-password", disabled=st.session_state.is_running),
            "password": st.text_input("Senha", type="password", key=f"{prefix}_pass", autocomplete="new-password", disabled=st.session_state.is_running)
        }

    with aba_origem: src_cfg = render_db_form("origem")
    with aba_destino: dst_cfg = render_db_form("destino")

    modo = st.selectbox("Modo", ["🛡️ Anonimização Total", "⚡ Cópia Direta"], disabled=st.session_state.is_running)
    chunk_size = st.number_input("Chunk", value=1000, step=1000, disabled=st.session_state.is_running) 
    anon_geo = st.toggle("Mascara De GPS", value=True, disabled=st.session_state.is_running)
    ignorar_duplicatas = st.toggle("🔄 Ignorar Duplicatas", value=True, disabled=st.session_state.is_running)
    super_proc = st.toggle("🚀 Multi CPU", value=False, disabled=st.session_state.is_running)
    n_cores = st.slider("CPU", 1, db_utils.get_cpu_info(), db_utils.get_cpu_info(), disabled=st.session_state.is_running) if super_proc else 1

    st.divider()

    # ==========================
    # PASSO 1: CONECTAR E MAPEAR
    # ==========================
    btn_conectar = st.button("🔌 1. Conectar e Mapear", use_container_width=True, disabled=st.session_state.is_running)
    
    if btn_conectar:
        with st.spinner("Conectando e mapeando banco de origem..."):
            try:
                src_engine = db_utils.connect(build_url(db_type, **src_cfg))
                schemas = db_utils.get_user_schemas(src_engine)
                
                mapa = {}
                for s in schemas:
                    tabelas = db_utils.get_tables(src_engine, s)
                    if tabelas: 
                        mapa[s] = tabelas
                        
                st.session_state.lista_schemas = list(mapa.keys())
                st.session_state.mapa_tabelas = mapa
                st.session_state.db_mapeado = True
                st.session_state.analise_concluida = False 
                st.success(f"✅ Sucesso! {len(mapa)} schemas mapeados.")
            except Exception as e:
                st.error(f"Falha na conexão:\n{e}")

    # ==========================
    # PASSO 2: SELEÇÃO GRANULAR
    # ==========================
    schema_selecionado = None
    tabelas_selecionadas = []
    
    if st.session_state.db_mapeado:
        st.markdown("### Seleção de Escopo")
        schema_selecionado = st.selectbox("Selecione o Schema:", options=st.session_state.lista_schemas, disabled=st.session_state.is_running)
        
        if schema_selecionado:
            todas_tabelas_do_schema = st.session_state.mapa_tabelas[schema_selecionado]
            tabelas_selecionadas = st.multiselect(
                "Tabelas Alvo:", 
                options=todas_tabelas_do_schema, 
                default=todas_tabelas_do_schema, 
                help="Selecione quais tabelas desse schema deseja processar.",
                disabled=st.session_state.is_running
            )
            
        st.divider()
        btn_analisar = st.button("🧠 2. Analisar Dados e IA", type="primary", use_container_width=True, disabled=not tabelas_selecionadas or st.session_state.is_running)
        
        if btn_analisar and tabelas_selecionadas:
            with st.spinner("Analisando estrutura e perfilando com IA..."):
                try:
                    src_engine = db_utils.connect(build_url(db_type, **src_cfg))
                    todas_set = set()
                    perfis_detectados = {}
                    amostra_crua_global = [] 
                    
                    for table in tabelas_selecionadas:
                        chunk_gen = db_utils.fetch_rows_streaming(src_engine, table, schema_selecionado, 100)
                        primeiro_lote = next(chunk_gen, [])
                        
                        if primeiro_lote:
                            rows_dict = [dict(r) for r in primeiro_lote]
                            if len(amostra_crua_global) < 5:
                                amostra_crua_global.extend(rows_dict[:5]) 
                                
                            colunas_tabela = list(rows_dict[0].keys())
                            for col in colunas_tabela:
                                todas_set.add(col)
                                valores = [r.get(col) for r in rows_dict if r.get(col) is not None]
                                tipo_dado = anonymizer.profile_column_type(col, valores)
                                if tipo_dado != "IGNORAR":
                                    perfis_detectados[col] = tipo_dado
                    
                    st.session_state.todas_colunas_disponiveis = sorted(list(todas_set))
                    st.session_state.colunas_perfiladas = perfis_detectados
                    st.session_state.amostra_crua = amostra_crua_global[:5] # Limita a 5 linhas pro preview
                    st.session_state.schema_alvo = schema_selecionado
                    st.session_state.tabelas_alvo = tabelas_selecionadas
                    
                    st.session_state.analise_concluida = True
                    st.success("✅ Perfilamento IA concluído!")
                except Exception as e:
                    st.error(f"🚨 Falha na Fase de Análise!\nDetalhe do erro: {e}")

    # ==========================
    # SELEÇÃO DE EXCEÇÕES (IGNORAR COLUNAS)
    # ==========================
    if st.session_state.analise_concluida:
        st.markdown("### Exceções de Segurança")
        opcoes_validas = st.session_state.todas_colunas_disponiveis
        sugeridas_ignorar = [c for c in opcoes_validas if c not in st.session_state.colunas_perfiladas]
        
        colunas_ignoradas = st.multiselect(
            "Ignorar estas colunas:", 
            options=opcoes_validas, 
            default=sugeridas_ignorar,
            disabled=st.session_state.is_running
        )
        st.session_state.colunas_ignoradas = colunas_ignoradas


# ==================================================
# UI PRINCIPAL & DEMONSTRATIVO
# ==================================================
st.title("🛡️ Pipeline De Proteção De Dados")

debug_box = st.empty() 

# -----------------
# AREA DE PREVIEW E START
# -----------------
iniciar_pipeline = False

if st.session_state.analise_concluida:
    with st.expander("👁️ Ver Perfilamento da IA", expanded=False):
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

    # === NOVO: DEMONSTRATIVO VISUAL (PREVIEW) ===
    st.markdown("### 🧪 Demonstrativo de Transformação")
    st.caption(f"Simulação gerada para o schema **{st.session_state.schema_alvo}** e tabelas selecionadas.")
    
    colunas_alvo_final = [c for c in st.session_state.todas_colunas_disponiveis if c not in st.session_state.colunas_ignoradas]

    if st.session_state.amostra_crua:
        try:
            df_antes = pd.DataFrame(st.session_state.amostra_crua)
            
            # Gera a simulação processando a amostra crua
            amostra_anonimizada = process_chunk_parallel(
                rows=st.session_state.amostra_crua,
                modo=modo,
                anon_geo=anon_geo,
                target_columns=colunas_alvo_final,
                col_profiles=st.session_state.colunas_perfiladas
            )
            df_depois = pd.DataFrame(amostra_anonimizada)
            
            aba_antes, aba_depois = st.tabs(["🔴 Dados Originais (Cru)", "🟢 Dados Mascarados (Simulação)"])
            with aba_antes: st.dataframe(df_antes, use_container_width=True)
            with aba_depois: st.dataframe(df_depois, use_container_width=True)
                
        except Exception as e:
            st.warning(f"⚠️ Não foi possível renderizar a simulação. Motivo: {e}")
    else:
        st.info("Nenhuma amostra disponível para esta seleção.")
        
    st.divider()
    
    # O GRANDE BOTÃO DE INÍCIO
    iniciar_pipeline = st.button("🚀 3. INICIAR PROCESSAMENTO NO BANCO", type="primary", use_container_width=True, disabled=st.session_state.is_running)
    
    if iniciar_pipeline:
        st.session_state.colunas_selecionadas_finais = colunas_alvo_final
        st.session_state.ultimo_progresso = 0.0
        st.session_state.ultimo_status_msg = ""
        st.session_state.ultima_metrica = ""

# -----------------
# AREA DE EXECUÇÃO / TELEMETRIA
# -----------------
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

    # ==========================
    # 1. FASE DE CONEXÃO
    # ==========================
    set_phase("Conectando bancos", "estabelecendo conexões")
    src_engine = db_utils.connect(build_url(db_type, **src_cfg))
    dst_engine = db_utils.connect(build_url(db_type, **dst_cfg))
    db_utils.set_replication_mode(dst_engine, "replica")

    # ==========================
    # 2. FASE DE MAPEAMENTO/CÓPIA ESTRUTURA
    # ==========================
    set_phase("Mapeando estruturas", "schemas e tabelas")
    if modo == "🛡️ Anonimização Total": anonymizer.reset_memory() 

    s = st.session_state.schema_alvo
    tables = st.session_state.tabelas_alvo
    work_list, total_estimated, total_tables = [], 0, 0

    # Copia a estrutura apenas do Schema selecionado
    db_utils.copy_schema(src_engine, dst_engine, s)
    
    # Avalia a ordem correta das tabelas selecionadas
    ordered = db_utils.build_dependency_graph(src_engine, tables, s)
    
    for t in ordered:
        count = db_utils.get_table_count(src_engine, t, s)
        work_list.append((s, t, count))
        total_estimated += count
        total_tables += 1

    if total_estimated == 0:
        debug_box.warning("⚠️ Nenhuma linha encontrada nas tabelas selecionadas!")
        return

    # ==========================
    # 3. FASE DE LIMPEZA
    # ==========================
    set_phase("Preparando destino", "limpeza")
    for s, t, _ in reversed(work_list): 
        db_utils.truncate_table(dst_engine, t, s)

    # ==========================
    # 4. FASE DE EXECUÇÃO
    # ==========================
    set_phase("Executando pipeline", "processamento e carga")
    total_rows, processed_tables, last_ui_update = 0, 0, 0
    weighted_speed_samples = []

    with ProcessPoolExecutor(max_workers=n_cores) as executor:
        for s, t, t_count in work_list:
            processed_tables += 1
            info = db_utils.get_table_info(src_engine, t, s)
            
            pks = info.get("primary_keys", [])
            pk_col = pks[0] if pks else None

            # [ETAPA: LEITURA]
            chunk_generator = db_utils.fetch_rows_streaming(src_engine, t, s, chunk_size, order_by=pk_col)

            while True:
                try:
                    chunk = next(chunk_generator)
                except StopIteration:
                    break

                chunk_start = time.time()
                rows = [dict(r) for r in chunk]
                
                # [ETAPA: PROCESSAMENTO / ANONIMIZAÇÃO]
                if modo == "🛡️ Anonimização Total":
                    if n_cores > 1:
                        sub_sz = max(1, len(rows) // n_cores)
                        sub_chunks = [rows[i:i + sub_sz] for i in range(0, len(rows), sub_sz)]
                        futures = [executor.submit(process_chunk_parallel, sub_chunk, modo, anon_geo, target_cols, perfis) for sub_chunk in sub_chunks]
                        rows = []
                        for original_chunk, f in zip(sub_chunks, futures):
                            rows.extend(f.result() or original_chunk)
                    else:
                        rows = process_chunk_parallel(rows, modo, anon_geo, target_cols, perfis) or rows

                # [ETAPA: ESCRITA / INSERÇÃO]
                if rows: 
                    db_utils.insert_rows(dst_engine, t, s, rows, ignore_conflicts=ignorar_duplicatas)

                inserted_count = len(rows)
                total_rows += inserted_count

                chunk_speed = inserted_count / max(time.time() - chunk_start, 0.001)
                weighted_speed_samples.append(chunk_speed)
                if len(weighted_speed_samples) > 30: weighted_speed_samples.pop(0)

                # ========================================================
                # 🚀 ATUALIZAÇÃO UI E ESTIMATIVA DE TEMPO (ETA)
                # ========================================================
                now = time.time()
                if now - last_ui_update > 1: 
                    stable_speed = sum(weighted_speed_samples) / len(weighted_speed_samples) if weighted_speed_samples else 0
                    total_progress = min(0.25 + ((total_rows / max(total_estimated, 1)) * 0.75), 1.0)
                    
                    eta_str = "Calculando..."
                    if stable_speed > 0:
                        linhas_restantes = max(0, total_estimated - total_rows)
                        segundos_restantes = int(linhas_restantes / stable_speed)
                        m, s_time = divmod(segundos_restantes, 60)
                        h, m = divmod(m, 60)
                        if h > 0: eta_str = f"{h}h {m}m {s_time}s"
                        else: eta_str = f"{m}m {s_time}s"

                    msg_metrica = (
                        f"### 📊 Progresso\n"
                        f"- 📂 Tabela: **{processed_tables}/{total_tables}**\n"
                        f"- 🧮 Registros: **{total_rows:,} / {total_estimated:,}**\n"
                        f"- ⚡ Velocidade: **{stable_speed:,.0f} linhas/s**\n"
                        f"- ⏳ Tempo Restante: **{eta_str}**"
                    )
                    
                    st.session_state.ultima_metrica = msg_metrica
                    st.session_state.ultimo_progresso = total_progress
                    metric_placeholder.markdown(msg_metrica)
                    progress_bar.progress(total_progress)
                    last_ui_update = now

    db_utils.set_replication_mode(dst_engine, "origin")
    
    st.session_state.ultimo_progresso = 1.0
    progress_bar.progress(1.0)
    
    msg_sucesso = f"✅ Pipeline concluído • {total_rows:,} linhas transferidas e mascaradas em {time.time() - t0_global:.2f}s"
    st.session_state.ultimo_status_msg = msg_sucesso
    st.session_state.ultimo_status_tipo = "success"
    status.success(msg_sucesso)

# -----------------
# GATILHO DE EXECUÇÃO SEGURO
# -----------------
if iniciar_pipeline:
    if src_cfg["db"] == src_cfg["user"] or dst_cfg["db"] == dst_cfg["user"]:
        st.warning("⚠️ Atenção: O nome do banco de dados está idêntico ao usuário. (Verifique o preenchimento automático do navegador).")
    elif not src_cfg["db"] or not dst_cfg["db"]:
        st.warning("⚠️ Atenção: O campo 'Banco' não pode ficar vazio.")
    else:
        st.session_state.is_running = True # Trava a UI
        try:
            run_pipeline()
            st.balloons()
        except Exception as e:
            error_str = str(e).lower()
            if "pg_hba.conf rejects" in error_str:
                debug_box.error("🚨 Acesso Negado pelo Banco de Dados! Usuário sem permissão.")
            elif "database" in error_str and "does not exist" in error_str:
                debug_box.error("🚨 Banco de Dados não encontrado! Verifique as conexões.")
            else:
                debug_box.error(f"🚨 **FALHA NO PIPELINE** 🚨\n\n{e}\n\n```python\n{traceback.format_exc()}\n```")
        finally:
            st.session_state.is_running = False # Destrava a UI
            st.rerun() # Atualiza a tela para voltar ao estado clicável