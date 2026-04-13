import os
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv

load_dotenv(encoding="utf-8")


# =========================
# SOURCE
# =========================
def get_source_url() -> str:
    url = os.getenv("DB_SOURCE")

    if not url:
        raise ValueError(
            "DB_SOURCE não definido no .env\n"
            "Ex: postgresql+psycopg://user:pass@host:5432/banco"
        )

    return _sanitize_db_url(url)


# =========================
# DESTINO (NOVO MODELO FIXO VIA ENV)
# =========================
def get_dest_url() -> str:
    url = os.getenv("DB_TARGET")

    if url:
        return _sanitize_db_url(url)

    # fallback automático: usa source + _anon
    source = get_source_url()
    parsed = urlparse(source)

    db_name = parsed.path.lstrip("/") or "database"
    new_db = f"/{db_name}_anon"

    return urlunparse(parsed._replace(path=new_db))


# =========================
# SERVER URL (ADMIN POSTGRES)
# =========================
def get_server_url() -> str:
    source = get_source_url()
    parsed = urlparse(source)

    return urlunparse(parsed._replace(path="/postgres"))


# =========================
# NOME DO BANCO
# =========================
def get_source_db_name() -> str:
    return urlparse(get_source_url()).path.lstrip("/")


def get_dest_db_name() -> str:
    parsed = urlparse(get_dest_url())
    return parsed.path.lstrip("/")


# =========================
# SEGURANÇA DE URL
# =========================
def _sanitize_db_url(url: str) -> str:
    return url.strip().encode("utf-8", "ignore").decode("utf-8")