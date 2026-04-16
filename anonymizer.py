import re
import random
import string
from faker import Faker

fake = Faker("pt_BR")

# =========================
# REGEX SENSÍVEIS
# =========================

REGEX = {
    "CPF": re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    "RG": re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}-?[0-9Xx]\b"),
    "PHONE": re.compile(r"\b(?:\(?\d{2}\)?\s?)?(?:9?\d{4})-?\d{4}\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
}

# nomes completos em maiúsculo ou misto
REGEX_NAME = re.compile(
    r'\b[A-ZÁÀÂÃÉÈÍÓÚÇ]{2,}(?:\s+[A-ZÁÀÂÃÉÈÍÓÚÇa-záàâãéèíóúç]{2,})+\b'
)

_memory = {"PER": {}, "CPF": {}, "RG": {}, "PHONE": {}, "EMAIL": {}, "ID": {}}


# =========================
# HELPERS
# =========================

def only_digits(v):
    return re.sub(r"\D", "", str(v))


def limit(val, size):
    return str(val)[:size]


def _get(val, cat, fn):
    v = str(val).upper()
    if v not in _memory[cat]:
        _memory[cat][v] = fn(v)
    return _memory[cat][v]


def mask_pattern(x):
    return "".join(
        random.choice(string.digits) if c.isdigit()
        else random.choice(string.ascii_uppercase) if c.isalpha()
        else c
        for c in x
    )


# =========================
# TEXTO LIVRE (CENTRAL)
# =========================

def anonymize_text(val):
    if not isinstance(val, str):
        return val

    # NOMES
    nomes = REGEX_NAME.findall(val)
    for nome in set(nomes):
        val = val.replace(nome, fake.name().upper())

    # CPF
    val = REGEX["CPF"].sub(lambda m: fake.cpf(), val)

    # RG
    val = REGEX["RG"].sub(lambda m: fake.rg(), val)

    # TELEFONE
    val = REGEX["PHONE"].sub(lambda m: fake.phone_number(), val)

    # EMAIL
    val = REGEX["EMAIL"].sub(lambda m: fake.email(), val)

    return limit(val, 500)


# =========================
# FUNÇÃO PRINCIPAL
# =========================

def anonymize_value(col, val, is_numeric=False):

    if val is None:
        return val, None

    val = str(val)
    col_lower = col.lower()

    # =========================
    # COLUNAS SENSÍVEIS DIRETAS
    # =========================

    if "cpf" in col_lower:
        return _get(val, "CPF", lambda _: fake.cpf()), "CPF"

    if "rg" in col_lower:
        return _get(val, "RG", lambda _: fake.rg()), "RG"

    if "telefone" in col_lower or "fone" in col_lower or "celular" in col_lower:
        return _get(val, "PHONE", lambda _: fake.phone_number()), "PHONE"

    if "email" in col_lower:
        return _get(val, "EMAIL", lambda _: fake.email()), "EMAIL"

    if "nome" in col_lower or "usuario" in col_lower or "usuário" in col_lower:
        return _get(val, "PER", lambda _: fake.name().upper()), "PER"

    # =========================
    # TEXTO LIVRE (ALTAMENTE IMPORTANTE)
    # =========================

    if len(val) > 0:
        val = anonymize_text(val)
        return val, "TEXT"

    # =========================
    # CÓDIGOS
    # =========================

    if any(c.isdigit() for c in val) and any(c.isalpha() for c in val):
        return _get(val, "ID", lambda _: mask_pattern(val)), "DOCS"

    return val, None