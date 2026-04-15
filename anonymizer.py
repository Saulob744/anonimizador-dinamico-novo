import re
import spacy
import random
import string
from faker import Faker

fake = Faker("pt_BR")

# ==================================================
# REGEX
# ==================================================
REGEX = {
    "CPF": re.compile(r"\d{3}\.?\d{3}\.?\d{3}-?\d{2}"),
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b"),
    "PHONE": re.compile(r"(\(?\d{2}\)?\s?\d{4,5}-?\d{4})"),
    "CODE": re.compile(r"\b(?=.*[A-Z])(?=.*\d)[A-Z0-9\.\-/]{5,25}\b")
}

# memória de consistência
_memory = {"PER": {}, "CPF": {}, "EMAIL": {}, "PHONE": {}, "ID": {}}

# IA
try:
    nlp = spacy.load("pt_core_news_sm", disable=["parser", "lemmatizer", "textcat"])
except:
    nlp = None

# ==================================================
# UTILIDADES CRÍTICAS (🔥 NOVO)
# ==================================================

def only_digits(val):
    return re.sub(r"\D", "", str(val))

def limit_size(val, max_len=255):
    val = str(val)
    return val[:max_len]

# ==================================================
# CONSISTÊNCIA
# ==================================================

def _get_consistent(val, category, fn_create):
    v = str(val).strip().upper()
    if not v or v.lower() == "none":
        return v

    if v not in _memory[category]:
        _memory[category][v] = fn_create(v)

    return _memory[category][v]

# ==================================================
# PADRÕES
# ==================================================

def mask_pattern(orig):
    res = []
    for char in orig:
        if char.isdigit():
            res.append(random.choice(string.digits))
        elif char.isalpha():
            res.append(random.choice(string.ascii_uppercase))
        else:
            res.append(char)
    return "".join(res)

# ==================================================
# TEXTO LONGO
# ==================================================

def anonymize_text_block(text):
    if not text or len(text) < 3:
        return text

    # nomes já vistos
    sorted_names = sorted(_memory["PER"].keys(), key=len, reverse=True)
    for orig_name in sorted_names:
        if len(orig_name) > 3:
            pattern = re.compile(re.escape(orig_name), re.IGNORECASE)
            text = pattern.sub(_memory["PER"][orig_name], text)

    # CPF
    text = REGEX["CPF"].sub(
        lambda m: _get_consistent(
            m.group(), "CPF", lambda x: only_digits(fake.cpf())
        ),
        text,
    )

    # EMAIL
    text = REGEX["EMAIL"].sub(
        lambda m: _get_consistent(m.group(), "EMAIL", lambda x: fake.email()),
        text,
    )

    # PHONE
    text = REGEX["PHONE"].sub(
        lambda m: _get_consistent(
            m.group(), "PHONE", lambda x: only_digits(fake.cellphone_number())
        ),
        text,
    )

    # IA
    text_for_ai = text.title() if text.islower() else text
    if nlp:
        doc = nlp(text_for_ai)
        for ent in reversed(doc.ents):
            if ent.label_ == "PER":
                fake_name = _get_consistent(
                    ent.text, "PER", lambda x: fake.name().upper()
                )
                text = text[:ent.start_char] + fake_name + text[ent.end_char:]

    # códigos mistos
    words = text.split()
    new_words = []
    for word in words:
        if len(word) > 5 and any(c.isdigit() for c in word) and any(c.isalpha() for c in word):
            new_words.append(mask_pattern(word))
        else:
            new_words.append(word)

    return " ".join(new_words)

# ==================================================
# FUNÇÃO PRINCIPAL
# ==================================================

def anonymize_value(col_name, value, is_numeric=False):
    if value is None or str(value).strip() == "":
        return value, None

    val_orig = str(value).strip()
    col_lower = col_name.lower()

    # ==================================================
    # 1. NOMES
    # ==================================================
    if any(k in col_lower for k in ["nome", "proprietario", "autor", "vitima", "usuario"]):
        return _get_consistent(
            val_orig, "PER", lambda x: limit_size(fake.name().upper(), 120)
        ), "PER"

    # ==================================================
    # 2. CPF DIRETO (🔥 FIX PRINCIPAL)
    # ==================================================
    if "cpf" in col_lower:
        return _get_consistent(
            val_orig, "CPF", lambda x: only_digits(fake.cpf())
        ), "DOCS"

    # ==================================================
    # 3. TEXTOS LONGOS
    # ==================================================
    if len(val_orig) > 30 or " " in val_orig:
        return limit_size(anonymize_text_block(val_orig), 500), "TEXT"

    # ==================================================
    # 4. CÓDIGOS / IDS
    # ==================================================
    if 4 < len(val_orig) < 30 and any(c.isdigit() for c in val_orig) and any(c.isalpha() for c in val_orig):
        return _get_consistent(
            val_orig, "ID", lambda x: mask_pattern(x)
        ), "DOCS"

    # ==================================================
    # 5. DETECÇÃO AUTOMÁTICA (CPF)
    # ==================================================
    val_clean = only_digits(val_orig)
    if len(val_clean) == 11:
        return _get_consistent(
            val_orig, "CPF", lambda x: only_digits(fake.cpf())
        ), "DOCS"

    return val_orig, None