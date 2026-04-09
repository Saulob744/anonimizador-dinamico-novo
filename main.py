import logging
import sys
from tqdm import tqdm

from config import (
    get_source_url,
    get_dest_url,
    get_server_url,
    get_dest_db_name,
    get_source_db_name,
)

from db_utils import (
    connect,
    recreate_database_if_not_exists,  # Nome padronizado conforme db_utils atualizado
    get_user_schemas,
    get_tables,
    get_table_info,
    build_dependency_graph,
    copy_schema,
    get_row_count,
    fetch_rows_chunked,
    insert_rows,
    disable_fk_constraints,
    enable_fk_constraints,
)

# Importação conforme a nova estrutura do anonymizer.py
from anonymizer import (
    anonymize_value,
    anonymize_text_value,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)

CHUNK_SIZE = 500

def print_banner():
    print("\n" + "=" * 60)
    print("   Anonimizador Inteligente PostgreSQL (IA + PoS Tagging)")
    print("=" * 60 + "\n")

# =========================================
# ANONIMIZAÇÃO DE LINHA
# =========================================
def anonymize_row(row: dict, column_treatments: dict) -> dict:
    result = dict(row)

    for col, treatment in column_treatments.items():
        if result[col] is None or treatment == "SKIP":
            continue
            
        # O tratamento agora é decidido pela inteligência do anonymizer
        if treatment == "TEXT_SCAN":
            # Para campos de texto livre (IA e Gramática)
            result[col] = anonymize_text_value(result[col])
        elif treatment == "ANONYMIZE":
            # Para campos diretos (Nome, CPF isolado)
            result[col] = anonymize_value(col, result[col])

    return result

# =========================================
# PROCESSAMENTO DE TABELA
# =========================================
def process_table(
    source_engine,
    dest_engine,
    schema: str,
    table_name: str,
    column_treatments: dict,
) -> int:
    total = get_row_count(source_engine, table_name, schema)
    if total == 0:
        return 0

    processed = 0
    with tqdm(
        total=total,
        desc=f"   {schema}.{table_name}",
        unit="reg",
        ncols=80,
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}]",
    ) as pbar:

        for chunk in fetch_rows_chunked(source_engine, table_name, schema, CHUNK_SIZE):
            anon_rows = []
            for row in chunk:
                try:
                    # O tratamento é baseado no mapeamento prévio das colunas
                    anon = anonymize_row(dict(row), column_treatments)
                    anon_rows.append(anon)
                except Exception as e:
                    logger.warning(f"Erro na linha de {table_name}: {e}")

            if anon_rows:
                insert_rows(dest_engine, table_name, schema, anon_rows)
            
            processed += len(anon_rows)
            pbar.update(len(anon_rows))

    return processed

# =========================================
# CLASSIFICAÇÃO DE COLUNAS
# =========================================
def classify_columns(info: dict) -> dict:
    """
    Decide o tratamento de cada coluna.
    """
    pk_cols = set(info["primary_keys"])
    fk_cols = set()
    for fk in info["foreign_keys"]:
        fk_cols.update(fk.get("constrained_columns", []))

    excluded = pk_cols | fk_cols
    treatments = {}

    for col in info["columns"]:
        col_name = col["name"]
        col_type = str(col.get("type", "")).lower()

        if col_name in excluded:
            treatments[col_name] = "SKIP"
            continue

        # Se for texto (TEXT, VARCHAR, etc), usamos o SCAN inteligente
        if any(t in col_type for t in ["text", "char", "varying"]):
            treatments[col_name] = "TEXT_SCAN"
        # Para outros tipos, tentamos a anonimização direta
        else:
            treatments[col_name] = "ANONYMIZE"

    return treatments

# =========================================
# MAIN
# =========================================
def main():
    print_banner()

    source_url = get_source_url()
    dest_url = get_dest_url(source_url)
    server_url = get_server_url(source_url)
    dest_db_name = get_dest_db_name(source_url)
    source_db_name = get_source_db_name(source_url)

    logger.info(f"Banco origem  : {source_db_name}")
    logger.info(f"Banco destino : {dest_db_name}\n")

    source_engine = connect(source_url)

    # RECRIA O BANCO (Função agora encerra conexões e limpa o destino)
    try:
        recreate_database_if_not_exists(server_url, dest_db_name)
    except Exception as e:
        logger.error(f"Erro ao recriar banco: {e}")
        sys.exit(1)
    
    dest_engine = connect(dest_url)
    disable_fk_constraints(dest_engine)

    schemas = get_user_schemas(source_engine)
    
    total_records = 0
    failed_tables = []

    for current_schema in schemas:
        logger.info(f">>> PROCESSANDO SCHEMA: '{current_schema}'")
        copy_schema(source_engine, dest_engine, current_schema)

        tables = get_tables(source_engine, current_schema)
        ordered_tables = build_dependency_graph(source_engine, tables, current_schema)

        for table in ordered_tables:
            info = get_table_info(source_engine, table, current_schema)
            column_treatments = classify_columns(info)

            try:
                count = process_table(
                    source_engine,
                    dest_engine,
                    current_schema,
                    table,
                    column_treatments,
                )
                total_records += count
            except Exception as e:
                logger.error(f"Falha técnica em {current_schema}.{table}: {e}")
                failed_tables.append(f"{current_schema}.{table}")

    enable_fk_constraints(dest_engine)
    logger.info(f"FINALIZADO. Total de registros: {total_records}")

    if failed_tables:
        logger.warning(f"Tabelas com erro: {failed_tables}")

    source_engine.dispose()
    dest_engine.dispose()

if __name__ == "__main__":
    main()