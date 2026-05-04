import pandas as pd
import json
import os

# =========================================================
# CSV STREAMING
# =========================================================

def fetch_csv_streaming(path, chunk_size=10000):
    """
    Lê CSVs gigantes sem explodir RAM.
    Retorna chunks no mesmo formato usado pelo banco:
    List[Dict]
    """

    for chunk in pd.read_csv(
        path,
        chunksize=chunk_size,
        dtype=str,
        keep_default_na=False,
        low_memory=True,
        encoding="utf-8"
    ):

        yield chunk.to_dict(orient="records")


# =========================================================
# DETECÇÃO DE TIPO
# =========================================================

def detect_source_type(path):

    ext = os.path.splitext(path)[1].lower()

    if ext == ".csv":
        return "csv"

    elif ext in [".xlsx", ".xls"]:
        return "excel"

    elif ext == ".json":
        return "json"

    elif ext == ".txt":
        return "text"

    elif ext == ".pdf":
        return "pdf"

    return None


# =========================================================
# LOADER UNIVERSAL
# =========================================================

def load_source(path, chunk_size=10000):

    source_type = detect_source_type(path)

    if source_type == "csv":
        return fetch_csv_streaming(path, chunk_size)

    raise Exception(f"Formato não suportado: {source_type}")

# =========================================================
# CSV WRITER
# =========================================================

def write_csv_streaming(rows, output_path, first_chunk=False):
    """
    Salva chunks processados sem carregar tudo em memória.
    """

    import pandas as pd

    df = pd.DataFrame(rows)

    df.to_csv(
        output_path,
        mode="w" if first_chunk else "a",
        index=False,
        header=first_chunk,
        encoding="utf-8"
    )