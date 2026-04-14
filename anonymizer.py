import re
import spacy
import random
import string
from faker import Faker

fake = Faker("pt_BR")

try:
    nlp = spacy.load("pt_core_news_lg", disable=["parser", "lemmatizer", "textcat"])
except:
    nlp = None

DEBUG = True  #  LIGA/DESLIGA DEBUG

def log(msg):
    if DEBUG:
        print(msg)

# ========================
# MEMÓRIA
# ========================
_memory = {
    "PER": {},
    "CPF": {},
    "EMAIL": {},
    "PHONE": {},
    "ID": {}
}

_used = set()

# ========================
# REGEX
# ========================
REGEX = {
    "CPF": re.compile(r"\b\d{11}\b"),
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})[-\s]?\d{4}\b"),
}

# ========================
# HELPERS
# ========================
def _get(key, category, fn):
    key = str(key)
    if key not in _memory[category]:
        _memory[category][key] = fn(key)
        log(f"[NEW] {category}: {key} -> {_memory[category][key]}")
    else:
        log(f"[CACHE] {category}: {key}")
    return _memory[category][key]

def fake_name():
    return f"{fake.first_name()} {fake.last_name()}"

def fake_number_like(orig):
    while True:
        new = "".join(random.choice(string.digits) for _ in orig)
        if new not in _used:
            _used.add(new)
            return new

def fake_mixed(orig):
    while True:
        res = ""
        for c in orig:
            if c.isdigit():
                res += random.choice(string.digits)
            elif c.isalpha():
                res += random.choice(string.ascii_letters)
            else:
                res += c
        if res not in _used:
            _used.add(res)
            return res

# ========================
# NLP
# ========================
def classify(val):
    val = str(val).strip()

    if not val:
        return "EMPTY"

    if nlp:
        doc = nlp(val)

        if any(token.pos_ == "VERB" for token in doc):
            return "TEXT"

        if any(ent.label_ == "PER" for ent in doc.ents) and len(doc) <= 4:
            return "NAME"

    return "TEXT"

# ========================
# TEXTO
# ========================
def anonymize_text(text):
    if not text:
        return text

    log(f"[TEXT] {text}")

    text = REGEX["CPF"].sub(
        lambda m: _get(m.group(), "CPF", lambda x: fake_number_like(x)),
        text
    )

    text = REGEX["EMAIL"].sub(
        lambda m: _get(m.group(), "EMAIL", lambda x: fake.email()),
        text
    )

    text = REGEX["PHONE"].sub(
        lambda m: _get(m.group(), "PHONE", lambda x: fake.phone_number()),
        text
    )

    if nlp:
        doc = nlp(text)
        for ent in reversed(doc.ents):
            if ent.label_ == "PER":
                fake_n = _get(ent.text, "PER", lambda x: fake_name())
                text = text[:ent.start_char] + fake_n + text[ent.end_char:]

    return text

# ========================
# 🚀 PRINCIPAL COM DEBUG
# ========================
def anonymize_value(col, value, col_type=None):
    if value is None:
        return None, None

    val = str(value).strip()

    log(f"\n[INPUT] COL={col} VAL={val}")

    #  CPF FORÇADO
    if val.isdigit() and len(val) == 11:
        log("→ CPF DETECTADO")
        return _get(val, "CPF", lambda x: fake_number_like(x)), "DOCS"

    # EMAIL
    if REGEX["EMAIL"].fullmatch(val):
        log("→ EMAIL")
        return _get(val, "EMAIL", lambda x: fake.email()), "CONTACTS"

    # PHONE
    if REGEX["PHONE"].fullmatch(val):
        log("→ PHONE")
        return _get(val, "PHONE", lambda x: fake.phone_number()), "CONTACTS"

    #  NUMÉRICO GRANDE
    if val.isdigit():
        if len(val) >= 9:
            log("→ NUMERO GRANDE (DOC)")
            return _get(val, "ID", lambda x: fake_number_like(x)), "DOCS"
        else:
            log("→ NUMERO PEQUENO (IGNORADO)")
            return value, None

    #  MIXED
    if any(c.isdigit() for c in val) and any(c.isalpha() for c in val):
        log("→ CODIGO MISTO")
        return _get(val, "ID", lambda x: fake_mixed(x)), "DOCS"

    #  UPPERCASE CODE
    if val.isupper() and len(val) >= 6 and " " not in val:
        log("→ CODIGO UPPER")
        return _get(val, "ID", lambda x: fake_mixed(x)), "DOCS"

    # NLP
    tipo = classify(val)
    log(f"→ NLP TYPE: {tipo}")

    if tipo == "NAME":
        log("→ NOME")
        return _get(val, "PER", lambda x: fake_name()), "PER"

    log("→ TEXTO")
    return anonymize_text(val), "TEXT"