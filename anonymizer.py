import re
import random
import string
from faker import Faker

fake = Faker("pt_BR")

REGEX = {
    "CPF": re.compile(r"\d{3}\.?\d{3}\.?\d{3}-?\d{2}")
}

_memory = {"PER": {}, "CPF": {}, "ID": {}}

def only_digits(v):
    return re.sub(r"\D", "", str(v))[:11]

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
        else c for c in x
    )

def anonymize_value(col, val, is_numeric=False):

    if val is None:
        return val, None

    val = str(val)
    col = col.lower()

    # NOME
    if "nome" in col:
        return _get(val, "PER", lambda _: limit(fake.name().upper(), 120)), "PER"

    # CPF (🔥 FIX)
    if "cpf" in col:
        return _get(val, "CPF", lambda _: only_digits(fake.cpf())), "DOCS"

    # TEXTO
    if len(val) > 30:
        val = REGEX["CPF"].sub(lambda m: only_digits(fake.cpf()), val)
        return limit(val, 500), "TEXT"

    # CODIGO
    if any(c.isdigit() for c in val) and any(c.isalpha() for c in val):
        return _get(val, "ID", lambda _: mask_pattern(val)), "DOCS"

    return val, None