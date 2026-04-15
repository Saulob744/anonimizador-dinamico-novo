import streamlit as st
import sqlalchemy as sa
import db_utils
import anonymizer
import importlib
from datetime import datetime
import urllib.parse

# ==================================================
# FORÇAR ATUALIZAÇÃO DOS MÓDULOS
# ==================================================
importlib.reload(db_utils)
importlib.reload(anonymizer)

# Configuração visual da página
st.set_page_config(page_title="🛡️ Aegis Anonymizer Pro", page_icon="🛡️", layout="wide")

st.markdown("""
    <style>
    .stApp { background-color: #0f172a; color: #ffffff; }
    [data-testid="stMetric"] { background-color: #1e293b; border-radius: 10px; padding: 10px; border: 1px solid #3b82f6; }
    </style>
    """, unsafe_allow_html=True)

# ==================================================
# FUNÇÕES DE APOIO
# ==================================================
def build_url(user, password, host, port, db):
    safe_user = urllib.parse.quote_plus(user)
    safe_password = urllib.parse.quote_plus(password)
    return f"postgresql://{safe_user}:{safe_password}@{host}:{port}/{db}"

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

# INICIALIZAÇÃO SEGURA DAS ESTATÍSTICAS (Incluindo 'TEXT')
if 'stats' not in st.session_state:
    st.session_state.stats = {
        "PER": 0, 
        "DOCS": 0, 
        "CONTACTS": 0, 
        "TEXT": 0,      # Adicionado para evitar o erro de KeyError
        "total_rows": 0
    }

# ==================================================
# SIDEBAR
# ==================================================
with st.sidebar:
    st.title("🛡️ Aegis Control")
    st.subheader("🔴 ORIGEM")
    src_host = st.text_input("IP do Banco Origem", "localhost")
    src_user = st.text_input("Usuário", "postgres")
    src_pass = st.text_input("Senha", type="password")
    src_db = st.text_input("Nome do Banco")

    st.divider()
    st.subheader("🟢 DESTINO")
    dst_db = st.text_input("Nome do Novo Banco (Destino)")
    
    st.divider()
    modo = st.selectbox("Modo de Operação", ["🛡️ Anonimização Total", "⚡ Cópia Direta"])
    chunk_size = st.number_input("Tamanho do Lote", value=1000, step=500)
    btn_iniciar = st.button("🚀 INICIAR PIPELINE", use_container_width=True)

# ==================================================
# PAINEL PRINCIPAL
# ==================================================
st.title("🛡️ Aegis Anonymizer Pipeline")
# Criamos 5 colunas para incluir a nova métrica de texto
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("📊 Total Processado", f"{st.session_state.stats['total_rows']:,}")
m2.metric("👤 Pessoas", st.session_state.stats['PER'])
m3.metric("🆔 Documentos", st.session_state.stats['DOCS'])
m4.metric("📞 Contatos", st.session_state.stats['CONTACTS'])
m5.metric("📝 Relatos/Textos", st.session_state.stats['TEXT'])

status_area = st.empty()
progress_bar = st.progress(0)

# ==================================================
# LÓGICA DE EXECUÇÃO
# ==================================================
if btn_iniciar:
    # --- TRAVA DE SEGURANÇA CRÍTICA ---
    if src_db.strip() == dst_db.strip():
        st.error("❌ SEGURANÇA: O banco de destino não pode ser igual ao de origem! Operação cancelada para evitar perda de dados.")
        st.stop()

    try:
        src_url = build_url(src_user, src_pass, src_host, "5432", src_db)
        dst_url = build_url(src_user, src_pass, src_host, "5432", dst_db)
        admin_url = build_url(src_user, src_pass, src_host, "5432", "postgres")

        status_area.info("⏳ Conectando e preparando banco de destino...")
        src_engine = db_utils.connect(src_url)
        db_utils.recreate_database_if_not_exists(admin_url, dst_db)
        dst_engine = db_utils.connect(dst_url)

        db_utils.set_replication_mode(dst_engine, 'replica')

        schemas = db_utils.get_user_schemas(src_engine)
        for schema in schemas:
            status_area.warning(f"📂 Processando Schema: {schema}")
            db_utils.copy_schema(src_engine, dst_engine, schema)
            
            raw_tables = db_utils.get_tables(src_engine, schema)
            tables = db_utils.build_dependency_graph(src_engine, raw_tables, schema)

            for table in tables:
                status_area.write(f"⚙️ Tabela: `{schema}.{table}`")
                info = db_utils.get_table_info(src_engine, table, schema)
                treatments = classify_columns(info)
                
                db_utils.truncate_table(dst_engine, table, schema)
                
                for chunk in db_utils.fetch_rows_streaming(src_engine, table, schema, chunk_size):
                    rows = [dict(r) for r in chunk]
                    
                    if "Anonimização" in modo:
                        for r in rows:
                            for col, treat in treatments.items():
                                if r[col] is None or treat == "SKIP": continue
                                
                                val_orig = r[col]
                                res_val, cat = anonymizer.anonymize_value(col, val_orig, is_numeric=(treat=="NUMERIC"))
                                
                                r[col] = res_val
                                if res_val != val_orig:
                                    st.session_state.stats['total_rows'] += 1
                                    # TRATAMENTO SEGURO DE CATEGORIAS
                                    if cat and cat in st.session_state.stats:
                                        st.session_state.stats[cat] += 1
                    
                    db_utils.insert_rows(dst_engine, table, schema, rows)
                st.toast(f"Tabela {table} finalizada!", icon="✅")

        db_utils.set_replication_mode(dst_engine, 'origin')
        status_area.success("✅ Processo concluído com sucesso!")
        st.balloons()

    except Exception as e:
        st.error(f"❌ Erro Crítico: {e}")
        try: db_utils.set_replication_mode(dst_engine, 'origin')
        except: pass