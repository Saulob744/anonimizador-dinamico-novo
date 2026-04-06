import os
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv

load_dotenv()


def get_source_url() -> str:
    url = os.getenv("DB_SOURCE")
    if not url:
        raise ValueError(
            "A variável de ambiente DB_SOURCE não está definida.\n"
            "Crie um arquivo .env com: DB_SOURCE=postgresql://user:pass@host:5432/banco"
        )
    return url


def get_dest_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    db_name = parsed.path.lstrip("/")
    new_db_name = f"{db_name}_anon"
    new_path = f"/{new_db_name}"
    new_parsed = parsed._replace(path=new_path)
    return urlunparse(new_parsed)


def get_server_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    new_parsed = parsed._replace(path="/postgres")
    return urlunparse(new_parsed)


def get_dest_db_name(source_url: str) -> str:
    parsed = urlparse(source_url)
    db_name = parsed.path.lstrip("/")
    return f"{db_name}_anon"


def get_source_db_name(source_url: str) -> str:
    parsed = urlparse(source_url)
    return parsed.path.lstrip("/")
