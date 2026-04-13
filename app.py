import streamlit as st
import sqlalchemy as sa
import pandas as pd
import time
import db_utils
import anonymizer
from datetime import datetime

# --- CONFIGURAÇÃO DA PÁGINA ---
st.set_page_config(page_title="🛡️ Aegis Anonymizer Pro", page_icon="🛡️", layout="wide", initial_sidebar_state="expanded")

# --- CSS DE ALTA VISIBILIDADE ---
st.markdown("""
    <style>
    .stApp { background-color: #0f172a; color: #ffffff; }
    
    /* Sidebar com borda de destaque */
    section[data-testid="stSidebar"] {
        background-color: #1e293b !important;
        border-right: 2px solid #3b82f6;
    }

    /* Cores dos Menus */
    .menu-origem { border-left: 5px solid #ef4444; padding: 10px 15px; background: #1a202c; border-radius: 5px; margin-bottom: 20px; }
    .menu-destino { border-left: 5px solid #10b981; padding: 10px 15px; background: #1a202c; border-radius: 5px; margin-bottom: 20px; }
    .menu-config { border-left: 5px solid #f59e0b; padding: 10px 15px; background: #1a202c; border-radius: 5px; margin-bottom: 20px; }

    /* Inputs: Contraste Máximo */
    .stTextInput input {
        background-color: #0f172a !important;
        color: #00ff00 !important; 
        border: 1px solid #3b82f6 !important;
        font-family: 'Courier New', monospace;
    }
    label { color: #3b82f6 !important; font-weight: bold !important; text-transform: uppercase; font-size: 0.8rem; }

    /* Cards de Métricas */
    [data-testid="stMetric"] { background-color: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 15px; }
    </style>
    """, unsafe_allow_html=True)

# --- AUXILIARES ---
def build_url(user, password, host, port, db):
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"

def classify_columns(info: dict) -> dict:
    treatments = {}
    pks = info.get("primary_keys", [])
    for col in info["columns"]:
        name = col["name"]
        ctype = str(col["type"]).lower()
        if name in pks:
            treatments[name] = "SKIP"
        elif any(t in ctype for t in ["int", "bigint", "numeric", "double", "real"]):
            treatments[name] = "NUMERIC"
        elif any(t in ctype for t in ["date", "time", "timestamp", "bool"]):
            treatments[name] = "SKIP"
        else:
            treatments[name] = "TEXT"
    return treatments

if 'stats' not in st.session_state:
    st.session_state.stats = {"PER": 0, "DOCS": 0, "CONTACTS": 0, "total_rows": 0}
if 'logs' not in st.session_state:
    st.session_state.logs = []

def add_log(msg):
    st.session_state.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# --- SIDEBAR ---
with st.sidebar:
    st.title("🛡️ Aegis Control")
    st.markdown('<div class="menu-origem">', unsafe_allow_html=True)
    st.subheader("🔴 ORIGEM")
    src_host = st.text_input("Host IP", value="host.docker.internal")
    c1, c2 = st.columns([0.6, 0.4])
    src_user = c1.text_input("Usuário", value="postgres", key="u1")
    src_port = c2.text_input("Porta", value="5432", key="p1")
    src_pass = st.text_input("Senha", type="password", key="s1")
    src_db = st.text_input("Banco de Dados", key="d1")
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="menu-destino">', unsafe_allow_html=True)
    st.subheader("🟢 DESTINO")
    dst_host = st.text_input("Host IP", value="host.docker.internal", key="h2")
    c3, c4 = st.columns([0.6, 0.4])
    dst_user = c3.text_input("Usuário", value="postgres", key="u2")
    dst_port = c4.text_input("Porta", value="5432", key="p2")
    dst_pass = st.text_input("Senha", type="password", key="s2")
    dst_db = st.text_input("Novo Banco", key="d2")
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="menu-config">', unsafe_allow_html=True)
    st.subheader("⚙️ AJUSTES")
    modo = st.selectbox("Operação", ["🛡️ Anonimização Total", "⚡ Cópia Direta"])
    chunk_size = st.number_input("Lote (Rows)", value=1000, step=500)
    st.markdown('</div>', unsafe_allow_html=True)

    btn_iniciar = st.button("🚀 INICIAR PIPELINE", use_container_width=True)

# --- MÉTRICAS ---
m1, m2, m3, m4 = st.columns(4)
m1.metric("📊 Processados", f"{st.session_state.stats['total_rows']:,}")
m2.metric("👤 Nomes", st.session_state.stats['PER'])
m3.metric("🆔 Documentos", st.session_state.stats['DOCS'])
m4.metric("📞 Contatos", st.session_state.stats['CONTACTS'])

tab_exec, tab_log = st.tabs(["🚀 EXECUÇÃO", "📋 LOGS"])

with tab_exec:
    status_msg = st.empty()
    bar = st.progress(0)
    table_info = st.empty()

with tab_log:
    log_area = st.empty()

# --- LÓGICA DE EXECUÇÃO ---
if btn_iniciar:
    try:
        src_url = build_url(src_user, src_pass, src_host, src_port, src_db)
        dst_url = build_url(dst_user, dst_pass, dst_host, dst_port, dst_db)
        add_log("Conectando aos servidores...")
        src_engine = db_utils.connect(src_url)
        
        # Cria banco destino
        admin_url = f"postgresql://{dst_user}:{dst_pass}@{dst_host}:{dst_port}/postgres"
        db_utils.recreate_database_if_not_exists(admin_url, dst_db)
        dst_engine = db_utils.connect(dst_url)
        
        # Desativa chaves estrangeiras temporariamente para performance e ordem
        db_utils.set_replication_mode(dst_engine, 'replica')
        
        schemas = db_utils.get_user_schemas(src_engine)
        for schema in schemas:
            add_log(f"Iniciando processamento do schema: {schema}")
            
            # Ordenação por dependência (Pai antes de Filho)
            raw_tables = db_utils.get_tables(src_engine, schema)
            tables = db_utils.build_dependency_graph(src_engine, raw_tables, schema)
            
            # Cria a estrutura (DDL)
            db_utils.copy_schema(src_engine, dst_engine, schema)
            
            for table in tables:
                table_info.markdown(f"🚩 **Processando:** `{schema}.{table}`")
                info = db_utils.get_table_info(src_engine, table, schema)
                treatments = classify_columns(info)
                
                # Limpa a tabela para evitar UniqueViolation
                db_utils.truncate_table(dst_engine, table, schema)
                
                with src_engine.connect() as conn:
                    total = conn.execute(sa.text(f'SELECT COUNT(*) FROM "{schema}"."{table}"')).scalar()
                
                if total == 0: continue
                
                processed = 0
                for chunk in db_utils.fetch_rows_streaming(src_engine, table, schema, chunk_size):
                    rows = [dict(r) for r in chunk]
                    
                    if "Anonimização" in modo:
                        for r in rows:
                            for col, treat in treatments.items():
                                if r[col] is None or treat == "SKIP": continue
                                
                                val_orig = r[col]
                                res_val, cat = anonymizer.anonymize_value(col, r[col], is_numeric=(treat=="NUMERIC"))
                                
                                r[col] = res_val
                                if res_val != val_orig:
                                    st.session_state.stats['total_rows'] += 1
                                    if cat: st.session_state.stats[cat] += 1
                    
                    try:
                        db_utils.insert_rows(dst_engine, table, schema, rows)
                    except Exception as e:
                        add_log(f"⚠️ Erro no lote da tabela {table}: {str(e)}")

                    processed += len(rows)
                    bar.progress(processed/total, text=f"{int((processed/total)*100)}%")
                    log_area.code("\n".join(st.session_state.logs[-20:]))

        # --- LIMPEZA FINAL DO SCHEMA PUBLIC ---
        add_log("Finalizando pipeline. Removendo schemas padrão vazios...")
        db_utils.cleanup_empty_public_schema(dst_engine)

        # Reativa chaves estrangeiras
        db_utils.set_replication_mode(dst_engine, 'origin')
        st.success("✅ Processo concluído com sucesso!")
        st.balloons()

    except Exception as e:
        st.error(f"Erro Crítico: {e}")
        add_log(f"ERRO: {e}")