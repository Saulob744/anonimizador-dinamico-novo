import re
import spacy
from faker import Faker

fake = Faker("pt_BR")

# ========================
# CARREGAMENTO DA IA
# ========================
try:
    # Usamos o modelo Large para melhor precisão em nomes brasileiros
    nlp = spacy.load("pt_core_news_lg")
except Exception as e:
    nlp = None
    print(f"AVISO: IA (spaCy) não carregada. Usando apenas heurísticas. Erro: {e}")

# ========================
# MAPAS DE CONSISTÊNCIA
# ========================
_name_map = {}
_cpf_map = {}
_email_map = {}
_phone_map = {}
_generic_map = {}
_person_name_cache = {}

# ========================
# CONFIGURAÇÕES
# ========================
SENSITIVE_KEYWORDS = [
    "nome", "name", "cpf", "email", "telefone", 
    "phone", "celular", "rg", "documento"
]

# Blacklist para evitar que crimes ou termos técnicos virem nomes de pessoas
BLACKLIST_PALAVRAS = {
    "roubo", "furto", "carro", "veículo", "rua", "avenida", "bairro", "cidade", 
    "estado", "polícia", "delegacia", "boletim", "ocorrência", "crime", "vítima", 
    "autor", "invasao", "domicilio", "injuria", "ameaca", "estelionato", 
    "homicidio", "lesao", "militar", "civil", "guarnicao", "viatura"
}

# ========================
# PREFIXOS (MILITARES + CIVIS)
# ========================
PATENTES = r"sd|cb|sgt|subten|ten|maj|cel|cap|asp|agent|pf|prf|senhor|senhora|sr|sra|dr|dra|prof|profa"
PREFIX_REGEX = re.compile(rf'^((?:{PATENTES})\.?\s+)', re.IGNORECASE)
SUFFIX_REGEX = re.compile(r'([.,;!?]+)$')

# ========================
# FUNÇÕES DE APOIO
# ========================
def separar_casca(texto: str):
    prefixo, sufixo, miolo = "", "", texto
    match_prefix = PREFIX_REGEX.match(miolo)
    if match_prefix:
        prefixo = match_prefix.group(1)
        miolo = miolo[len(prefixo):]
    match_suffix = SUFFIX_REGEX.search(miolo)
    if match_suffix:
        sufixo = match_suffix.group(1)
        miolo = miolo[:-len(sufixo)]
    return prefixo, miolo, sufixo

def is_sensitive_column(column_name: str) -> bool:
    """Função exigida pelo main.py para classificar colunas"""
    col = column_name.lower()
    return any(k in col for k in SENSITIVE_KEYWORDS)

def is_text_column_type(data_type: str) -> bool:
    """Função exigida pelo main.py para identificar colunas de texto longo"""
    return any(t in data_type.lower() for t in ["char", "text", "varchar"])

# ========================
# DETECÇÃO INTELIGENTE
# ========================
def is_person_name(value: str) -> bool:
    if not value or value in _person_name_cache:
        return _person_name_cache.get(value, False)

    if not isinstance(value, str) or len(value) < 2 or re.search(r"\d", value):
        return False

    # Validação via spaCy (IA)
    if nlp:
        doc = nlp(value)
        # Se a IA detectar como pessoa e não estiver na blacklist
        result = any(ent.label_ == "PER" for ent in doc.ents) and value.lower() not in BLACKLIST_PALAVRAS
    else:
        # Fallback caso a IA falhe: padrão de Nome Próprio (Iniciais Maiúsculas)
        result = bool(re.match(r"^[A-ZÀ-Ü][a-zà-ü]+(?:\s+[A-ZÀ-Ü][a-zà-ü]+)*$", value))
    
    _person_name_cache[value] = result
    return result

# ========================
# GERADORES FAKE (CONSISTENTES)
# ========================
def _fake_name(original: str) -> str:
    key = original.strip().lower()
    if key not in _name_map:
        _name_map[key] = fake.name()
    return _name_map[key]

def _fake_cpf(original: str) -> str:
    key = re.sub(r"\D", "", original)
    if key not in _cpf_map:
        _cpf_map[key] = re.sub(r"\D", "", fake.cpf())
    return _cpf_map[key]

def _fake_email(original: str) -> str:
    key = original.lower().strip()
    if key not in _email_map:
        _email_map[key] = fake.email()
    return _email_map[key]

def _fake_phone(original: str) -> str:
    key = re.sub(r"\D", "", original)
    if key not in _phone_map:
        _phone_map[key] = fake.phone_number()
    return _phone_map[key]

# ========================
# INTERFACE COM O MAIN
# ========================
def anonymize_value(column_name: str, value):
    if value is None: return None
    val_str = str(value)
    col_lower = column_name.lower()

    # CPFs e Emails diretos
    if re.fullmatch(r"\d{3}[\.\-]?\d{3}[\.\-]?\d{3}[\.\-]?\d{2}", val_str): return _fake_cpf(val_str)
    if "@" in val_str and "." in val_str: return _fake_email(val_str)

    # Nomes com patentes/prefixos
    prefixo, miolo, sufixo = separar_casca(val_str)
    if is_person_name(miolo):
        return prefixo + _fake_name(miolo) + sufixo

    # Se for coluna sensível mas não caiu em nome (ex: RG)
    if is_sensitive_column(column_name):
        key = val_str.lower().strip()
        if key not in _generic_map: _generic_map[key] = fake.word()
        return _generic_map[key]

    return value

def anonymize_text_value(value):
    if not isinstance(value, str) or not value: return value

    # 1. Regex para CPF e Email
    text = re.sub(r"\d{3}[\.\-]?\d{3}[\.\-]?\d{3}[\.\-]?\d{2}", lambda m: _fake_cpf(m.group()), value)
    
    # 2. IA para nomes em textos
    if nlp:
        doc = nlp(text)
        # Ordenamos por tamanho para não substituir nomes parciais dentro de nomes maiores
        ents = sorted([e for e in doc.ents if e.label_ == "PER"], key=lambda x: len(x.text), reverse=True)
        for ent in ents:
            if ent.text.lower() not in BLACKLIST_PALAVRAS:
                # Mantém pontuação e prefixos se a IA pegou junto
                pref, mio, suf = separar_casca(ent.text)
                text = text.replace(ent.text, pref + _fake_name(mio) + suf)

    return text