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
    "RG": re.compile(r"\b\d{1,2}\.?\d{3,5}\.?\d{3}-?[0-9Xx]\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9?\d{4})-?\d{4}\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "CEP": re.compile(r"\b\d{5}-?\d{3}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "CHASSI": re.compile(r"\b[A-HJ-NPR-Z0-9]{10,17}\b", re.IGNORECASE),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}

# nomes mais realistas
REGEX_NAME = re.compile(
    r'\b[A-ZÁÀÂÃÉÈÍÓÚÇ][a-záàâãéèíóúç]+'
    r'(?:\s+[A-ZÁÀÂÃÉÈÍÓÚÇ][a-záàâãéèíóúç]+){1,3}\b'
)

# =========================
# MEMÓRIA
# =========================

_memory = {
    "PER": {},
    "CPF": {},
    "RG": {},
    "PHONE": {},
    "EMAIL": {},
    "CEP": {},
    "PLATE": {},
    "CHASSI": {},
    "IP": {},
    "ID": {}
}

# =========================
# HELPERS
# =========================

def only_digits(v):
    return re.sub(r"\D", "", str(v))


def limit(val, size):
    return str(val)[:size]


def _normalize(v):
    return str(v).strip().upper()


# 🔧 FIX IMPORTANTE: função segura e consistente
def _get(val, cat, fn):
    v = _normalize(val)

    if cat not in _memory:
        _memory[cat] = {}

    if v not in _memory[cat]:
        _memory[cat][v] = fn()

    return _memory[cat][v]


def mask_pattern(x):
    return "".join(
        random.choice(string.digits) if c.isdigit()
        else random.choice(string.ascii_uppercase) if c.isalpha()
        else c
        for c in str(x)
    )

# =========================
# NAMES DETECTOR
# =========================

def _replace_names(text):
    nomes = REGEX_NAME.findall(text)

    for n in set(nomes):
        text = text.replace(n, fake.name().upper())

    return text


# =========================
# TEXTO LIVRE (INTELIGENTE E NÃO AGRESSIVO)
# =========================

def anonymize_text(val):
    if not isinstance(val, str):
        return val

    text = val

    # nomes primeiro
    text = _replace_names(text)

    def repl(cat, fn):
        return lambda m: _get(m.group(), cat, fn)

    # substituições seguras
    text = REGEX["CPF"].sub(repl("CPF", fake.cpf), text)
    text = REGEX["RG"].sub(repl("RG", fake.rg), text)
    text = REGEX["PHONE"].sub(repl("PHONE", fake.phone_number), text)
    text = REGEX["EMAIL"].sub(repl("EMAIL", fake.email), text)
    text = REGEX["CEP"].sub(repl("CEP", fake.postcode), text)
    text = REGEX["PLATE"].sub(repl("PLATE", fake.license_plate), text)
    text = REGEX["CHASSI"].sub(
    lambda m: _get(m.group(), "CHASSI", lambda: mask_pattern(m.group())),
    text
        )

    text = REGEX["IP"].sub(
        lambda m: _get(m.group(), "IP", lambda: "0.0.0.0"),
        text
    )

    return limit(text, 500)


# =========================
# FUNÇÃO PRINCIPAL (CONTROLADA)
# =========================

def anonymize_value(col, val, is_numeric=False):

    if val is None:
        return val, None

    val_str = str(val)
    col_lower = col.lower()

    # =========================
    # COLUNAS EXPLÍCITAS
    # =========================

    if "cpf" in col_lower:
        return _get(val_str, "CPF", fake.cpf), "CPF"

    if "rg" in col_lower:
        return _get(val_str, "RG", fake.rg), "RG"

    if "telefone" in col_lower or "fone" in col_lower or "celular" in col_lower:
        return _get(val_str, "PHONE", fake.phone_number), "PHONE"

    if "email" in col_lower:
        return _get(val_str, "EMAIL", fake.email), "EMAIL"

    if "nome" in col_lower or "usuario" in col_lower:
        return _get(val_str, "PER", lambda: fake.name().upper()), "PER"

    # =========================
    # DETECÇÃO DINÂMICA (MENOS AGRESSIVA)
    # =========================

    has_pii_pattern = any(
        r.search(val_str)
        for r in REGEX.values()
    )

    has_name = bool(REGEX_NAME.search(val_str))

    # só anonimiza se REALMENTE parecer dado sensível
    if has_pii_pattern or has_name:
        new_val = anonymize_text(val_str)

        if new_val != val_str:
            return new_val, "TEXT"

    # =========================
    # NÃO MEXE EM TEXTO NORMAL
    # =========================

    return val_str, None