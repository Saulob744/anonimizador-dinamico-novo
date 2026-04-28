import re
import random
import string
import unicodedata
import hashlib
from faker import Faker
from gliner import GLiNER
import logging

logger = logging.getLogger(__name__)

# ==================================================
# CONFIG
# ==================================================
fake = Faker("pt_BR")
_gliner_model = None

# ==================================================
# GLINER CACHE (CARREGAMENTO PREGUIÇOSO)
# ==================================================
def get_gliner():
    global _gliner_model
    if _gliner_model is None:
        logger.info("Carregando modelo GLiNER na memória...")
        _gliner_model = GLiNER.from_pretrained("urchade/gliner_base") 
    return _gliner_model

GLINER_LABELS = ["person", "email", "phone number", "address", "organization", "location"]

# ==================================================
# REGEX (BASE FORTE - PRIORIDADE 1)
# ==================================================
REGEX = {
    "CPF": re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "CEP": re.compile(r"\b\d{5}-?\d{3}\b"),
    "COORDS": re.compile(r"-?\d{1,3}\.\d+,\s*-?\d{1,3}\.\d+")
}

NAME_REGEX = re.compile(
    r"\b([A-ZÀ-Ü][A-ZÀ-Üa-zà-ü']+(?:\s+(?:D\.|DA|DE|DO|DAS|DOS|[A-ZÀ-Ü][A-Za-zà-ü']+)){1,4})\b"
)

# Regex Específico para validar se a CÉLULA INTEIRA é apenas uma coordenada
PURE_COORD_PATTERN = re.compile(r"^-?\d{1,3}\.\d+,\s*-?\d{1,3}\.\d+$")

# ==================================================
# NORMALIZAÇÃO CANÔNICA
# ==================================================
def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text.upper()).strip()

def _canonical(value: str) -> str:
    v = _normalize(value)
    v = re.sub(r"\b([A-Z])\.", r"\1", v)
    return " ".join([p for p in v.split() if len(p) > 1])

def _fingerprint(value: str) -> str:
    base = _canonical(value)
    parts = sorted([p for p in base.split() if len(p) > 2])
    return hashlib.sha256(" ".join(parts).encode()).hexdigest()

# ==================================================
# GERADOR DETERMINÍSTICO (ZERO CONSUMO DE RAM)
# ==================================================
def _get_fake(value: str, typ: str) -> str:
    seed_str = _fingerprint(value) + typ
    seed = int(hashlib.sha256(seed_str.encode()).hexdigest()[:8], 16)
    
    fake.seed_instance(seed)
    random.seed(seed)

    if typ == "PER": return fake.name().upper()
    if typ == "CPF": return fake.cpf()
    if typ == "EMAIL": return fake.email()
    if typ == "PHONE": return fake.phone_number()
    if typ == "PLATE": return fake.license_plate().upper()
    if typ == "CEP": return fake.postcode()
    if typ == "ORG": return fake.company()
    if typ == "LOC": 
        base = _normalize(value).split()[0] if value.split() else "LOC"
        return f"{base}_REGIAO_{random.randint(1, 100)}"
    if typ == "COORDS":
        try:
            lat, lon = map(float, value.split(","))
            return f"{round(lat + random.uniform(-0.05, 0.05), 4)}, {round(lon + random.uniform(-0.05, 0.05), 4)}"
        except:
            return "-0.0000, -0.0000"
            
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=10))

# ==================================================
# CLASSIFICADOR CENTRAL
# ==================================================
def _resolve_type(raw_type: str) -> str:
    t = raw_type.lower()
    if "person" in t: return "PER"
    if "email" in t: return "EMAIL"
    if "phone" in t: return "PHONE"
    if "organization" in t: return "ORG"
    if "location" in t or "address" in t: return "LOC"
    return "UNK"

# ==================================================
# DETECÇÃO GLINER (OTIMIZADO E SILENCIOSO)
# ==================================================
def _detect_gliner(text: str):
    if len(text) < 10 or " " not in text:
        return []

    found = []
    try:
        model = get_gliner()
        preds = model.predict_entities(text, GLINER_LABELS, threshold=0.45)
        
        for ent in preds:
            typ = _resolve_type(ent["label"])
            found.append((ent["start"], ent["end"], ent["text"], typ))
            
    except Exception:
        pass
        
    return found

# ==================================================
# DETECTOR UNIFICADO
# ==================================================
def _detect_all(text: str):
    found = []

    for typ, pattern in REGEX.items():
        for m in pattern.finditer(text):
            found.append((m.start(), m.end(), m.group(), typ))

    for m in NAME_REGEX.finditer(text):
        raw = m.group()
        words = _canonical(raw).split()
        if len(words) >= 2 and not any(w.isdigit() for w in words):
            found.append((m.start(), m.end(), raw, "PER"))

    found.extend(_detect_gliner(text))

    found = sorted(found, key=lambda x: x[0])
    
    clean_found = []
    last_end = -1
    for s, e, v, typ in found:
        if s >= last_end: 
            clean_found.append((s, e, v, typ))
            last_end = e

    return clean_found

def reset_memory():
    """Reinicia as seeds de forma determinística."""
    fake.seed_instance(42)

# ==================================================
# ANONIMIZAÇÃO PRINCIPAL
# ==================================================
def anonymize_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        return text

    if text.isdigit() or len(text) < 3:
        return text

    entities = _detect_all(text)
    
    if not entities:
        return text

    result = []
    last = 0

    for s, e, v, typ in entities:
        result.append(text[last:s])
        repl = _get_fake(v, typ)
        result.append(repl)
        last = e

    result.append(text[last:])
    return "".join(result)


# ==================================================
# REDUTOR DE PRECISÃO GEOGRÁFICA (NOVO)
# ==================================================
def alter_geo_precision(value: str, precision: int = 3) -> str:
    """
    Corta as casas decimais de uma coordenada.
    Ex: -23.550520, -46.633308 -> -23.550, -46.633
    """
    try:
        lat_str, lon_str = value.split(",")
        lat = float(lat_str.strip())
        lon = float(lon_str.strip())
        return f"{lat:.{precision}f}, {lon:.{precision}f}"
    except:
        return value


# ==================================================
# API BANCO (COM SUPORTE A GPS DINÂMICO)
# ==================================================
def anonymize_value(col_name: str, val, anon_location: bool = True):
    if val is None or isinstance(val, (int, float, bool)):
        return val, None

    if type(val).__name__ in ['date', 'datetime', 'Timestamp']:
        return val, None

    val_str = str(val).strip()

    # 🎯 INTERCEPTAÇÃO: Se for puramente uma coordenada
    if PURE_COORD_PATTERN.match(val_str):
        if anon_location:
            # Ativo: Reduz a precisão para 3 casas (borra a exatidão)
            return alter_geo_precision(val_str, precision=3), "COORD"
        else:
            # Desativado: Retorna a coordenada real
            return val_str, None

    # Fluxo normal para CPFs, Nomes, Textos, etc.
    new_val = anonymize_text(val_str)
    return new_val, ("TEXT" if new_val != val_str else None)