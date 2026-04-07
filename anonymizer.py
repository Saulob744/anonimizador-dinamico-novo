import re
import spacy
from faker import Faker

fake = Faker("pt_BR")

# ========================
# REGEX GLOBAIS
# ========================
CPF_REGEX = re.compile(r"\d{3}[\.\-]?\d{3}[\.\-]?\d{3}[\.\-]?\d{2}")

# ========================
# CARREGAMENTO DA IA
# ========================
try:
    nlp = spacy.load("pt_core_news_lg")
except Exception as e:
    nlp = None
    print(f"AVISO: IA (spaCy) não carregada. Erro: {e}")

# ========================
# MAPAS DE CONSISTÊNCIA
# ========================
_name_map = {}
_first_name_map = {} # Vínculo: 'Roberto' -> 'Clarice'
_cpf_map = {}
_email_map = {}
_phone_map = {}
_generic_map = {}
_person_name_cache = {}

# ========================
# CONFIGURAÇÕES E FILTROS
# ========================
SENSITIVE_KEYWORDS = ["nome", "name", "cpf", "email", "telefone", "phone", "celular", "rg", "documento"]

TERMOS_PROIBIDOS = {
    "suspeito", "suspeita", "vítima", "vitima", "autor", "autora", "testemunha",
    "equipe", "viatura", "ocorrência", "invasao", "domicilio", "natureza",
    "condutor", "noticiante", "detido", "abordado", "desconhecido", "ignorado", "n/i"
}

BLACKLIST_PALAVRAS = {
    "roubo", "furto", "carro", "veículo", "rua", "avenida", "bairro", "cidade", 
    "estado", "polícia", "delegacia", "boletim", "ocorrência", "crime", 
    "invasao", "domicilio", "injuria", "ameaca", "estelionato", 
    "homicidio", "lesao", "militar", "civil", "guarnicao", "viatura"
}

# ========================
# PREFIXOS E CAMADA FINAL
# ========================
PATENTES = r"sd|cb|sgt|subten|ten|maj|cel|cap|asp|agent|pf|prf|senhor|senhora|sr|sra|dr|dra|prof|profa|inspetor"
PREFIX_REGEX = re.compile(rf'^((?:{PATENTES})\.?\s+)', re.IGNORECASE)
SUFFIX_REGEX = re.compile(r'([.,;!?]+)$')

CAMADA_FINAL_REGEX = re.compile(rf'\b({PATENTES})\.?\s+([A-ZÀ-Ü][a-zà-ü]+(?:\s+[A-ZÀ-Ü][a-zà-ü]+)*)\b', re.IGNORECASE)

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
    return prefixo, miolo.strip(), sufixo

def is_sensitive_column(column_name: str) -> bool:
    col = column_name.lower()
    return any(k in col for k in SENSITIVE_KEYWORDS)

def is_text_column_type(data_type: str) -> bool:
    return any(t in data_type.lower() for t in ["char", "text", "varchar"])

def is_person_name(value: str) -> bool:
    if not value or value.lower() in TERMOS_PROIBIDOS:
        return False
    if value in _person_name_cache:
        return _person_name_cache.get(value, False)
    if not isinstance(value, str) or len(value) < 2 or re.search(r"\d", value):
        return False
    if nlp:
        doc = nlp(value)
        result = any(ent.label_ == "PER" for ent in doc.ents) and value.lower() not in BLACKLIST_PALAVRAS
    else:
        result = bool(re.match(r"^[A-ZÀ-Ü][a-zà-ü]+(?:\s+[A-ZÀ-Ü][a-zà-ü]+)*$", value))
    _person_name_cache[value] = result
    return result

# ========================
# GERADORES FAKE (CORRIGIDO)
# ========================
def _fake_name(original: str) -> str:
    """Garante sincronia total entre nomes completos e primeiros nomes."""
    # 1. Normalização rigorosa
    clean_original = " ".join(original.strip().split())
    key = clean_original.lower()
    parts = clean_original.split()
    first_name_orig = parts[0].lower()

    # 2. Se o nome COMPLETO já existe no mapa, retorna ele
    if key in _name_map:
        return _name_map[key]

    # 3. Se o PRIMEIRO NOME já foi mapeado antes (através de um nome completo)
    if first_name_orig in _first_name_map:
        fake_first = _first_name_map[first_name_orig]
        
        # Se o input era só o primeiro nome, retorna o fake_first
        if len(parts) == 1:
            return fake_first
        
        # Se for nome completo, usa o primeiro nome já conhecido e gera um novo sobrenome
        new_full_name = f"{fake_first} {fake.last_name()}"
        _name_map[key] = new_full_name
        return new_full_name

    # 4. Se é um nome totalmente novo:
    new_first = fake.first_name()
    new_last = fake.last_name()
    new_full = f"{new_first} {new_last}"
    
    # Registra o vínculo do primeiro nome para usos futuros
    _first_name_map[first_name_orig] = new_first
    # Registra o nome completo
    _name_map[key] = new_full
    
    return new_first if len(parts) == 1 else new_full

def _fake_cpf(original: str) -> str:
    key = re.sub(r"\D", "", original)
    if key not in _cpf_map:
        _cpf_map[key] = re.sub(r"\D", "", fake.cpf())
    return _cpf_map[key]

# ========================
# INTERFACE COM O MAIN
# ========================
def anonymize_value(column_name: str, value):
    if value is None: return None
    val_str = str(value).strip()
    
    if val_str.lower() in TERMOS_PROIBIDOS: 
        return value
    if CPF_REGEX.fullmatch(val_str): 
        return _fake_cpf(val_str)
    
    prefixo, miolo, sufixo = separar_casca(val_str)
    
    if is_person_name(miolo):
        return prefixo + _fake_name(miolo) + sufixo

    if is_sensitive_column(column_name):
        # Evita o erro 'corrupti' gerando um nome humano para colunas sensíveis
        return _fake_name(val_str)

    return value

def anonymize_text_value(value):
    if not isinstance(value, str) or not value: return value

    # 1. Troca CPFs
    text = CPF_REGEX.sub(lambda m: _fake_cpf(m.group()), value)
    
    # 2. IA para Nomes (Ordenado por tamanho para priorizar nomes completos)
    if nlp:
        doc = nlp(text)
        ents = sorted([e for e in doc.ents if e.label_ == "PER"], key=lambda x: len(x.text), reverse=True)
        for ent in ents:
            original_ent = ent.text
            if original_ent.lower() not in BLACKLIST_PALAVRAS and original_ent.lower() not in TERMOS_PROIBIDOS:
                pref, mio, suf = separar_casca(original_ent)
                # O _fake_name agora garante a consistência entre 'Roberto Almeida' e 'Roberto'
                nome_fake = pref + _fake_name(mio) + suf
                # Substituição com \b para garantir que troque palavras exatas
                text = re.sub(rf"\b{re.escape(original_ent)}\b", nome_fake, text, flags=re.IGNORECASE)

    # 3. Camada Final (Patentes)
    def replace_final(match):
        cargo = match.group(1)
        nome = match.group(2)
        if nome.lower() in TERMOS_PROIBIDOS or nome.lower() in BLACKLIST_PALAVRAS:
            return match.group(0)
        return f"{cargo} {_fake_name(nome)}"

    text = CAMADA_FINAL_REGEX.sub(replace_final, text)

    return text