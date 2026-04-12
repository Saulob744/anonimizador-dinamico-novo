import re
import spacy
import random
import string
from faker import Faker

# Inicializa o Faker para o contexto brasileiro
fake = Faker("pt_BR")

# ========================
# CARREGAMENTO DA IA
# ========================
try:
    nlp = spacy.load("pt_core_news_lg", disable=["parser", "lemmatizer", "textcat"])
except Exception as e:
    nlp = None
    print(f"AVISO: spaCy 'pt_core_news_lg' não encontrado.")

# ========================
# MAPAS DE MEMÓRIA (Consistência Global)
# ========================
_memory = {
    "PER": {}, "CPF": {}, "EMAIL": {}, "CEP": {}, "PHONE": {}, "ID_DYNAM": {}
}
_used_fakes = set()

# Regex para dados conhecidos (Melhoradas)
REGEX_RULES = {
    "CPF": re.compile(r"\b\d{3}[\.\-]?\d{3}[\.\-]?\d{3}[\.\-]?\d{2}\b"),
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b"),
    "CEP": re.compile(r"\b\d{5}[\-]?\d{3}\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})[-\s]?\d{4}\b"),
    # Regex Genérica para Códigos (Placas, Chassis, Tokens, Protocolos)
    "GENERIC_CODE": re.compile(r"\b(?=[A-Za-z]*\d)(?=\d*[A-Za-z])[A-Za-z0-9\-\.]{5,20}\b")
}

# ========================
# NÚCLEO DE INTELIGÊNCIA
# ========================

def _get_consistent(val, category, func):
    v = str(val).strip()
    if v not in _memory[category]:
        _memory[category][v] = func(v)
    return _memory[category][v]

def _gen_fake_id(orig):
    """Gera um ID falso mantendo rigorosamente o formato original."""
    while True:
        fake_val = "".join(
            random.choice(string.ascii_uppercase) if c.isupper() else
            random.choice(string.ascii_lowercase) if c.islower() else
            random.choice(string.digits) if c.isdigit() else c
            for c in orig
        )
        if fake_val not in _used_fakes:
            _used_fakes.add(fake_val)
            return fake_val

def is_dynamic_sensitive(val_str):
    """
    Detecta dinamicamente se a string é um código sensível (Placa, Chassi, Token).
    Usa análise de mistura de caracteres (Entropia).
    """
    s = val_str.strip()
    if not s or " " in s or len(s) < 5: return False
    
    # 1. Se parece com um documento conhecido via Regex
    for rule in ["CPF", "EMAIL", "CEP", "PHONE"]:
        if REGEX_RULES[rule].fullmatch(s): return True

    # 2. Análise de Código Alfanumérico (Mistura de Letras e Números)
    has_digit = any(c.isdigit() for c in s)
    has_alpha = any(c.isalpha() for c in s)
    has_special = any(c in "-." for c in s)

    # Se tem letras e números misturados, é um forte candidato a código/ID
    if (has_digit and has_alpha) or (len(s) >= 12 and has_digit):
        # Validação com IA: se for uma palavra dicionarizada, ignoramos
        if nlp and len(s) < 15: # Palavras longas raramente são comuns
            doc = nlp(s.lower())
            if any(t.pos_ in ["NOUN", "VERB", "ADJ"] and not t.is_stop for t in doc):
                return False
        return True
    
    return False

def anonymize_value(col_name, value):
    if value is None: return None
    val_str = str(value).strip()
    col_lower = str(col_name).lower()

    # 1. Checagem por Nome de Coluna ou Formato exato (CPF, Email, Fone)
    if "cpf" in col_lower or REGEX_RULES["CPF"].fullmatch(val_str):
        fake_cpf = _get_consistent(val_str, "CPF", lambda x: fake.cpf())
        if "." not in val_str or len(val_str) <= 11:
            return fake_cpf.replace(".", "").replace("-", "")
        return fake_cpf

    if "email" in col_lower or REGEX_RULES["EMAIL"].fullmatch(val_str):
        return _get_consistent(val_str, "EMAIL", lambda x: fake.safe_email())
    
    if "fone" in col_lower or "tel" in col_lower or REGEX_RULES["PHONE"].fullmatch(val_str):
        return _get_consistent(val_str, "PHONE", lambda x: fake.cellphone_number())

    # 2. Detecção Dinâmica (Placas, Chassis, Tokens em colunas avulsas)
    if is_dynamic_sensitive(val_str):
        return _get_consistent(val_str, "ID_DYNAM", _gen_fake_id)

    # 3. Se não for um valor "puro", trata como bloco de texto (NLP)
    return anonymize_text_block(val_str)

def anonymize_text_block(text):
    """
    Escaneia textos e limpa nomes, documentos e CÓDIGOS dinâmicos.
    """
    if not text: return text

    # A. Limpeza de Documentos Conhecidos (Regex)
    text = REGEX_RULES["EMAIL"].sub(lambda m: _get_consistent(m.group(), "EMAIL", lambda x: fake.safe_email()), text)
    text = REGEX_RULES["CPF"].sub(lambda m: _get_consistent(m.group(), "CPF", lambda x: fake.cpf()), text)
    text = REGEX_RULES["PHONE"].sub(lambda m: _get_consistent(m.group(), "PHONE", lambda x: fake.cellphone_number()), text)

    # B. Identificação Dinâmica de CÓDIGOS no meio do texto (Placas, Chassis, Tokens)
    # Procuramos por palavras que pareçam códigos e não nomes
    words = text.split()
    for word in words:
        clean_word = word.strip(".,()[]{}")
        if is_dynamic_sensitive(clean_word):
            fake_code = _get_consistent(clean_word, "ID_DYNAM", _gen_fake_id)
            text = text.replace(clean_word, fake_code)

    # C. IA do spaCy para Nomes de Pessoas (Entidades PER)
    if nlp and any(c.isupper() for c in text):
        doc = nlp(text)
        # Processamos de trás para frente para não errar os índices ao substituir
        for ent in reversed(doc.ents):
            if ent.label_ == "PER":
                # Filtro extra: nomes raramente têm números
                if any(c.isdigit() for c in ent.text): continue
                
                fake_n = _get_consistent(ent.text, "PER", lambda x: f"{fake.first_name()} {fake.last_name()}")
                text = text[:ent.start_char] + fake_n + text[ent.end_char:]
                
    return text
