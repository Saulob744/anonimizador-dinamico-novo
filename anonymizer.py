import re
import random
import string
import unicodedata
import hashlib
import logging
from functools import lru_cache
from faker import Faker
from gliner import GLiNER

logger = logging.getLogger(__name__)

# =========================================================
# CONFIGURAÇÕES GLOBAIS
# =========================================================
_MAPPING_CACHE = {}
_USED_FAKES = set()
fake = Faker("pt_BR")
_gliner_model = None

CACHE_LIMIT = 500000
GLINER_MIN_TEXT = 40 
DEBUG_MODE = True

# A MÁGICA: Passamos rótulos "iscas". A IA vai separar o que é médico/legal do que é nome real.
GLINER_LABELS = [
    "person",              # O que queremos
    "address",             # O que queremos
    "organization",        # O que queremos
    "medical condition",   # Isca para não pegar doenças
    "body part",           # Isca para não pegar partes do corpo
    "measurement",         # Isca para não pegar cm, ml, kg
    "profession",          # Isca para não pegar médico, juiz, legista
    "legal term"           # Isca para não pegar processo, boletim
]

# =========================================================
# REGEX PRINCIPAIS
# =========================================================
REGEX = {
    "CPF": re.compile(r"\b(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(\d{2}\)|\d{2})\s?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "LOC": re.compile(r"\b(Rua|Av|Avenida|Alameda|Travessa|Pca|Praca)\s+([A-ZÀ-Ü0-9][^\s,]+(\s+[A-ZÀ-Ü0-9][^\s,]+){0,4})\b", re.IGNORECASE),
    "COORD": re.compile(r"^\s*-?(?:90(?:\.0+)?|[0-8]?\d(?:\.\d+)?)\s*,\s*-?(?:180(?:\.0+)?|1[0-7]\d(?:\.\d+)?|\d{1,2}(?:\.\d+)?)\s*$"),
    "LAT": re.compile(r"^\s*-?(?:90(?:\.0+)?|[0-8]?\d(?:\.\d+)?)\s*$"),
    "LONG": re.compile(r"^\s*-?(?:180(?:\.0+)?|1[0-7]\d(?:\.\d+)?|\d{1,2}(?:\.\d+)?)\s*$"),
}

# Regex Segura: Pega apenas formato "Nome Sobrenome" perfeito (evita "Fratura De", etc)
NAME_REGEX = re.compile(r"\b([A-ZÀ-Ü][a-zà-ü]+(?:\s(?:de|da|do|dos|das)\s|\s)[A-ZÀ-Ü][a-zà-ü]+(?:\s[A-ZÀ-Ü][a-zà-ü]+){0,3})\b")

UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
DOC_PATTERN = re.compile(r"\b(cpf|rg|documentos?|cnpj|cnh|passaporte)\b")
NAME_COL_PATTERN = re.compile(r"\b(nome|nomes|razao social)\b")

SENSITIVE_HINTS = re.compile(r"\b(nome|mae|pai|filiacao|suspeito|autor|vitima|indiciado|cpf|rg|telefone|email|endereco|usuario|funcionario|servidor|pessoa)\b", re.IGNORECASE)
NON_SENSITIVE_HINTS = re.compile(r"\b(natureza|crime|tipo|status|descricao|historico|categoria|municipio|cidade|bairro|marca|modelo|cor|orgao|setor|departamento|processo|protocolo|codigo|id)\b", re.IGNORECASE)
CITY_LIKE_PATTERN = re.compile(r"^[A-ZÀ-Ü][a-zà-ü]+(?:\s[A-ZÀ-Ü][a-zà-ü]+)?$")

# =========================================================
# FUNÇÕES DE BASE (Mantidas Intactas)
# =========================================================
def debug_log(msg):
    if DEBUG_MODE:
        logger.warning(msg)

@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", "", text.upper().strip())

def _fingerprint(value: str) -> str:
    parts = sorted(p for p in _normalize(value).split() if p not in {"DA", "DE", "DO", "DAS", "DOS"})
    return hashlib.sha256(" ".join(parts).encode()).hexdigest()

def _get_fake(value: str, typ: str) -> str:
    global _MAPPING_CACHE
    if len(_MAPPING_CACHE) > CACHE_LIMIT: _MAPPING_CACHE.clear()
    cache_key = f"{typ}:{_normalize(value)}"
    if cache_key in _MAPPING_CACHE: return _MAPPING_CACHE[cache_key]

    seed = int(_fingerprint(value)[:8], 16)
    attempts = 0

    while True:
        fake.seed_instance(seed + attempts)
        random.seed(seed + attempts)
        try:
            if typ == "UUID": val = str(fake.uuid4())
            elif typ in {"PER", "NAME"}: val = fake.name().upper()
            elif typ == "CPF": val = fake.cpf()
            elif typ == "EMAIL": val = fake.email()
            elif typ == "PLATE": val = fake.license_plate().upper()
            elif typ == "LOC": 
                prefix = value.split()[0].upper() if " " in value else "RUA"
                val = f"{prefix} {fake.name().upper()}"
            elif typ == "COORD":
                lat, lon = map(float, value.split(","))
                val = f"{lat + random.uniform(-0.003, 0.003):.4f}, {lon + random.uniform(-0.003, 0.003):.4f}"
            elif typ in {"LAT", "LONG"}:
                val = f"{float(value) + random.uniform(-0.003, 0.003):.4f}"
            else:
                val = "".join(random.choices(string.ascii_uppercase + string.digits, k=max(5, len(value))))
        except Exception:
            val = value

        if typ in {"CPF", "UUID", "PER", "NAME"} and val in _USED_FAKES:
            attempts += 1
            if attempts > 50: break
            continue

        if typ in {"CPF", "UUID", "PER", "NAME"}: _USED_FAKES.add(val)
        _MAPPING_CACHE[cache_key] = val
        return val

# =========================================================
# IA: GLINER
# =========================================================
def get_gliner():
    global _gliner_model
    if _gliner_model is None:
        # AQUI VOCÊ PODE MUDAR O MODELO NO FUTURO. 
        # O base é rápido, mas para o seu caso o Multilingual entende melhor PT-BR.
        _gliner_model = GLiNER.from_pretrained("urchade/gliner_base")
    return _gliner_model

# =========================================================
# DETECÇÃO HÍBRIDA (REGEX + IA + STOP WORDS)
# =========================================================
def _detect_all(text: str, anon_loc: bool):
    found = []

    # 1. Regex de Estruturas Fixas (CPF, Email, Telefone, etc)
    for typ, pat in REGEX.items():
        if typ in {"COORD", "LOC"} and not anon_loc: continue
        for match in pat.finditer(text):
            found.append((match.start(), match.end(), match.group(), typ))

    # 2. Regex Segura de Nomes (Apenas garante formato "Nome Sobrenome")
    for match in NAME_REGEX.finditer(text):
        found.append((match.start(), match.end(), match.group(), "PER"))

    # 3. IA Lendo o Contexto
    if len(text) >= GLINER_MIN_TEXT:
        try:
            preds = get_gliner().predict_entities(
                text,
                GLINER_LABELS,
                threshold=0.55, # Um nível seguro de confiança
            )

            for entity in preds:
                lbl = entity["label"].lower()
                ent_text = entity["text"]

                # SE NÃO FOR PESSOA, ENDEREÇO OU ORGANIZAÇÃO, NÓS IGNORAMOS!
                # Isso mata as alucinações médicas/legais sem usar Stop Words
                if lbl not in ["person", "address", "organization"]:
                    continue

                # Evita nomes curtos demais ou que sejam só números
                if len(ent_text) <= 2 or ent_text.isdigit():
                    continue

                typ = "PER" if lbl == "person" else "LOC" if lbl == "address" else "ORG"
                
                if typ == "LOC" and not anon_loc: continue
                found.append((entity["start"], entity["end"], ent_text, typ))

        except Exception as e:
            debug_log(f"[GLINER ERROR] {e}")

    # Remove duplicatas
    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, v, t))
            last = e

    return clean

# =========================================================
# EXECUÇÃO DA ANONIMIZAÇÃO
# =========================================================
def anonymize_text(text: str, anon_loc: bool = True) -> str:
    if not isinstance(text, str): return text
    text = text.strip()
    if len(text) < 3: return text

    entities = _detect_all(text, anon_loc)
    if not entities: return text

    result = []
    last = 0
    for s, e, v, t in entities:
        result.extend([text[last:s], _get_fake(v, t)])
        last = e

    result.append(text[last:])
    return "".join(result)

def anonymize_value(col_name: str, val, anon_location: bool = True):
    if val is None or isinstance(val, (int, float, bool)) or type(val).__name__ in {"date", "datetime", "Timestamp"}:
        return val, None

    v_str = str(val).strip()
    col_clean = col_name.lower().replace("_", " ").replace("-", " ")

    if DOC_PATTERN.search(col_clean): return _get_fake(v_str, "CPF"), "TEXT"
    if NAME_COL_PATTERN.search(col_clean): return _get_fake(v_str, "PER"), "TEXT"
    if UUID_PATTERN.match(v_str): return _get_fake(v_str, "UUID"), "UUID"

    new_v = anonymize_text(v_str, anon_location)
    return new_v, ("TEXT" if new_v != v_str else None)

def reset_memory():
    global _MAPPING_CACHE, _USED_FAKES
    _MAPPING_CACHE.clear()
    _USED_FAKES.clear()
    _normalize.cache_clear()

# =========================================================
# SCORE DE SENSIBILIDADE DE COLUNA
# =========================================================
def score_column_sensitivity(col_name: str, sample_values) -> dict:
    score, reasons = 0, []
    col_clean = str(col_name).lower().replace("_", " ").replace("-", " ")

    if SENSITIVE_HINTS.search(col_clean):
        score += 40
        reasons.append("sensitive_column_name")
    if NON_SENSITIVE_HINTS.search(col_clean):
        score -= 50
        reasons.append("non_sensitive_column_name")

    valid_samples = [str(v).strip() for v in sample_values if v is not None and str(v).strip()][:100]
    if not valid_samples: return {"score": score, "decision": False, "reasons": reasons}

    total = len(valid_samples)
    person_hits, structured_hits, city_hits, short_text_hits, categorical_hits = 0, 0, 0, 0, 0
    unique_ratio = len(set([_normalize(v) for v in valid_samples])) / total

    for val in valid_samples:
        val_clean = val.strip()
        for typ, pat in REGEX.items():
            if pat.search(val_clean):
                if typ in {"CPF", "EMAIL", "PHONE", "LOC"}: structured_hits += 1
                break

        if NAME_REGEX.search(val_clean): person_hits += 1
        
        if len(val_clean.split()) <= 2: short_text_hits += 1
        if len(val_clean) < 30 and not any(ch.isdigit() for ch in val_clean): categorical_hits += 1

    person_ratio, structured_ratio, short_ratio, categorical_ratio = person_hits/total, structured_hits/total, short_text_hits/total, categorical_hits/total

    score += int(person_ratio * 60) + int(structured_ratio * 35)

    if short_ratio > 0.75: score -= 25; reasons.append("mostly_short_values")
    if categorical_ratio > 0.70: score -= 20; reasons.append("categorical_values")
    if unique_ratio < 0.35: score -= 20; reasons.append("high_repetition")

    threshold = 55 if NON_SENSITIVE_HINTS.search(col_clean) else 35

    return {"decision": score >= threshold}

def should_anonymize_column(col_name: str, sample_values) -> bool:
    try: return score_column_sensitivity(col_name, sample_values)["decision"]
    except Exception: return True