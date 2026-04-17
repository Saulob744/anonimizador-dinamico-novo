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
    "PHONE": re.compile(
        r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})-?\d{4}\b"
    ),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "CEP": re.compile(r"\b\d{5}-?\d{3}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "CHASSI": re.compile(r"\b(?=.*\d)[A-HJ-NPR-Z0-9]{10,17}\b", re.IGNORECASE),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}

# nomes mais realistas
REGEX_NAME = re.compile(
    r'\b(?:'
    r'[A-ZÁÀÂÃÉÈÍÓÚÇ]{2,}(?:\s+[A-ZÁÀÂÃÉÈÍÓÚÇ]{2,}){1,3}'  # CAPS
    r'|'
    r'[A-ZÁÀÂÃÉÈÍÓÚÇ][a-záàâãéèíóúç]+(?:\s+[a-záàâãéèíóúç]+){1,3}'  # normal
    r')\b',
    re.IGNORECASE
)

# =========================
# MEMÓRIA (CONSISTÊNCIA GLOBAL)
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
    "IP": {}
}

# =========================
# HELPERS
# =========================

def _normalize(v):
    return " ".join(str(v).strip().upper().split())


def _normalize_name(v):
    return " ".join(str(v).strip().lower().split())


def limit(val, size):
    return str(val)[:size]


def mask_pattern(x):
    return "".join(
        random.choice(string.digits) if c.isdigit()
        else random.choice(string.ascii_uppercase) if c.isalpha()
        else c
        for c in str(x)
    )


def _get(val, cat, fn):
    v = _normalize(val)

    if cat not in _memory:
        _memory[cat] = {}

    if v not in _memory[cat]:
        _memory[cat][v] = fn()

    return _memory[cat][v]

# =========================
# VALIDAÇÃO DE NOMES
# =========================

def _is_valid_name(n):
    partes = n.split()

    if len(partes) < 2 or len(partes) > 4:
        return False

    # evita lixo tipo "DE DA DOS"
    valid_parts = [p for p in partes if len(p) > 2]

    if len(valid_parts) < 2:
        return False

    return True

# =========================
# NAMES DETECTOR (SEGURO)
# =========================

def _replace_names(text):
    nomes = set(REGEX_NAME.findall(text))

    for n in sorted(nomes, key=len, reverse=True):

        if not _is_valid_name(n):
            continue

        fake_name = _get(_normalize_name(n), "PER", lambda: fake.name().upper())

        # protege contra variações (case insensitive real)
        text = re.sub(
            rf"(?<!\w){re.escape(n)}(?!\w)",
            fake_name,
            text,
            flags=re.IGNORECASE
        )

    return text

# =========================
# TEXTO LIVRE
# =========================

def anonymize_text(val):
    if not isinstance(val, str):
        return val

    text = val

    # 1. PII estruturado primeiro
    def repl(cat, fn):
        return lambda m: _get(m.group(), cat, fn)

    text = REGEX["CPF"].sub(repl("CPF", fake.cpf), text)
    text = REGEX["RG"].sub(repl("RG", fake.rg), text)
    text = REGEX["PHONE"].sub(repl("PHONE", fake.phone_number), text)
    text = REGEX["EMAIL"].sub(repl("EMAIL", fake.email), text)
    text = REGEX["CEP"].sub(repl("CEP", fake.postcode), text)
    text = REGEX["PLATE"].sub(repl("PLATE", fake.license_plate), text)

    # 2. nomes depois (evita conflito com CPF/RG etc)
    text = _replace_names(text)

    # 3. CHASSI (mais seguro)
    text = REGEX["CHASSI"].sub(
        lambda m: _get(m.group(), "CHASSI", lambda: mask_pattern(m.group())),
        text
    )

    # 4. IP fixo (ou mascarado se quiser evoluir depois)
    text = REGEX["IP"].sub(
        lambda m: _get(m.group(), "IP", lambda: "0.0.0.0"),
        text
    )

    return limit(text, 500)

# =========================
# FUNÇÃO PRINCIPAL
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

    if any(x in col_lower for x in ["telefone", "fone", "celular"]):
        return _get(val_str, "PHONE", fake.phone_number), "PHONE"

    if "email" in col_lower:
        return _get(val_str, "EMAIL", fake.email), "EMAIL"

    if "nome" in col_lower or "usuario" in col_lower:
        return _get(val_str, "PER", lambda: fake.name().upper()), "PER"

    # =========================
    # DETECÇÃO DINÂMICA
    # =========================

    has_pii_pattern = any(r.search(val_str) for r in REGEX.values())
    has_name = bool(REGEX_NAME.search(val_str))

    if has_pii_pattern or has_name:
        new_val = anonymize_text(val_str)

        if new_val != val_str:
            return new_val, "TEXT"

    # =========================
    # DEFAULT
    # =========================

    return val_str, None