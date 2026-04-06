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
    create_database_if_not_exists,
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

from anonymizer import (
    is_sensitive_column,
    is_text_column_type,
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


# =========================================
# UI
# =========================================
def print_banner():
    print("\n" + "=" * 60)
    print("   Anonimizador de Banco de Dados PostgreSQL")
    print("=" * 60 + "\n")


# =========================================
# ANONIMIZAÇÃO DE LINHA
# =========================================
def anonymize_row(
    row: dict,
    sensitive_columns: list[str],
    text_scan_columns: list[str],
) -> dict:
    result = dict(row)

    # 🔹 Campos sensíveis diretos
    for col in sensitive_columns:
        if col in result and result[col] is not None:
            result[col] = anonymize_value(col, result[col])

    # 🔹 Scan de texto (nomes dentro de strings)
    for col in text_scan_columns:
        if col in result and result[col]:
            result[col] = anonymize_text_value(result[col])

    return result


# =========================================
# PROCESSAMENTO DE TABELA
# =========================================
def process_table(
    source_engine,
    dest_engine,
    table_name: str,
    sensitive_columns: list[str],
    text_scan_columns: list[str],
) -> int:
    total = get_row_count(source_engine, table_name)

    if total == 0:
        logger.info(f"  Tabela '{table_name}': vazia, pulando.")
        return 0

    processed = 0

    with tqdm(
        total=total,
        desc=f"  {table_name}",
        unit="reg",
        ncols=70,
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}]",
    ) as pbar:

        for chunk in fetch_rows_chunked(source_engine, table_name, CHUNK_SIZE):

            anon_rows = []
            for row in chunk:
                try:
                    anon = anonymize_row(
                        dict(row),
                        sensitive_columns,
                        text_scan_columns,
                    )
                    anon_rows.append(anon)
                except Exception as e:
                    logger.warning(f"Erro ao anonimizar linha: {e}")

            insert_rows(dest_engine, table_name, anon_rows)

            processed += len(anon_rows)
            pbar.update(len(anon_rows))

    return processed


# =========================================
# CLASSIFICAÇÃO DE COLUNAS
# =========================================
def classify_columns(info: dict) -> tuple[list[str], list[str]]:
    pk_cols = set(info["primary_keys"])

    fk_cols = set()
    for fk in info["foreign_keys"]:
        fk_cols.update(fk.get("constrained_columns", []))

    excluded = pk_cols | fk_cols

    sensitive = []
    text_scan = []

    for col in info["columns"]:
        col_name = col["name"]
        col_type = str(col.get("type", "")).lower()

        if col_name in excluded:
            continue

        if is_sensitive_column(col_name):
            sensitive.append(col_name)

        elif is_text_column_type(col_type):
            text_scan.append(col_name)

    return sensitive, text_scan


# =========================================
# MAIN
# =========================================
def main():
    print_banner()

    # 🔹 URLs
    try:
        source_url = get_source_url()
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    dest_url = get_dest_url(source_url)
    server_url = get_server_url(source_url)
    dest_db_name = get_dest_db_name(source_url)
    source_db_name = get_source_db_name(source_url)

    logger.info(f"Banco origem  : {source_db_name}")
    logger.info(f"Banco destino : {dest_db_name}\n")

    # 🔹 Conexão origem
    try:
        logger.info("Conectando ao banco de origem...")
        source_engine = connect(source_url)
        logger.info("OK\n")
    except Exception as e:
        logger.error(f"Erro na conexão origem: {e}")
        sys.exit(1)

    # 🔹 Criar banco destino
    try:
        logger.info(f"Criando banco destino '{dest_db_name}' se necessário...")
        create_database_if_not_exists(server_url, dest_db_name)
        logger.info("OK\n")
    except Exception as e:
        logger.error(f"Erro ao criar banco destino: {e}")
        sys.exit(1)

    # 🔹 Conexão destino
    try:
        logger.info("Conectando ao banco destino...")
        dest_engine = connect(dest_url)
        logger.info("OK\n")
    except Exception as e:
        logger.error(f"Erro na conexão destino: {e}")
        sys.exit(1)

    # 🔹 Copiar schema
    try:
        logger.info("Copiando estrutura do banco...")
        copy_schema(source_engine, dest_engine)
        logger.info("Estrutura copiada\n")
    except Exception as e:
        logger.error(f"Erro ao copiar schema: {e}")
        sys.exit(1)

    # 🔹 Ordenação
    tables = get_tables(source_engine)
    ordered_tables = build_dependency_graph(source_engine, tables)

    logger.info(f"Tabelas encontradas: {len(ordered_tables)}")
    logger.info(f"Ordem: {', '.join(ordered_tables)}\n")

    # 🔹 Classificação
    sensitive_map = {}
    text_scan_map = {}

    for table in ordered_tables:
        info = get_table_info(source_engine, table)

        sensitive, text_scan = classify_columns(info)

        sensitive_map[table] = sensitive
        text_scan_map[table] = text_scan

        logger.info(
            f"{table} -> sensiveis={len(sensitive)} | texto={len(text_scan)}"
        )

    print()

    # 🔹 Execução
    logger.info("Iniciando anonimização...\n")

    disable_fk_constraints(dest_engine)

    total_records = 0
    failed_tables = []

    for table in ordered_tables:
        try:
            count = process_table(
                source_engine,
                dest_engine,
                table,
                sensitive_map.get(table, []),
                text_scan_map.get(table, []),
            )
            total_records += count

        except Exception as e:
            logger.error(f"Erro na tabela '{table}': {e}")
            failed_tables.append(table)

    enable_fk_constraints(dest_engine)

    # 🔹 Final
    print("\n" + "=" * 60)
    logger.info("FINALIZADO")
    logger.info(f"Registros processados: {total_records}")
    logger.info(f"Banco gerado: {dest_db_name}")

    if failed_tables:
        logger.warning(f"Tabelas com erro: {failed_tables}")
    else:
        logger.info("Tudo processado com sucesso")

    print("=" * 60 + "\n")

    source_engine.dispose()
    dest_engine.dispose()
    


if __name__ == "__main__":
    main()