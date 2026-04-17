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
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "CEP": re.compile(r"\b\d{5}-?\d{3}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "CHASSI": re.compile(r"\b(?=.*\d)[A-HJ-NPR-Z0-9]{10,17}\b", re.IGNORECASE),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),

    # código mais restrito
    "CODE": re.compile(r"\b[A-Z]{2,5}-\d+[A-Z0-9]*\b")
}

# =========================
# REGEX NOME (AJUSTADO)
# =========================

REGEX_NAME = re.compile(
    r'\b('
    r'(?:[A-ZÁÀÂÃÉÈÍÓÚÇ]{2,}|[A-ZÁÀÂÃÉÈÍÓÚÇ][a-záàâãéèíóúç]+)'
    r'(?:\s+(?:da|de|do|dos|das))?'
    r'(?:\s+(?:[A-ZÁÀÂÃÉÈÍÓÚÇ]{2,}|[A-ZÁÀÂÃÉÈÍÓÚÇ][a-záàâãéèíóúç]+)){1,2}'
    r')\b'
)

# =========================
# MEMÓRIA
# =========================

_memory = {}

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
    key = (_normalize_name(val) if cat == "PER" else _normalize(val))

    if cat not in _memory:
        _memory[cat] = {}

    if key not in _memory[cat]:
        _memory[cat][key] = fn()

    return _memory[cat][key]

# =========================
# VALIDAÇÃO INTELIGENTE
# =========================

def _is_valid_name(n):

    n = n.strip()

    # 🔴 evita frases grandes (proteção geral)
    if len(n) > 40:
        return False

    partes = n.split()

    # regra básica
    if len(partes) < 2 or len(partes) > 4:
        return False

    # 🔴 BLOQUEIO EXTRA: padrão marca/modelo (2 ou 3 palavras)
    if n.isupper() and 2 <= len(partes) <= 3:
        if sum(1 for p in partes if len(p) <= 6) >= 2:
            return False

    # 🔴 BLOQUEIO: endereço (primeira palavra curta tipo Rua/Av)
    if len(partes[0]) <= 4 and partes[0][0].isupper():
        return False

    # 🔴 evita romano (XV de Novembro)
    if re.match(r"^[IVXLCDM]+\b", partes[0]):
        return False

    # não pode ter número
    if any(re.search(r"\d", p) for p in partes):
        return False

    score = 0

    # =========================
    # 1. CAPITALIZAÇÃO (FORTE)
    # =========================
    for p in partes:
        if p[0].isupper():
            score += 2
        if p.isupper():
            score += 1

    # =========================
    # 2. TAMANHO DAS PALAVRAS
    # =========================
    tamanhos = [len(p) for p in partes]

    if all(3 <= len(p) <= 12 for p in partes):
        score += 2

    if max(tamanhos) - min(tamanhos) >= 2:
        score += 1

    # 🔴 penaliza palavras muito longas (texto disfarçado)
    if any(len(p) > 15 for p in partes):
        score -= 2

    # =========================
    # 3. ESTRUTURA NATURAL
    # =========================
    conectores = {"da", "de", "do", "dos", "das"}
    if any(p.lower() in conectores for p in partes):
        score += 1

    # =========================
    # 4. PENALIDADES
    # =========================

    # padrão marca/modelo curto (VW GOL)
    if n.isupper() and len(partes) == 2:
        if all(len(p) <= 5 for p in partes):
            score -= 4

    # palavras com mesmo tamanho (muito artificial)
    if len(set(tamanhos)) == 1:
        score -= 2

    # tudo muito curto
    if sum(1 for p in partes if len(p) <= 4) == len(partes):
        score -= 2

    # duas palavras grandes técnicas
    if len(partes) == 2 and n.isupper():
        if all(len(p) >= 6 for p in partes):
            score -= 3

    # 🔴 penaliza tudo minúsculo (texto comum)
    if n.islower():
        score -= 2

    # 🔴 penaliza mistura estranha (ex: texto quebrado)
    if sum(1 for p in partes if p[0].isupper()) < 2:
        score -= 2

    # =========================
    # 5. DECISÃO FINAL
    # =========================

    return score >= 3

# =========================
# NAMES
# =========================

def _replace_names(text):

    def repl(match):
        nome = match.group()

        # valida se é nome real
        if not _is_valid_name(nome):
            return nome

        # usa sempre mesmo fake consistente
        return _get(nome, "PER", lambda: fake.first_name().upper() + " " + fake.last_name().upper())

    return REGEX_NAME.sub(repl, text)

# =========================
# TEXTO LIVRE
# =========================

def anonymize_text(val):

    if not isinstance(val, str):
        return val

    text = val

    def repl(cat, fn):
        return lambda m: _get(m.group(), cat, fn)

    text = REGEX["CPF"].sub(repl("CPF", fake.cpf), text)
    text = REGEX["RG"].sub(repl("RG", fake.rg), text)
    text = REGEX["PHONE"].sub(repl("PHONE", fake.phone_number), text)
    text = REGEX["EMAIL"].sub(repl("EMAIL", fake.email), text)
    text = REGEX["CEP"].sub(repl("CEP", fake.postcode), text)
    text = REGEX["PLATE"].sub(repl("PLATE", fake.license_plate), text)

    # nomes
    text = _replace_names(text)

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
# DETECTOR DE CÓDIGO
# =========================

def is_probably_code(text):
    if not isinstance(text, str):
        return False

    if len(text) > 40:
        return False

    if REGEX["CODE"].fullmatch(text.strip()):
        return True

    return False

# =========================
# FUNÇÃO PRINCIPAL
# =========================

def anonymize_value(col, val, is_numeric=False):

    if val is None:
        return val, None

    val_str = str(val)
    col_lower = col.lower()

    # 🔥 código isolado
    if is_probably_code(val_str):
        return _get(val_str, "CODE", lambda: mask_pattern(val_str)), "CODE"

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

    new_val = anonymize_text(val_str)

    if new_val != val_str:
        return new_val, "TEXT"

    return val_str, None