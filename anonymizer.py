import re
import spacy
import random
import string
from faker import Faker

# Inicializa o gerador de dados fakes
fake = Faker("pt_BR")

# ==================================================
# 1. DETECÇÃO DE PADRÕES (REGEX) - DEFINIÇÃO GLOBAL
# ==================================================
# Isso deve vir ANTES das funções para evitar o erro 'NameError'
REGEX = {
    "CPF": re.compile(r"\d{3}\.?\d{3}\.?\d{3}-?\d{2}"),
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b"),
    # Captura telefones (41) 99999-9999, 41999999999, etc.
    "PHONE": re.compile(r"(\(?\d{2}\)?\s?\d{4,5}-?\d{4})"),
    # Padrão para códigos alfanuméricos (Chassi, Placas, Protocolos)
    "CODE": re.compile(r"\b(?=.*[A-Z])(?=.*\d)[A-Z0-9\.\-/]{5,25}\b")
}

# Memória para manter a consistência entre tabelas
_memory = { "PER": {}, "CPF": {}, "EMAIL": {}, "PHONE": {}, "ID": {} }

# Carregamento do modelo de IA
try:
    nlp = spacy.load("pt_core_news_sm", disable=["parser", "lemmatizer", "textcat"])
except:
    nlp = None

# ==================================================
# 2. FUNÇÕES DE APOIO
# ==================================================

def _get_consistent(val, category, fn_create):
    """Garante que o mesmo dado original gere sempre o mesmo dado fake."""
    v = str(val).strip().upper() 
    if not v or v.lower() == 'none': return v
    if v not in _memory[category]:
        _memory[category][v] = fn_create(v)
    return _memory[category][v]

def mask_pattern(orig):
    """Clona o formato original: letras viram letras, números viram números."""
    res = []
    for char in orig:
        if char.isdigit(): res.append(random.choice(string.digits))
        elif char.isalpha(): res.append(random.choice(string.ascii_uppercase))
        else: res.append(char)
    return "".join(res)

# ==================================================
# 3. O "LIQUIDIFICADOR" DE TEXTOS LONGOS
# ==================================================
def anonymize_text_block(text):
    if not text or len(text) < 3: return text

    # TÉCNICA A: Referência Cruzada (Busca nomes já conhecidos na memória)
    sorted_names = sorted(_memory["PER"].keys(), key=len, reverse=True)
    for orig_name in sorted_names:
        if len(orig_name) > 3:
            pattern = re.compile(re.escape(orig_name), re.IGNORECASE)
            text = pattern.sub(_memory["PER"][orig_name], text)

    # TÉCNICA B: Regex Dinâmico (Procura documentos e contatos no meio do texto)
    text = REGEX["CPF"].sub(lambda m: _get_consistent(m.group(), "CPF", lambda x: fake.cpf()), text)
    text = REGEX["EMAIL"].sub(lambda m: _get_consistent(m.group(), "EMAIL", lambda x: fake.email()), text)
    text = REGEX["PHONE"].sub(lambda m: _get_consistent(m.group(), "PHONE", lambda x: fake.cellphone_number()), text)

    # TÉCNICA C: Inteligência Artificial (spaCy)
    # Se o texto for todo minúsculo, capitalizamos para ajudar a IA a achar nomes
    text_for_ai = text.title() if text.islower() else text
    if nlp:
        doc = nlp(text_for_ai)
        for ent in reversed(doc.ents):
            if ent.label_ == "PER":
                fake_name = _get_consistent(ent.text, "PER", lambda x: fake.name().upper())
                text = text[:ent.start_char] + fake_name + text[ent.end_char:]

    # TÉCNICA D: Limpeza de Códigos perdidos
    words = text.split()
    new_words = []
    for word in words:
        if len(word) > 5 and any(c.isdigit() for c in word) and any(c.isalpha() for c in word):
            new_words.append(mask_pattern(word))
        else:
            new_words.append(word)
    
    return " ".join(new_words)

# ==================================================
# 4. FUNÇÃO PRINCIPAL (CHAMADA PELO APP)
# ==================================================
def anonymize_value(col_name, value, is_numeric=False):
    if value is None or str(value).strip() == "": return value, None

    val_orig = str(value).strip()
    col_lower = col_name.lower()

    # 1. Prioridade por Nome de Coluna (Onde o 'lucas' minúsculo é pego)
    if any(k in col_lower for k in ["nome", "proprietario", "autor", "vitima", "usuario"]):
        return _get_consistent(val_orig, "PER", lambda x: fake.name().upper()), "PER"

    # 2. Textos Longos (Relatos/Interrogatórios)
    if len(val_orig) > 30 or " " in val_orig:
        return anonymize_text_block(val_orig), "TEXT"

    # 3. Códigos e IDs (Chassis/Placas)
    if 4 < len(val_orig) < 30 and any(c.isdigit() for c in val_orig) and any(c.isalpha() for c in val_orig):
        return _get_consistent(val_orig, "ID", lambda x: mask_pattern(x)), "DOCS"

    # 4. Fallback: CPF ou Telefone em colunas genéricas
    val_clean = re.sub(r"\D", "", val_orig)
    if len(val_clean) == 11:
        return _get_consistent(val_orig, "CPF", lambda x: fake.cpf()), "DOCS"

    return val_orig, None