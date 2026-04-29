import re, random, string, unicodedata, hashlib, logging
from faker import Faker
from gliner import GLiNER

logger = logging.getLogger(__name__)

# ==================================================
# CONFIGURAÇÕES GERAIS E IA
# ==================================================
fake = Faker("pt_BR")
_gliner_model = None

# GLiNER agora só será usado para colunas de Texto Livre (Observações)
GLINER_LABELS = ["person", "first name", "email", "phone number", "address", "organization"]

def get_gliner():
    global _gliner_model
    if _gliner_model is None:
        logger.info("Carregando modelo GLiNER na memória...")
        _gliner_model = GLiNER.from_pretrained("urchade/gliner_base")
    return _gliner_model

# ==================================================
# MOTORES DE BUSCA (REGEX PARA TEXTO LIVRE)
# ==================================================
REGEX = {
    "CPF": re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "CEP": re.compile(r"\b\d{5}-?\d{3}\b"),
    "COORDS": re.compile(r"-?\d{1,3}\.\d+,\s*-?\d{1,3}\.\d+"),
    "CODE": re.compile(r"\b(?=[A-Za-z-]*\d)(?=[0-9-]*[A-Za-z])[A-Za-z0-9-]{5,}\b")
}

UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
NAME_REGEX = re.compile(r"\b([A-ZÀ-Ü][A-ZÀ-Üa-zà-ü']+(?:\s+(?:D\.|DA|DE|DO|DAS|DOS|[A-ZÀ-Ü][A-Za-zà-ü']+)){1,4})\b")
PURE_COORD_PATTERN = re.compile(r"^-?\d{1,3}\.\d+,\s*-?\d{1,3}\.\d+$")

# ==================================================
# FUNÇÕES CORE (NORMALIZAÇÃO E HASH)
# ==================================================
def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", "".join(c for c in text if not unicodedata.combining(c)))).upper().strip()

def _canonical(value: str) -> str:
    v = re.sub(r"\b([A-Z])\.", r"\1", _normalize(value))
    return " ".join([p for p in v.split() if len(p) > 1])

def _fingerprint(value: str) -> str:
    return hashlib.sha256(" ".join(sorted([p for p in _canonical(value).split() if len(p) > 2])).encode()).hexdigest()

# ==================================================
# GERADOR DETERMINÍSTICO
# ==================================================
def _get_fake(value: str, typ: str) -> str:
    seed = int(hashlib.sha256((_fingerprint(value) + typ).encode()).hexdigest()[:8], 16)
    fake.seed_instance(seed)
    random.seed(seed)

    if typ == "UUID": return fake.uuid4()
    if typ == "PER": return fake.name().upper()
    if typ == "CPF": return fake.cpf()
    if typ == "EMAIL": return fake.email()
    if typ == "PHONE": return fake.phone_number()
    if typ == "PLATE": return fake.license_plate().upper()
    if typ == "CEP": return fake.postcode()
    if typ == "ORG": return fake.company()
    
    if typ == "LOC":
        # Extrai a primeira palavra para manter contexto (ex: "Rua_REGIAO_42")
        base = _normalize(value).split()[0] if value.split() else "LOC"
        return f"{base}_REGIAO_{random.randint(10, 99)}"

    if typ == "CODE":
        return "".join(random.choices(string.ascii_uppercase + string.digits, k=max(5, len(value))))

    return "".join(random.choices(string.ascii_uppercase + string.digits, k=10))

# ==================================================
# DETECÇÃO PARA TEXTO LIVRE (OBSERVAÇÕES)
# ==================================================
def _resolve_type(raw_type: str) -> str:
    t = raw_type.lower()
    if any(k in t for k in ["person", "name", "suspect", "victim", "employee"]): return "PER"
    if "email" in t: return "EMAIL"
    if "phone" in t: return "PHONE"
    if "organization" in t: return "ORG"
    if any(k in t for k in ["location", "address"]): return "LOC"
    return "UNK"

def _detect_gliner(text: str):
    if len(text) < 10 or " " not in text: return []
    try:
        preds = get_gliner().predict_entities(text, GLINER_LABELS, threshold=0.35)
        return [ (e["start"], e["end"], e["text"], _resolve_type(e["label"])) for e in preds ]
    except Exception:
        return []

def _detect_all(text: str):
    found = []
    for typ, pattern in REGEX.items():
        for m in pattern.finditer(text): found.append((m.start(), m.end(), m.group(), typ))

    for m in NAME_REGEX.finditer(text):
        raw = m.group()
        if len(_canonical(raw).split()) >= 2:
            found.append((m.start(), m.end(), raw, "PER"))

    found.extend(_detect_gliner(text))

    clean_found, last_end = [], -1
    for s, e, v, typ in sorted(found, key=lambda x: (x[0], -(x[1]-x[0]))):
        if s >= last_end:
            clean_found.append((s, e, v, typ))
            last_end = e
    return clean_found

def reset_memory():
    fake.seed_instance(42)

def alter_geo_precision(value: str, precision: int = 3) -> str:
    try:
        lat, lon = map(float, value.split(","))
        return f"{lat:.{precision}f}, {lon:.{precision}f}"
    except: return value

def anonymize_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip() or (text.isdigit() or len(text) < 3):
        return text

    entities = _detect_all(text)
    if not entities: return text

    result, last = [], 0
    for s, e, v, typ in entities:
        result.extend([text[last:s], _get_fake(v, typ)])
        last = e

    result.append(text[last:])
    return "".join(result)

# ==================================================
# ROTEADOR SEMÂNTICO DE COLUNAS (O CÉREBRO DINÂMICO)
# ==================================================
def anonymize_value(col_name: str, val, anon_location: bool = True):
    # 1. Ignora tipos rápidos que não são texto sensível
    if val is None or isinstance(val, (int, float, bool)) or type(val).__name__ in ['date', 'datetime', 'Timestamp']:
        return val, None

    val_str = str(val).strip()

    # 2. Intercepta UUIDs (Blindagem do BD)
    if UUID_PATTERN.match(val_str):
        return _get_fake(val_str, "UUID"), "UUID"

    # 3. Intercepta Coordenadas Puras
    if PURE_COORD_PATTERN.match(val_str):
        return (alter_geo_precision(val_str, 3), "COORD") if anon_location else (val_str, None)

    # 4. 🚀 ROTEAMENTO DINÂMICO POR NOME DA COLUNA
    c = col_name.lower()

    # A) COLUNAS SEGURAS (Pula processamento, economiza CPU e evita falsos positivos)
    if any(k in c for k in ["crime", "tipo", "status", "situacao", "estado", "cidade", "natureza", "sexo", "genero", "profissao", "cor", "marca", "modelo"]):
        return val_str, None

    # B) COLUNAS ESTRUTURADAS (Troca tudo diretamente sem ler o texto interno)
    if any(k in c for k in ["nome", "vitima", "suspeito", "autor", "pessoa", "testemunha"]):
        return _get_fake(val_str, "PER"), "PER"
        
    if any(k in c for k in ["endereco", "rua", "logradouro", "bairro", "local"]):
        return _get_fake(val_str, "LOC"), "LOC"
        
    if any(k in c for k in ["email", "mail"]):
        return _get_fake(val_str, "EMAIL"), "EMAIL"
        
    if any(k in c for k in ["fone", "celular", "telefone"]):
        return _get_fake(val_str, "PHONE"), "PHONE"
        
    if any(k in c for k in ["cpf", "cnpj", "rg", "documento"]):
        return _get_fake(val_str, "CPF"), "CPF"

    if any(k in c for k in ["placa", "veiculo"]):
        return _get_fake(val_str, "PLATE"), "PLATE"

    # C) TEXTO LIVRE (Se não for nenhuma coluna acima, como "observacoes" ou "historico")
    # Passa pelo motor pesado (Regex + IA)
    new_val = anonymize_text(val_str)
    return new_val, ("TEXT" if new_val != val_str else None)