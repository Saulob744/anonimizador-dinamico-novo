import re
import spacy
import random
import string
import unicodedata
from faker import Faker

fake = Faker("pt_BR")
nlp = spacy.load("pt_core_news_lg")

_memory = {}

# ==================================================
# REGEX (mantido)
# ==================================================
REGEX = {
    "CPF": re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    "RG": re.compile(r"\b\d{1,2}\.?\d{3,5}\.?\d{3}-?[0-9Xx]\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
}

# ==================================================
# NORMALIZAÇÃO
# ==================================================
def _normalize(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.upper()

def _key(val, cat):
    return _normalize(val) if cat == "PER" else str(val)

def _get(val, cat, generator):
    k = _key(val, cat)
    if cat not in _memory:
        _memory[cat] = {}
    if k not in _memory[cat]:
        _memory[cat][k] = generator()
    return _memory[cat][k]

# ==================================================
# NOVO: SCORE DE ENTIDADE (SEM BLACKLIST)
# ==================================================
def _is_person_candidate(text):
    if not text or len(text) < 3:
        return False

    words = text.strip().split()

    # regra estrutural forte
    if not (2 <= len(words) <= 4):
        return False

    # rejeita números
    if any(c.isdigit() for c in text):
        return False

    # precisa ter padrão nome (capitalização OU misto)
    caps = sum(1 for w in words if w[:1].isupper())
    if caps < 1:
        return False

    return True

# ==================================================
# DETECÇÃO (MELHORADA)
# ==================================================
def _detect_names(text):
    found = []

    doc = nlp(text)

    # 1. spaCy
    for ent in doc.ents:
        if ent.label_ == "PER" and _is_person_candidate(ent.text):
            found.append((ent.start_char, ent.end_char, ent.text))

    # 2. regex controlado (mais preciso)
    for m in re.finditer(r"\b[A-ZÀ-Ÿ][a-zà-ÿ]+(?:\s+[A-ZÀ-Ÿ][a-zà-ÿ]+){1,3}\b", text):
        val = m.group()
        if _is_person_candidate(val):
            found.append((m.start(), m.end(), val))

    return found

# ==================================================
# OVERLAP FIX (CORRIGIDO)
# ==================================================
def _resolve_overlaps(items):
    items = sorted(items, key=lambda x: (x[0], -(x[1]-x[0])))
    out = []

    for s, e, v, t in items:
        if not any(rs <= s < re for rs, re, _, _ in out):
            out.append((s, e, v, t))

    return out

# ==================================================
# ANONIMIZAÇÃO PRINCIPAL
# ==================================================
def anonymize_text(text, is_vehicle_col=False):
    if not isinstance(text, str) or not text:
        return text

    entities = []

    # NOMES
    if not is_vehicle_col:
        for s, e, v in _detect_names(text):
            entities.append((s, e, v, "PER"))

    # REGEX
    for typ, pattern in REGEX.items():
        for m in pattern.finditer(text):
            entities.append((m.start(), m.end(), m.group(), typ))

    entities = _resolve_overlaps(entities)

    result = []
    last = 0

    for s, e, v, typ in sorted(entities, key=lambda x: x[0]):
        result.append(text[last:s])

        if typ == "PER":
            repl = _get(v, "PER", lambda: fake.name().upper())

        elif typ == "CPF":
            repl = _get(v, "CPF", fake.cpf)

        elif typ == "RG":
            repl = _get(v, "RG", lambda: fake.bothify("##.###.###-#"))

        elif typ == "EMAIL":
            repl = _get(v, "EMAIL", fake.email)

        elif typ == "PLATE":
            repl = _get(v, "PLATE", lambda: fake.license_plate().upper())

        elif typ == "PHONE":
            repl = _get(v, "PHONE", fake.cellphone_number)

        else:
            repl = _get(v, typ, lambda: "".join(
                random.choice(string.digits) if c.isdigit()
                else random.choice(string.ascii_uppercase) if c.isalpha()
                else c for c in v
            ))

        result.append(repl)
        last = e

    result.append(text[last:])
    return "".join(result)

# ==================================================
# INTERFACE DB
# ==================================================
def anonymize_value(col, val):

    if val is None:
        return val, None

    if isinstance(val, (int, float)) or col.lower().endswith("_id"):
        return val, None

    val_str = str(val)
    col_lower = col.lower()

    # colunas nome
    if any(x in col_lower for x in ["nome", "usuario", "cliente", "proprietario"]):
        return _get(val_str, "PER", lambda: fake.name().upper()), "PER"

    is_vehicle_col = any(x in col_lower for x in ["modelo", "marca", "veiculo"])

    new_val = anonymize_text(val_str, is_vehicle_col)

    return new_val, "TEXT" if new_val != val_str else None