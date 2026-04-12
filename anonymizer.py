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
<<<<<<< HEAD
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
=======
    # Manter o tagger ativado (removido do disable) para ele saber a diferença entre Substantivo e Nome Próprio
    nlp = spacy.load("pt_core_news_lg", disable=["parser", "lemmatizer", "textcat"])
except Exception as e:
    nlp = None
    print(f"AVISO: Modelo spaCy não encontrado. A detecção de nomes será limitada. Erro: {e}")

# ========================
# MAPAS DE MEMÓRIA (Cache de Consistência)
# ========================
_name_map = {}
_first_name_map = {}
_cpf_map = {}
_universal_fake_map = {}
_used_fakes = set() 

# ========================
# REGEX DE SEGURANÇA
# ========================
CPF_REGEX = re.compile(r"\b\d{3}[\.\-]?\d{3}[\.\-]?\d{3}[\.\-]?\d{2}\b")
ID_UNIVERSAL_REGEX = re.compile(r'\b(?=.*[0-9])[a-zA-Z0-9\.\-/]{5,}\b')
PATENTES_PREFIXO = r"sd|cb|sgt|subten|ten|maj|cel|cap|asp|agent|pf|prf|inspetor|sr|dra?|prof"
UUID_REGEX = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
HASH_REGEX = re.compile(r'\b[0-9a-fA-F]{32}\b|\b[0-9a-fA-F]{40}\b|\b[0-9a-fA-F]{64}\b')

# ========================
# GERAÇÃO FAKE & ANTI-COLISÃO
# ========================
def get_dynamic_id(original_val: str) -> str:
    val_str = str(original_val).strip()
    if len(val_str) < 3: return val_str
    
    key = f"ID_{val_str.upper()}"
    if key in _universal_fake_map:
        return _universal_fake_map[key]
    
    while True:
        fake_val = ""
        for char in val_str:
            if char.isalpha():
                if char.isupper():
                    fake_val += random.choice(string.ascii_uppercase)
                else:
                    fake_val += random.choice(string.ascii_lowercase)
            elif char.isdigit():
                fake_val += random.choice(string.digits)
            else:
                fake_val += char
                
        if fake_val not in _used_fakes:
            _used_fakes.add(fake_val)
            break
            
    _universal_fake_map[key] = fake_val
    return fake_val

def _fake_name(original: str) -> str:
    if not original or len(original) < 3: return original
    clean_orig = " ".join(original.strip().split())
    key = f"NAME_{clean_orig.lower()}"
>>>>>>> c310b3398f9ed58198c2d70376e9bf875425f26a
    
    return False

<<<<<<< HEAD
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
=======
    parts = clean_orig.split()
    first_name_orig = parts[0].lower()

    if first_name_orig in _first_name_map:
        new_first = _first_name_map[first_name_orig]
    else:
        new_first = fake.first_name()
        _first_name_map[first_name_orig] = new_first

    new_full = f"{new_first} {fake.last_name()}" if len(parts) > 1 else new_first
    _name_map[key] = new_full
    return new_full

def _fake_cpf(original: str) -> str:
    digits = re.sub(r"\D", "", original)
    if digits not in _cpf_map:
        _cpf_map[digits] = re.sub(r"\D", "", fake.cpf())
    return _cpf_map[digits]

# ========================
# RADARES DE CONTEÚDO
# ========================
def is_standalone_code(val_str: str) -> bool:
    if val_str.count(' ') > 1: return False 
    length = len(val_str)
    has_digit = any(c.isdigit() for c in val_str)
    has_alpha = any(c.isalpha() for c in val_str)
    if length >= 16 and not val_str.isspace(): return True
    if length >= 5 and has_digit and has_alpha: return True
    if length >= 8 and has_digit and not has_alpha: return True
    return False

def is_false_positive_person(ent_text: str) -> bool:
    """
    Converte para minúsculo para forçar a IA a usar o dicionário gramatical.
    Ex: 'Fuzil' -> 'fuzil' (Substantivo Comum / NOUN).
    Ex: 'Livia' -> 'livia' (Nome Próprio / PROPN).
    """
    if not nlp: return False
    doc_lower = nlp(ent_text.lower())
    
    # Se não houver NENHUM Nome Próprio na palavra em minúsculo, é porque era um objeto.
    has_propn = any(token.pos_ == "PROPN" for token in doc_lower)
    return not has_propn

def anonymize_text_value(value):
    if not value or not isinstance(value, str): return value

    value = HASH_REGEX.sub(lambda m: get_dynamic_id(m.group()), value)
    value = UUID_REGEX.sub(lambda m: get_dynamic_id(m.group()), value)
    value = ID_UNIVERSAL_REGEX.sub(lambda m: get_dynamic_id(m.group()), value)

    if nlp and any(c.isupper() for c in value):
        # Transforma "FUZIL" em "Fuzil" para a IA analisar
        work_text = value.title() if value.isupper() else value
        doc = nlp(work_text)
        
        # Processa de trás pra frente
        for ent in reversed(doc.ents):
            if ent.label_ == "PER" and not any(c.isdigit() for c in ent.text):
                
                # NOVO: O filtro passa a prova real na palavra
                if is_false_positive_person(ent.text):
                    continue # Descobriu que era um objeto disfarçado de pessoa. Pula!
                    
                value = value[:ent.start_char] + _fake_name(ent.text) + value[ent.end_char:]

    business_prefixes = ["ENVOLVIDO - ", "AUTOR - ", "VITIMA - ", "NOME: ", "NOME - "]
    for pref in business_prefixes:
        if pref.upper() in value.upper():
            regex_pref = rf"(?i)({re.escape(pref)})([\w\sÀ-Üà-ü]+?)(?=\s{{2,}}|$|\n)"
            def replace_pref(match):
                p_encontrado = match.group(1)
                nome_potencial = match.group(2).strip()
                if len(nome_potencial) > 3 and not any(x in nome_potencial.upper() for x in ["FURTO", "SIMPLES", "VEICULO"]):
                    return f"{p_encontrado}{_fake_name(nome_potencial)}"
                return match.group(0)
            value = re.compile(regex_pref).sub(replace_pref, value)

    value = CPF_REGEX.sub(lambda m: _fake_cpf(m.group()), value)
    pattern_patente = rf'\b({PATENTES_PREFIXO})\.?\s+([A-ZÀ-Ü][a-zà-ü]+(?:\s+[A-ZÀ-Ü][a-zà-ü]+)*)\b'
    value = re.sub(pattern_patente, lambda m: f"{m.group(1)} {_fake_name(m.group(2))}" if len(m.group(2)) > 3 else m.group(0), value, flags=re.IGNORECASE)

    return value

def anonymize_value(column_name: str, value):
    if value is None: return None
    val_str = str(value).strip()
    
    if "cpf" in column_name.lower() or CPF_REGEX.fullmatch(val_str):
        return _fake_cpf(val_str)
        
    if is_standalone_code(val_str):
        return get_dynamic_id(val_str)
        
    return anonymize_text_value(val_str)
>>>>>>> c310b3398f9ed58198c2d70376e9bf875425f26a
