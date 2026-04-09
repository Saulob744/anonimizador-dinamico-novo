import re
import spacy
import random
import string
from datetime import datetime, date
from faker import Faker

fake = Faker("pt_BR")

# ========================
# CARREGAMENTO DA IA
# ========================
try:
    nlp = spacy.load("pt_core_news_lg")
except Exception as e:
    nlp = None
    print(f"ERRO: Modelo spaCy não encontrado.")

# ========================
# MAPAS DE MEMÓRIA (Persistência Global)
# ========================
_name_map = {}
_first_name_map = {}
_cpf_map = {}
_universal_fake_map = {} # Cache para Chassis, Placas e IDs

# ========================
# REGEX DE SEGURANÇA
# ========================
CPF_REGEX = re.compile(r"\b\d{3}[\.\-]?\d{3}[\.\-]?\d{3}[\.\-]?\d{2}\b")
# Detecta Chassis/Placas: Sequências de 5+ chars que misturam letras e números
ALFANUMERICO_REGEX = re.compile(r'\b(?=[A-Z]*\d)(?=[\d]*[A-Z])[A-Z\d]{5,}\b', re.IGNORECASE)
PATENTES_PREFIXO = r"sd|cb|sgt|subten|ten|maj|cel|cap|asp|agent|pf|prf|inspetor|sr|dra?|prof"

# ========================
# CÉREBRO DE GERAÇÃO FAKE
# ========================

def get_dynamic_fake(original_val: str) -> str:
    """
    Anonimiza IDs, Chassis e Placas mantendo o formato original.
    Ex: 9C2-KF3 -> 4R1-HG8
    """
    key = str(original_val).strip().upper()
    if key in _universal_fake_map:
        return _universal_fake_map[key]
    
    fake_val = ""
    for char in str(original_val):
        if char.isalpha():
            fake_val += random.choice(string.ascii_uppercase)
        elif char.isdigit():
            fake_val += random.choice(string.digits)
        else:
            fake_val += char
            
    _universal_fake_map[key] = fake_val
    return fake_val

def _fake_name(original: str) -> str:
    if not original or len(original) < 2: return original
    clean_orig = " ".join(original.strip().split())
    key = clean_orig.lower()
    
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
# MOTOR DE ANÁLISE GRAMATICAL
# ========================

def is_actually_a_person(ent_text, ent_doc_part):
    # Se contém números, não é uma pessoa (provavelmente chassi ou placa)
    if any(char.isdigit() for char in ent_text): return False
    
    has_proper_noun = any(token.pos_ == "PROPN" for token in ent_doc_part)
    is_common_object = all(token.pos_ == "NOUN" and not token.text[0].isupper() for token in ent_doc_part)
    
    if is_common_object and not has_proper_noun: return False
    if len(ent_text) < 3 and not ent_text.isupper(): return False
        
    return True

# ========================
# LÓGICA DE PROCESSAMENTO DE TEXTO
# ========================

def anonymize_text_value(value):
    if not value or not isinstance(value, str): return value

    # 1. NOVO: Padrões Alfanuméricos (Chassis, Placas, IDs)
    # Detecta e troca mantendo o formato sem precisar de listas
    value = ALFANUMERICO_REGEX.sub(lambda m: get_dynamic_fake(m.group()), value)

    # 2. Inteligência de IA para Nomes
    work_text = value.title() if value.isupper() else value
    if nlp:
        doc = nlp(work_text)
        ents = sorted(doc.ents, key=lambda x: x.start_char, reverse=True)
        for ent in ents:
            if ent.label_ == "PER":
                orig_segment = value[ent.start_char:ent.end_char]
                if is_actually_a_person(orig_segment, ent):
                    value = value[:ent.start_char] + _fake_name(orig_segment) + value[ent.end_char:]

    # 3. NOVO: Fallback para Prefixos de Negócio (ENVOLVIDO - , NOME: )
    # Trata nomes que a IA ignora por estarem colados em hifens
    business_prefixes = ["ENVOLVIDO - ", "AUTOR - ", "VITIMA - ", "NOME: ", "NOME - "]
    for pref in business_prefixes:
        if pref.upper() in value.upper():
            # Regex para pegar o nome após o prefixo até encontrar um separador de campo
            regex_pref = rf"(?i)({re.escape(pref)})([\w\sÀ-Üà-ü]+?)(?=\s{{2,}}|$|\n)"
            def replace_pref(match):
                p_encontrado = match.group(1)
                nome_potencial = match.group(2).strip()
                # Valida se não é uma palavra de sistema (ex: FURTO)
                if len(nome_potencial) > 3 and not any(x in nome_potencial.upper() for x in ["FURTO", "SIMPLES", "VEICULO"]):
                    return f"{p_encontrado}{_fake_name(nome_potencial)}"
                return match.group(0)
            value = re.compile(regex_pref).sub(replace_pref, value)

    # 4. CPFs e Patentes
    value = CPF_REGEX.sub(lambda m: _fake_cpf(m.group()), value)
    pattern_patente = rf'\b({PATENTES_PREFIXO})\.?\s+([A-ZÀ-Ü][a-zà-ü]+(?:\s+[A-ZÀ-Ü][a-zà-ü]+)*)\b'
    value = re.sub(pattern_patente, lambda m: f"{m.group(1)} {_fake_name(m.group(2))}" if len(m.group(2)) > 3 else m.group(0), value, flags=re.IGNORECASE)

    return value

def anonymize_value(column_name: str, value):
    if value is None: return None
    val_str = str(value).strip()
    if "cpf" in column_name.lower() or CPF_REGEX.fullmatch(val_str):
        return _fake_cpf(val_str)
    return anonymize_text_value(val_str)