from sources import (
    load_source,
    write_csv_streaming
)

from app import process_chunk_parallel

source = load_source(
    "teste.csv",
    chunk_size=1
)

first_chunk = True

for chunk in source:

    processed = process_chunk_parallel(
        chunk,
        "🛡️ Anonimização Total",
        True
    )

    write_csv_streaming(
        processed,
        "teste_anonimizado.csv",
        first_chunk
    )

    first_chunk = False

    print(processed)

print("FINALIZADO")