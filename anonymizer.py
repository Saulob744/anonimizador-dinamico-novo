import re
from faker import Faker

fake = Faker("pt_BR")

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
    "nome", "name",
    "cpf",
    "email",
    "telefone", "phone", "celular",
    "rg", "documento"
]

BLACKLIST_PALAVRAS = {
    "roubo", "furto", "carro", "veículo", "rua", "avenida", 
    "bairro", "cidade", "estado", "polícia", "delegacia", 
    "boletim", "ocorrência", "crime", "vítima", "autor"
}

# ========================
# EXTRATOR UNIVERSAL DE PREFIXOS E PONTUAÇÃO (A MÁGICA AQUI)
# ========================
# Captura qualquer variação de Sr, Sra, Sr(a), Dr, Dra, Prof, etc., com ou sem ponto
PREFIX_REGEX = re.compile(r'^((?:sr|sra|sr\(a\)|senhor|senhora|dr|dra|doutor|doutora|prof|profa)\.?\s+)', re.IGNORECASE)
# Captura qualquer pontuação grudada no final
SUFFIX_REGEX = re.compile(r'([.,;!?]+)$')

def separar_casca(texto: str):
    """
    Separa a string em 3 partes: Prefixo (ex: 'Sr. '), Miolo ('Carlos Eduardo') e Sufixo (ex: ',')
    """
    prefixo = ""
    sufixo = ""
    miolo = texto

    # Extrai o prefixo (se existir)
    match_prefix = PREFIX_REGEX.match(miolo)
    if match_prefix:
        prefixo = match_prefix.group(1)
        miolo = miolo[len(prefixo):] # Remove o prefixo do miolo

    # Extrai o sufixo (se existir)
    match_suffix = SUFFIX_REGEX.search(miolo)
    if match_suffix:
        sufixo = match_suffix.group(1)
        miolo = miolo[:-len(sufixo)] # Remove o sufixo do miolo

    return prefixo, miolo, sufixo

# ========================
# NORMALIZAÇÃO
# ========================
def _normalize_key(value: str) -> str:
    return value.strip().lower()

# ========================
# REGEX BÁSICOS
# ========================
CPF_REGEX = re.compile(r"\d{3}[\.\-]?\d{3}[\.\-]?\d{3}[\.\-]?\d{2}")
EMAIL_REGEX = re.compile(r"[\w\.-]+@[\w\.-]+\.\w+")

_HAS_DIGIT = re.compile(r"\d")
_VALID_NAME_WORD = re.compile(r"^[A-ZÀ-Ü][a-zà-ü]+$")
_NAME_CONNECTOR = re.compile(r"^(de|da|do|dos|das|e)$", re.IGNORECASE)

# ========================
# DETECÇÃO DE COLUNA SENSÍVEL
# ========================
def is_sensitive_column(column_name: str) -> bool:
    col = column_name.lower()
    return any(k in col for k in SENSITIVE_KEYWORDS)

# ========================
# DETECÇÃO DE NOME
# ========================
def is_person_name(value: str) -> bool:
    if value in _person_name_cache:
        return _person_name_cache[value]

    result = _check_person_name(value)
    _person_name_cache[value] = result
    return result

def _check_person_name(value: str) -> bool:
    # Como a "casca" já foi removida antes de chamar essa função,
    # o value aqui será sempre apenas o nome limpo (ex: "Carlos Eduardo").
    if not isinstance(value, str):
        return False

    if len(value) < 5 or len(value) > 80:
        return False

    if _HAS_DIGIT.search(value):
        return False

    words = value.split()

    if len(words) < 2 or len(words) > 4:
        return False

    valid_words = 0

    for word in words:
        if _NAME_CONNECTOR.match(word):
            continue
        if word.lower() in BLACKLIST_PALAVRAS:
            return False
        if not _VALID_NAME_WORD.match(word):
            return False
        valid_words += 1

    return valid_words >= 2

# ========================
# FAKE GENERATORS
# ========================
def _fake_name(original: str) -> str:
    # O original aqui já chega limpinho sem Sr. ou pontuação
    key = _normalize_key(original)
    if key not in _name_map:
        _name_map[key] = fake.name()
    return _name_map[key]

def _fake_cpf(original: str) -> str:
    key = _normalize_key(original)
    if key not in _cpf_map:
        cpf = re.sub(r"\D", "", fake.cpf())
        _cpf_map[key] = cpf
    return _cpf_map[key]

def _fake_email(original: str) -> str:
    key = _normalize_key(original)
    if key not in _email_map:
        _email_map[key] = fake.email()
    return _email_map[key]

def _fake_phone(original: str) -> str:
    key = _normalize_key(original)
    if key not in _phone_map:
        _phone_map[key] = fake.phone_number()
    return _phone_map[key]

def _fake_generic(original: str) -> str:
    key = _normalize_key(original)
    if key not in _generic_map:
        _generic_map[key] = fake.word()
    return _generic_map[key]

# ========================
# ANONIMIZAÇÃO DIRETA (Tabelas e Colunas)
# ========================
def anonymize_value(column_name: str, value):
    if value is None:
        return None

    val = str(value)
    col = column_name.lower()

    if CPF_REGEX.fullmatch(val): return _fake_cpf(val)
    if EMAIL_REGEX.fullmatch(val): return _fake_email(val)
    if "telefone" in col or "phone" in col: return _fake_phone(val)

    # Aplica o separador de casca mesmo para valores diretos na coluna
    prefixo, miolo, sufixo = separar_casca(val)
    if is_person_name(miolo):
        return prefixo + _fake_name(miolo) + sufixo

    if is_sensitive_column(col): return _fake_generic(val)

    return value

# ========================
# ANONIMIZAÇÃO DE TEXTO LONGO (Relatos)
# ========================
def anonymize_text_value(value):
    if value is None:
        return None
    if not isinstance(value, str):
        return value

    text = value

    text = re.sub(CPF_REGEX, lambda m: _fake_cpf(m.group()), text)
    text = re.sub(EMAIL_REGEX, lambda m: _fake_email(m.group()), text)

    words = text.split()
    new_words = []

    i = 0
    while i < len(words):

        matched = False
        
        # Aumentamos para 5 para cobrir casos de nomes grandes (4 palavras + 1 prefixo)
        for window_size in [5, 4, 3, 2]:
            if i + (window_size - 1) < len(words):
                candidate_raw = " ".join(words[i:i+window_size])
                
                # Desmonta a string: Tira o Sr., o nome, e a pontuação
                prefixo, miolo, sufixo = separar_casca(candidate_raw)
                
                # Valida apenas o nome limpo
                if is_person_name(miolo):
                    fake_n = _fake_name(miolo)
                    
                    # Remonta perfeitamente e adiciona no texto
                    new_words.append(prefixo + fake_n + sufixo)
                    i += window_size
                    matched = True
                    break
        
        if matched:
            continue

        new_words.append(words[i])
        i += 1

    return " ".join(new_words)

# ========================
# TIPO DE TEXTO
# ========================
def is_text_column_type(data_type: str) -> bool:
    return any(t in data_type.lower() for t in ["char", "text", "varchar"])


