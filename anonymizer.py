import re
import spacy
import random
import string
import urllib.parse
from faker import Faker

fake = Faker("pt_BR")

try:
    # Recomendado usar o 'lg' se o ambiente suportar, pela precisão com nomes brasileiros
    nlp = spacy.load("pt_core_news_sm", disable=["parser", "lemmatizer", "textcat"])
except:
    nlp = None

# ==================================================
# DETECÇÃO DE PADRÕES (REGEX)
# ==================================================
REGEX = {
    "CPF": re.compile(r"\d{3}\.?\d{3}\.?\d{3}-?\d{2}"),
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b"),
    # Captura telefones (41) 99999-9999, 41999999999, etc.
    "PHONE": re.compile(r"(\(?\d{2}\)?\s?\d{4,5}-?\d{4})"),
    # Padrão para códigos alfanuméricos (Chassi, Placas, Protocolos SESP)
    "CODE": re.compile(r"\b(?=.*[A-Z])(?=.*\d)[A-Z0-9\.\-/]{5,25}\b")
}

_memory = { "PER": {}, "CPF": {}, "EMAIL": {}, "PHONE": {}, "ID": {} }

def _get_consistent(val, category, fn_create):
    v = str(val).strip().upper() 
    if not v or v.lower() == 'none': return v
    if v not in _memory[category]:
        _memory[category][v] = fn_create(v)
    return _memory[category][v]

def mask_pattern(orig):
    """Gera um 'clone' do formato original: letra=letra, num=num."""
    res = []
    for char in orig:
        if char.isdigit(): res.append(random.choice(string.digits))
        elif char.isalpha(): res.append(random.choice(string.ascii_uppercase))
        else: res.append(char)
    return "".join(res)

def anonymize_text_block(text):
    if not text or len(text) < 3: return text

    # 1. Aplica Regex de forma dinâmica dentro do texto
    text = REGEX["CPF"].sub(lambda m: _get_consistent(m.group(), "CPF", lambda x: fake.cpf()), text)
    text = REGEX["EMAIL"].sub(lambda m: _get_consistent(m.group(), "EMAIL", lambda x: fake.email()), text)
    text = REGEX["PHONE"].sub(lambda m: _get_consistent(m.group(), "PHONE", lambda x: fake.cellphone_number()), text)

    # 2. NLP para Nomes Próprios
    if nlp:
        doc = nlp(text)
        for ent in reversed(doc.ents):
            if ent.label_ == "PER":
                fake_name = _get_consistent(ent.text, "PER", lambda x: fake.name().upper())
                text = text[:ent.start_char] + fake_name + text[ent.end_char:]
    return text

def anonymize_value(col_name, value, is_numeric=False):
    if value is None or str(value).strip() == "": return value, None

    val_orig = str(value).strip()
    val_clean = re.sub(r"\D", "", val_orig) # Apenas números para detecção
    col_lower = col_name.lower()

    # --- 1. BLOCOS DE TEXTO (Narrativas/Observações) ---
    if len(val_orig) > 45 or (" " in val_orig and len(val_orig) > 20):
        return anonymize_text_block(val_orig), "TEXT"

    # --- 2. DETECÇÃO DINÂMICA POR FORMATO (CPF ou Celular) ---
    # Se tem 11 dígitos e a coluna sugere contato OU se parece muito com um celular
    if len(val_clean) in [10, 11]:
        if any(k in col_lower for k in ["tel", "fone", "cel", "contato", "whatsapp"]):
             return _get_consistent(val_orig, "PHONE", lambda x: fake.cellphone_number()), "CONTACTS"
        
        # Se não caiu em telefone mas tem 11 dígitos, verifica CPF
        if len(val_clean) == 11:
            return _get_consistent(val_orig, "CPF", lambda x: fake.cpf()), "DOCS"

    # --- 3. NOMES PRÓPRIOS (Baseado em coluna) ---
    if any(k in col_lower for k in ["nome", "proprietario", "autor", "vitima", "usuario"]):
        return _get_consistent(val_orig, "PER", lambda x: fake.name().upper()), "PER"

    # --- 4. CÓDIGOS GERAIS (Chassi, Placa, RG, Protocolo) ---
    # Se contém números e letras OU se a coluna sugere um ID/Documento
    if len(val_orig) > 4 and (any(c.isdigit() for c in val_orig) and any(c.isalpha() for c in val_orig)):
        return _get_consistent(val_orig, "ID", lambda x: mask_pattern(x)), "DOCS"
    
    # Caso especial: RG ou Protocolo puramente numérico (mas curto)
    if 5 <= len(val_clean) <= 9 and any(k in col_lower for k in ["rg", "protocolo", "id", "numero"]):
        return _get_consistent(val_orig, "ID", lambda x: mask_pattern(x)), "DOCS"

    return val_orig, None