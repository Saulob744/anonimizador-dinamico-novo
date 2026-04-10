import re
import spacy
import random
import string
from faker import Faker

fake = Faker("pt_BR")

# ========================
# CARREGAMENTO DA IA
# ========================
try:
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
    
    if key in _name_map: return _name_map[key]

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