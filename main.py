import logging
import sys
import sqlalchemy as sa
from tqdm import tqdm
from config import get_source_url, get_dest_url, get_server_url, get_dest_db_name, get_source_db_name
import db_utils
import anonymizer

# Configuração de Log
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CHUNK_SIZE = 1500  # Tamanho do lote para processamento

def classify_columns(info: dict) -> dict:
    """Mapeia o tratamento de cada coluna baseado no tipo de dado."""
    treatments = {}
    for col in info["columns"]:
        name = col["name"]
        ctype = str(col["type"]).lower()

        # SKIP: Ignora datas, horas e booleanos para evitar erros de conversão
        if any(t in ctype for t in ["date", "time", "timestamp", "bool"]):
            treatments[name] = "SKIP"
        else:
            # Tudo o que for texto ou código potencial entra para análise
            treatments[name] = "ANONYMIZE"
            
    return treatments

def run_pipeline(mode: str):
    """Pipeline principal de Migração e Anonimização."""
    source_url = get_source_url()
    dest_url = get_dest_url(source_url)
    
    logger.info("Conectando aos bancos de dados...")
    src_engine = db_utils.connect(source_url)
    
    try:
        db_utils.recreate_database_if_not_exists(get_server_url(source_url), get_dest_db_name(source_url))
    except Exception as e:
        logger.error(f"Erro ao preparar banco de destino: {e}")
        sys.exit(1)
        
    dest_engine = db_utils.connect(dest_url)

    # Modo réplica para acelerar inserção (Desativa Triggers e FKs temporariamente)
    db_utils.set_replication_mode(dest_engine, 'replica')

    schemas = db_utils.get_user_schemas(src_engine)
    
    for schema in schemas:
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
    
    src_engine.dispose()
    dest_engine.dispose()

if __name__ == "__main__":
    print("\n" + "=" * 50)
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
