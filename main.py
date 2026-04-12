import logging
import sys
import sqlalchemy as sa
from tqdm import tqdm
from config import get_source_url, get_dest_url, get_server_url, get_dest_db_name, get_source_db_name
import db_utils
import anonymizer

<<<<<<< HEAD
# Configuração de Log
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CHUNK_SIZE = 1500  # Tamanho do lote para processamento

def classify_columns(info: dict) -> dict:
    """Mapeia o tratamento de cada coluna baseado no tipo de dado."""
=======
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CHUNK_SIZE = 1000

def classify_columns(info: dict) -> dict:
    """Mapeia o que deve ser feito com cada coluna da tabela."""
    pk_cols = set(info["primary_keys"])
    fk_cols = set()
    for fk in info["foreign_keys"]:
        fk_cols.update(fk.get("constrained_columns", []))

>>>>>>> c310b3398f9ed58198c2d70376e9bf875425f26a
    treatments = {}
    for col in info["columns"]:
        name = col["name"]
        ctype = str(col["type"]).lower()

<<<<<<< HEAD
        # SKIP: Ignora datas, horas e booleanos para evitar erros de conversão
        if any(t in ctype for t in ["date", "time", "timestamp", "bool"]):
            treatments[name] = "SKIP"
        else:
            # Tudo o que for texto ou código potencial entra para análise
=======
        # 1. Ignorar campos de Data, Hora e Booleanos (Evita o DatetimeFieldOverflow)
        if any(t in ctype for t in ["date", "time", "timestamp", "bool"]):
            treatments[name] = "SKIP"
            
        # 2. Textos passam pela Inteligência Artificial
        elif any(t in ctype for t in ["char", "text", "varying"]):
            treatments[name] = "TEXT_SCAN"
            
        # 3. Outros campos (Inteiros, IDs, etc) passam pela anonimização direta
        else:
>>>>>>> c310b3398f9ed58198c2d70376e9bf875425f26a
            treatments[name] = "ANONYMIZE"
            
    return treatments

<<<<<<< HEAD
def run_pipeline(mode: str):
    """Pipeline principal de Migração e Anonimização."""
    source_url = get_source_url()
    dest_url = get_dest_url(source_url)
    
    logger.info("Conectando aos bancos de dados...")
=======
def process_row_anon(row: dict, treatments: dict) -> dict:
    """Aplica as regras de anonimização em uma única linha."""
    new_row = dict(row)
    for col, method in treatments.items():
        if new_row[col] is None or method == "SKIP":
            continue
        
        if method == "TEXT_SCAN":
            new_row[col] = anonymizer.anonymize_text_value(str(new_row[col]))
        elif method == "ANONYMIZE":
            new_row[col] = anonymizer.anonymize_value(col, new_row[col])
            
    return new_row

def run_pipeline(mode: str):
    """Executa o pipeline principal com base na escolha do usuário."""
    source_url = get_source_url()
    dest_url = get_dest_url(source_url)
    
    logger.info("Iniciando conexão com os bancos...")
>>>>>>> c310b3398f9ed58198c2d70376e9bf875425f26a
    src_engine = db_utils.connect(source_url)
    
    try:
        db_utils.recreate_database_if_not_exists(get_server_url(source_url), get_dest_db_name(source_url))
    except Exception as e:
<<<<<<< HEAD
        logger.error(f"Erro ao preparar banco de destino: {e}")
=======
        logger.error(f"Erro ao recriar banco de destino: {e}")
>>>>>>> c310b3398f9ed58198c2d70376e9bf875425f26a
        sys.exit(1)
        
    dest_engine = db_utils.connect(dest_url)

<<<<<<< HEAD
    # Modo réplica para acelerar inserção (Desativa Triggers e FKs temporariamente)
=======
    # 1. Desliga FKs para inserção ultra-rápida (PostgreSQL)
>>>>>>> c310b3398f9ed58198c2d70376e9bf875425f26a
    db_utils.set_replication_mode(dest_engine, 'replica')

    schemas = db_utils.get_user_schemas(src_engine)
    
    for schema in schemas:
<<<<<<< HEAD
        tables = db_utils.get_tables(src_engine, schema)
        if not tables:
            continue
            
        logger.info(f"Processando Schema: {schema}")
        db_utils.copy_schema(src_engine, dest_engine, schema)
        ordered_tables = db_utils.build_dependency_graph(src_engine, tables, schema)

        for table in ordered_tables:
            info = db_utils.get_table_info(src_engine, table, schema)
            treatments = classify_columns(info)

            with src_engine.connect() as conn:
                total_rows = conn.execute(sa.text(f'SELECT COUNT(*) FROM "{schema}"."{table}"')).scalar()
                
            if total_rows == 0: continue
                
            with tqdm(total=total_rows, desc=f"Tabela: {table}", unit="reg") as pbar:
                for chunk in db_utils.fetch_rows_streaming(src_engine, table, schema, CHUNK_SIZE):
                    
                    rows = [dict(row) for row in chunk]
                    
                    if mode == "FULL_ANON":
                        # Aplica a anonimização inteligente em cada linha
                        for row in rows:
                            for col, method in treatments.items():
                                if method == "ANONYMIZE" and row[col] is not None:
                                    row[col] = anonymizer.anonymize_value(col, row[col])
                                    
                    db_utils.insert_rows(dest_engine, table, schema, rows)
                    pbar.update(len(rows))

    db_utils.set_replication_mode(dest_engine, 'origin')
    logger.info("🎉 Processo de anonimização e migração concluído!")
=======
        # 1. Pega as tabelas PRIMEIRO
        tables = db_utils.get_tables(src_engine, schema)
        
        # 2. O PULO DO GATO: Se o schema estiver vazio, ignora e vai para o próximo
        if not tables:
            logger.info(f"Ignorando Schema '{schema}' (Vazio na origem).")
            continue
            
        # 3. Se tem tabelas, aí sim a gente cria a estrutura no destino
        logger.info(f"Copiando estrutura do Schema '{schema}'...")
        db_utils.copy_schema(src_engine, dest_engine, schema)
        
        ordered_tables = db_utils.build_dependency_graph(src_engine, tables, schema)

        for table in ordered_tables:
            logger.info(f"Processando Tabela: {schema}.{table} | Modo: {mode}")
            
            with src_engine.connect() as conn:
                total_rows = conn.execute(sa.text(f'SELECT COUNT(*) FROM "{schema}"."{table}"')).scalar()
                
            if total_rows == 0:
                continue
                
            info = db_utils.get_table_info(src_engine, table, schema)
            treatments = classify_columns(info)

            with tqdm(total=total_rows, desc=table, unit="reg", ncols=80) as pbar:
                # Streaming em lotes (Chunks)
                for chunk in db_utils.fetch_rows_streaming(src_engine, table, schema, CHUNK_SIZE):
                    
                    if mode == "MIGRATE_ONLY":
                        # Cópia Fiel: Apenas converte para dicionário e envia
                        final_rows = [dict(row) for row in chunk]
                    else:
                        # Full Pipeline: Anonimiza linha por linha
                        final_rows = [process_row_anon(dict(row), treatments) for row in chunk]
                        
                    db_utils.insert_rows(dest_engine, table, schema, final_rows)
                    pbar.update(len(final_rows))

    # 2. Religa restrições de FKs no final
    db_utils.set_replication_mode(dest_engine, 'origin')
    
    # 3. FAXINA FINAL: Remove o schema 'public' gerado por padrão pelo PostgreSQL se ele estiver vazio
    try:
        insp_dest = sa.inspect(dest_engine)
        if 'public' in insp_dest.get_schema_names():
            tables_in_public = insp_dest.get_table_names(schema='public')
            if not tables_in_public:
                with dest_engine.begin() as conn:
                    conn.execute(sa.text("DROP SCHEMA public;"))
                logger.info("🗑️ Schema 'public' nativo (vazio) foi removido do destino.")
    except Exception as e:
        logger.warning(f"Não foi possível remover o schema public vazio: {e}")

    logger.info("🎉 Operação finalizada com sucesso!")
>>>>>>> c310b3398f9ed58198c2d70376e9bf875425f26a
    
    src_engine.dispose()
    dest_engine.dispose()

if __name__ == "__main__":
    print("\n" + "=" * 50)
<<<<<<< HEAD
    print(" 🛡️ AEGIS TOOLKIT - SISTEMA DE ANONIMIZAÇÃO IA")
    print("=" * 50)
    print("1. Migração Pura (Apenas cópia)")
    print("2. Pipeline Completo (IA + Consistência de Dados)")
    print("=" * 50)
    
    escolha = input("Selecione a opção: ").strip()
    
    if escolha == '1':
        run_pipeline(mode="MIGRATE_ONLY")
    elif escolha == '2':
        run_pipeline(mode="FULL_ANON")
    else:
        print("Opção inválida.")
=======
    print(" 🛡️ AEGIS TOOLKIT - Dados Seguros & Migração")
    print("=" * 50)
    print("1. Migração Pura (Cópia fiel de um banco para outro)")
    print("2. Pipeline Completo (Copiar + Aplicar Anonimização IA)")
    print("=" * 50)
    
    escolha = input("Selecione uma opção (1 ou 2): ").strip()
    
    if escolha == '1':
        print("\n🚀 Iniciando MODO CÓPIA RÁPIDA...\n")
        run_pipeline(mode="MIGRATE_ONLY")
    elif escolha == '2':
        print("\n🎭 Iniciando MODO ANONIMIZAÇÃO COMPLETA...\n")
        run_pipeline(mode="FULL_ANON")
    else:
        print("\n❌ Opção inválida. Encerrando.")
>>>>>>> c310b3398f9ed58198c2d70376e9bf875425f26a
