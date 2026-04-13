import re
import spacy
import random
import string
from faker import Faker

fake = Faker("pt_BR")

try:
    nlp = spacy.load("pt_core_news_lg", disable=["parser", "lemmatizer", "textcat"])
except Exception:
    nlp = None

_memory = {
    "PER": {}, "CPF": {}, "EMAIL": {}, "PHONE": {}, 
    "ID_DYNAM": {}, "ID_NUMERIC": {}, "RG": {}
}
_used_fakes = set()

REGEX_RULES = {
    "CPF": re.compile(r"(\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b)"),
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b"),
    "PHONE": re.compile(r"(\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})[-\s]?\d{4}\b)"),
}

# ========================
# FUNÇÕES DE APOIO
# ========================

def _clean_val(val):
    return re.sub(r'[^a-zA-Z0-9]', '', str(val))

def _get_consistent(val, category, func):
    v = str(val).strip()
    if v not in _memory[category]:
        _memory[category][v] = func(v)
    return _memory[category][v]

def _gen_fake_numeric(orig):
    orig_clean = _clean_val(orig)
    if not orig_clean: return random.randint(10000, 99999)
    res = "".join(random.choice("123456789") if i==0 else random.choice("0123456789") for i in range(len(orig_clean)))
    return int(res)

def _gen_structure_preserved_id(orig):
    """Gera um fake mantendo rigorosamente a estrutura original (inclusive RGs curtos)."""
    while True:
        res = ""
        for char in orig:
            if char.isupper(): res += random.choice(string.ascii_uppercase)
            elif char.islower(): res += random.choice(string.ascii_lowercase)
            elif char.isdigit(): res += random.choice(string.digits)
            else: res += char 
        if res not in _used_fakes:
            _used_fakes.add(res)
            return res

def is_likely_an_id(s):
    """
    Detecta se uma string é um ID Único (RG, Chassi, Placa, Protocolo).
    Melhorado para capturar sequências numéricas a partir de 5 dígitos.
    """
    s_clean = _clean_val(s)
    # Ignora datas (8 dígitos com barras ou traços geralmente são datas)
    if "/" in s or (len(s_clean) == 8 and "-" in s): return False
    
    # Se tem entre 5 e 25 caracteres e não tem espaços
    if 5 <= len(s_clean) <= 25 and " " not in s.strip():
        # Caso 1: Mistura de letras e números (Chassi, RG com UF, Placa)
        if any(c.isdigit() for c in s_clean) and any(c.isalpha() for c in s_clean):
            return True
        # Caso 2: Apenas números mas em formato de ID (RG antigo, IDs de sistema)
        if s_clean.isdigit():
            return True
            
    return False

# ========================
# NÚCLEO DE ANONIMIZAÇÃO
# ========================

def anonymize_value(col_name, value, is_numeric=False):
    if value is None: return None, None
    
    val_str = str(value).strip()
    col_lower = str(col_name).lower()

    # 1. TRATAMENTO NUMÉRICO (BIGINT/INT)
    if is_numeric:
        return _get_consistent(val_str, "ID_NUMERIC", _gen_fake_numeric), "DOCS"

    # 2. IDENTIFICAÇÃO POR PREFIXO (ENVOLVIDO - , CHASSI - )
    if " - " in val_str:
        prefix, content = val_str.split(" - ", 1)
        up_prefix = prefix.upper()
        if any(k in up_prefix for k in ["ENVOLVIDO", "AUTOR", "VITIMA", "CONDUTOR"]):
            fake_n = _get_consistent(content, "PER", lambda x: f"{fake.first_name()} {fake.last_name()}".upper())
            return f"{prefix} - {fake_n}", "PER"
        if any(k in up_prefix for k in ["CHASSI", "PLACA", "RG", "CPF", "DOC"]):
            fake_id = _get_consistent(content, "ID_DYNAM", _gen_structure_preserved_id)
            return f"{prefix} - {fake_id}", "DOCS"

    # 3. IDENTIFICAÇÃO POR NOME DE COLUNA (PESSOAS E RG)
    if any(k in col_lower for k in ["nome", "usuario", "pessoa", "autor", "vitima", "proprietario"]):
        fake_n = _get_consistent(val_str, "PER", lambda x: f"{fake.first_name()} {fake.last_name()}".upper())
        return fake_n, "PER"
    
    if "rg" in col_lower:
        return _get_consistent(val_str, "RG", _gen_structure_preserved_id), "DOCS"

    # 4. REGEX DE DOCUMENTOS PADRÃO (CPF, EMAIL)
    if REGEX_RULES["CPF"].fullmatch(val_str):
        fake_cpf = _get_consistent(val_str, "CPF", lambda x: fake.cpf())
        return (fake_cpf if "." in val_str else _clean_val(fake_cpf)), "DOCS"
    
    if REGEX_RULES["EMAIL"].fullmatch(val_str):
        return _get_consistent(val_str, "EMAIL", lambda x: fake.safe_email()), "CONTACTS"

    # 5. HEURÍSTICA DE CÓDIGOS (CAPTURA RGs CURTOS E CHASSIS SOLTOS)
    if is_likely_an_id(val_str):
        return _get_consistent(val_str, "ID_DYNAM", _gen_structure_preserved_id), "DOCS"

    # 6. BLOCOS DE TEXTO (Narrativas)
    txt_anon = anonymize_text_block(val_str)
    category = "PER" if txt_anon != val_str else None
    return txt_anon, category

def anonymize_text_block(text):
    if not text or len(text) < 3: return text

    # Passo A: IA para nomes
    if nlp and any(c.isupper() for c in text):
        doc = nlp(text.title())
        for ent in reversed(doc.ents):
            if ent.label_ == "PER" and not any(c.isdigit() for c in ent.text):
                orig_name = text[ent.start_char:ent.end_char]
                fake_n = _get_consistent(orig_name, "PER", lambda x: f"{fake.first_name()} {fake.last_name()}".upper())
                text = text[:ent.start_char] + fake_n + text[ent.end_char:]

    # Passo B: Scan de palavras (Pega o '3344556' no meio de um texto se houver)
    words = text.split()
    new_words = []
    for word in words:
        clean_word = word.strip(".,()[]{}")
        if is_likely_an_id(clean_word) and not REGEX_RULES["CPF"].fullmatch(clean_word):
            fake_id = _get_consistent(clean_word, "ID_DYNAM", _gen_structure_preserved_id)
            new_words.append(word.replace(clean_word, fake_id))
        else:
            new_words.append(word)
    
    return " ".join(new_words)