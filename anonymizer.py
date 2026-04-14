import re
import spacy
import random
import string
from faker import Faker

fake = Faker("pt_BR")

try:
    # Carregando o modelo grande para maior precisão em nomes brasileiros
    nlp = spacy.load("pt_core_news_lg", disable=["parser", "lemmatizer", "textcat"])
except:
    nlp = None

DEBUG = True

def log(msg):
    if DEBUG: print(msg)

# ==================================================
# MEMÓRIA GLOBAL (Garante Consistência)
# ==================================================
_memory = {
    "PER": {},   # Pessoas
    "CPF": {},   # Documentos
    "EMAIL": {}, # Contatos
    "PHONE": {}, # Telefones
    "ID": {}     # Protocolos/RGs/Códigos
}

# ==================================================
# DETECÇÃO DE PADRÕES (REGEX)
# ==================================================
REGEX = {
    "CPF": re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b"),
    # Captura RGs e Protocolos genéricos (Ex: SESP-123.456)
    "CODE": re.compile(r"\b(?=.*[A-Z])(?=.*\d)[A-Z0-9\.\-/]{5,20}\b")
}

# ==================================================
# MÁQUINAS DE GERAÇÃO (PRESERVA FORMATO)
# ==================================================
def _get_consistent(val, category, fn_create):
    """Garante que o mesmo dado original sempre gere o mesmo dado fake."""
    v = str(val).strip()
    if not v or v.lower() == 'none': return v
    
    if v not in _memory[category]:
        _memory[category][v] = fn_create(v)
    return _memory[category][v]

def mask_pattern(orig):
    """Muda letras por letras e números por números, mantendo pontuação."""
    res = []
    for char in orig:
        if char.isdigit(): res.append(random.choice(string.digits))
        elif char.isalpha(): res.append(random.choice(string.ascii_uppercase))
        else: res.append(char)
    return "".join(res)

# ==================================================
# INTELIGÊNCIA NLP
# ==================================================
def anonymize_text_block(text):
    """Analisa textos longos, preservando cidades e mudando apenas nomes."""
    if not text or len(text) < 3: return text

    # 1. Regex primeiro (mais rápido para dados estruturados dentro de texto)
    text = REGEX["CPF"].sub(lambda m: _get_consistent(m.group(), "CPF", lambda x: fake.cpf()), text)
    text = REGEX["EMAIL"].sub(lambda m: _get_consistent(m.group(), "EMAIL", lambda x: fake.email()), text)

    # 2. spaCy para nomes de pessoas
    if nlp:
        doc = nlp(text)
        # Processamos de trás para frente para não perder o índice da string ao alterar o tamanho
        for ent in reversed(doc.ents):
            # SÓ anonimiza se for Pessoa (PER)
            # Ignora GPE (Cidades), ORG (Empresas), LOC (Lugares)
            if ent.label_ == "PER":
                fake_name = _get_consistent(ent.text, "PER", lambda x: fake.name().upper())
                text = text[:ent.start_char] + fake_name + text[ent.end_char:]
            
    return text

# ==================================================
# FUNÇÃO PRINCIPAL (CHAMADA PELO APP)
# ==================================================
def anonymize_value(col_name, value, is_numeric=False):
    if value is None or str(value).strip() == "":
        return value, None

    val = str(value).strip()
    col_lower = col_name.lower()

    # --- 1. FILTROS DE EXCEÇÃO (Não anonimizar) ---
    # Se for cidade, estado ou data, retornamos o original
    if any(k in col_lower for k in ["cidade", "municipio", "uf", "estado", "data", "nascimento"]):
        return value, None

    # --- 2. DOCUMENTOS E CONTATOS (Fixo) ---
    if REGEX["CPF"].fullmatch(val):
        return _get_consistent(val, "CPF", lambda x: fake.cpf()), "DOCS"
    
    if "@" in val and REGEX["EMAIL"].fullmatch(val):
        return _get_consistent(val, "EMAIL", lambda x: fake.email()), "CONTACTS"

    # --- 3. NOMES PRÓPRIOS (Detecção por coluna ou NLP) ---
    if any(k in col_lower for k in ["nome", "usuario", "proprietario", "vitima", "autor"]):
        # Se for um nome curto (Cidade/Sigla), o NLP GPE vai salvar
        if nlp:
            doc = nlp(val)
            # Se a IA disser que é lugar, não mexemos
            if any(ent.label_ == "GPE" for ent in doc.ents):
                return value, None
        return _get_consistent(val, "PER", lambda x: fake.name().upper()), "PER"

    # --- 4. CÓDIGOS, RGs E PROTOCOLOS (Preserva Formato) ---
    if REGEX["CODE"].fullmatch(val) or (len(val) > 5 and any(c.isdigit() for c in val) and any(c.isalpha() for c in val)):
        return _get_consistent(val, "ID", lambda x: mask_pattern(x)), "DOCS"

    # --- 5. TEXTO LIVRE (Descrições/Observações) ---
    if len(val) > 20 or " " in val:
        return anonymize_text_block(val), "TEXT"

    # --- 6. DEFAULT (Se não temos certeza, mantemos o original para não ser agressivo) ---
    return value, None