import re, random, string, unicodedata, hashlib, logging
from faker import Faker
from gliner import GLiNER

logger = logging.getLogger(__name__)

_MAPPING_CACHE, _USED_FAKES = {}, set()
fake = Faker("pt_BR")
_gliner_model = None

# Configurações de Detecção - Regex Otimizados e Precisos
GLINER_LABELS = ["person", "email", "phone number", "address", "organization"]
REGEX = {
    # Exige CPF formatado com todos os pontos/hífen ou apenas 11 números seguidos
    "CPF": re.compile(r"\b(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    # Exige DDD com parênteses completos ou sem, evitando pegar um parêntese isolado
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(\d{2}\)|\d{2})\s?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "COORD": re.compile(r"-?\d{1,3}\.\d+,\s*-?\d{1,3}\.\d+"),
    "LOC": re.compile(r"\b(Rua|Av|Avenida|Alameda|Travessa|Pca|Praca)\s+([A-ZÀ-Ü0-9][^\s,]+(\s+[A-ZÀ-Ü0-9][^\s,]+){0,4})\b", re.IGNORECASE),
    "CODE": re.compile(r"\b(?=[A-Za-z-]*\d)(?=[0-9-]*[A-Za-z])[A-Za-z0-9-]{5,}\b")
}

# Regex de Nome: Permite maiúsculas e minúsculas, limite de 2 a 4 palavras.
NAME_REGEX = re.compile(r"\b([A-ZÀ-Ü][a-zA-ZÀ-Üà-ü]{1,}(?:\s+(?:d[eao]s?|D[EAO]S?|[A-ZÀ-Ü][a-zA-ZÀ-Üà-ü]{1,}\.?)){1,3})\b")
UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


def _normalize(t: str) -> str:
    if not t: return ""
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", "", t.upper().strip())


def _fingerprint(v: str) -> str:
    parts = sorted([p for p in _normalize(v).split() if p not in ["DA", "DE", "DO", "DAS", "DOS"]])
    return hashlib.sha256(" ".join(parts).encode()).hexdigest()


def _get_fake(value: str, typ: str) -> str:
    ckey = f"{typ}:{_normalize(value)}"
    if ckey in _MAPPING_CACHE: return _MAPPING_CACHE[ckey]

    seed = int(_fingerprint(value)[:8], 16)
    attempts = 0
    while True:
        fake.seed_instance(seed + attempts)
        random.seed(seed + attempts)

        if typ == "UUID": val = str(fake.uuid4())
        elif typ in ["PER", "NAME"]: val = fake.name().upper()
        elif typ == "CPF": val = fake.cpf()
        elif typ == "EMAIL": val = fake.email()
        elif typ == "PLATE": val = fake.license_plate().upper()
        elif typ == "LOC":
            prefix = value.split()[0].upper() if " " in value else "RUA"
            val = f"{prefix} {fake.name().upper()}"
        elif typ == "COORD":
            try:
                lat, lon = map(float, value.split(","))
                val = f"{lat + random.uniform(-0.009, 0.009):.4f}, {lon + random.uniform(-0.009, 0.009):.4f}"
            except: val = value
        else: val = "".join(random.choices(string.ascii_uppercase + string.digits, k=max(5, len(value))))

        if typ in ["CPF", "UUID", "PER", "NAME"] and val in _USED_FAKES:
            attempts += 1
            if attempts > 50: break
            continue
        
        if typ in ["CPF", "UUID", "PER", "NAME"]: _USED_FAKES.add(val)
        _MAPPING_CACHE[ckey] = val
        return val


def get_gliner():
    global _gliner_model
    if _gliner_model is None: _gliner_model = GLiNER.from_pretrained("urchade/gliner_base")
    return _gliner_model


def _detect_all(text: str, anon_loc: bool):
    found = []
    
    # 1. Regex
    for typ, pat in REGEX.items():
        if typ in ["COORD", "LOC"] and not anon_loc: continue
        for m in pat.finditer(text): found.append((m.start(), m.end(), m.group(), typ))

    # 2. Nomes Próprios
    for m in NAME_REGEX.finditer(text): found.append((m.start(), m.end(), m.group(), "PER"))
    
    # 3. IA (GLiNER)
    try:
        preds = get_gliner().predict_entities(text, GLINER_LABELS, threshold=0.30)
        for e in preds:
            lbl = e["label"].lower()
            typ = "PER" if "person" in lbl else "LOC" if "address" in lbl else "ORG"
            if typ == "LOC" and not anon_loc: continue
            found.append((e["start"], e["end"], e["text"], typ))
    except: pass

    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, v, t))
            last = e
    return clean


def anonymize_text(text: str, anon_loc: bool = True) -> str:
    if not isinstance(text, str) or len(text.strip()) < 3: return text
    
    entities = _detect_all(text, anon_loc)
    if not entities: return text

    res, last = [], 0
    for s, e, v, t in entities:
        res.extend([text[last:s], _get_fake(v, t)])
        last = e
    res.append(text[last:])
    return "".join(res)


def anonymize_value(col_name: str, val, anon_location: bool = True):
    if val is None or isinstance(val, (int, float, bool)) or type(val).__name__ in ['date', 'datetime', 'Timestamp']:
        return val, None

    v_str = str(val).strip()
    col_lower = col_name.lower()

    if any(k in col_lower for k in ["cpf", "rg", "documento"]):
        return _get_fake(v_str, "CPF"), "TEXT"
    
    if any(k in col_lower for k in ["nome", "razao_social"]):
        return _get_fake(v_str, "PER"), "TEXT"

    if UUID_PATTERN.match(v_str):
        return _get_fake(v_str, "UUID"), "UUID"
    
    new_v = anonymize_text(v_str, anon_location)
    return new_v, ("TEXT" if new_v != v_str else None)


def reset_memory():
    global _MAPPING_CACHE, _USED_FAKES
    _MAPPING_CACHE, _USED_FAKES = {}, set()