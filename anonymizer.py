import re
import random
import string
import unicodedata
import hashlib
import logging
from functools import lru_cache
from faker import Faker
from gliner import GLiNER

# Configuração de Logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================================================
# CONFIGURAÇÕES GLOBAIS E ESTADO
# =========================================================
_MAPPING_CACHE = {}
_USED_FAKES = set()
fake = Faker("pt_BR")
_gliner_model = None

CACHE_LIMIT = 500000
GLINER_MIN_TEXT = 80
DEBUG_MODE = True

GLINER_LABELS = ["person", "email", "phone number", "address", "organization"]

# =========================================================
# REGEX E HEURÍSTICAS
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
    "CODE": re.compile(r"\b(?=[A-Za-z-]*\d)(?=[0-9-]*[A-Za-z])[A-Za-z0-9-]{5,}\b"),
    "PER_COMMON": re.compile(r"\b(maria|jos[eé]|jo[aã]o|ana|carlos|paulo|pedro|lucas|luiz|gabriel|rafael|fernando|roberto|mariana|patricia|camila|amanda|bruna|julia|marcos|diego|ricardo|gustavo)\b", re.IGNORECASE),
}

NAME_REGEX = re.compile(r"\b([A-ZÀ-Ü][a-zA-ZÀ-Üà-ü]{1,}(?:\s+(?:d[eao]s?|D[EAO]S?|[A-ZÀ-Ü][a-zA-ZÀ-Üà-ü]{1,}\.?)){1,3})\b")
UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")

# Dicas para o Score
SENSITIVE_HINTS = re.compile(r"\b(nome|mae|pai|filiacao|suspeito|autor|vitima|indiciado|cpf|rg|telefone|email|endereco|usuario|funcionario|servidor|pessoa)\b", re.IGNORECASE)
NON_SENSITIVE_HINTS = re.compile(r"\b(natureza|crime|tipo|status|descricao|historico|categoria|municipio|cidade|bairro|marca|modelo|cor|orgao|setor|departamento|processo|protocolo|codigo|id)\b", re.IGNORECASE)
CITY_LIKE_PATTERN = re.compile(r"^[A-ZÀ-Ü][a-zà-ü]+(?:\s[A-ZÀ-Ü][a-zà-ü]+)?$")

# =========================================================
# UTILITÁRIOS
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

def get_gliner():
    global _gliner_model
    if _gliner_model is None:
        _gliner_model = GLiNER.from_pretrained("urchade/gliner_base")
    return _gliner_model

# =========================================================
# GERAÇÃO DINÂMICA DE FAKES
# =========================================================
def _get_fake(value: str, typ: str) -> str:
    global _MAPPING_CACHE
    if len(_MAPPING_CACHE) > CACHE_LIMIT:
        _MAPPING_CACHE.clear()

    cache_key = f"{typ}:{_normalize(value)}"
    if cache_key in _MAPPING_CACHE:
        return _MAPPING_CACHE[cache_key]

    seed = int(_fingerprint(value)[:8], 16)
    attempts = 0
    val = value

    while attempts < 50:
        fake.seed_instance(seed + attempts)
        random.seed(seed + attempts)
        try:
            if typ == "UUID": val = str(fake.uuid4())
            elif typ in {"PER", "NAME"}: val = fake.name().upper()
            elif typ == "PER_COMMON": val = fake.first_name().upper()
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
        except:
            break

        if typ not in {"CPF", "UUID", "PER", "NAME"} or val not in _USED_FAKES:
            break
        attempts += 1

    if typ in {"CPF", "UUID", "PER", "NAME"}:
        _USED_FAKES.add(val)
    
    _MAPPING_CACHE[cache_key] = val
    return val

# =========================================================
# O CÉREBRO: SCORE DE SENSIBILIDADE
# =========================================================
def score_column_sensitivity(col_name: str, sample_values) -> dict:
    """Versão unificada e dinâmica para análise de colunas."""
    score = 0
    reasons = []
    col_clean = str(col_name).lower().replace("_", " ").replace("-", " ")

    # Camada 1: Heurística do Nome
    if SENSITIVE_HINTS.search(col_clean):
        score += 45
        reasons.append("hint_positive")
    if NON_SENSITIVE_HINTS.search(col_clean):
        score -= 55
        reasons.append("hint_negative")

    valid_samples = [str(v).strip() for v in sample_values if v and str(v).strip()][:100]
    if not valid_samples:
        return {"score": score, "decision": False, "reasons": reasons}

    # Camada 2: Métricas de Dados
    person_hits = 0
    structured_hits = 0
    city_hits = 0
    total = len(valid_samples)
    unique_vals = len(set(_normalize(v) for v in valid_samples)) / total

    for val in valid_samples:
        # Regex Check
        found_regex = False
        for typ, pat in REGEX.items():
            if pat.search(val):
                if typ in {"CPF", "EMAIL", "PHONE", "LOC"}:
                    structured_hits += 1
                found_regex = True
                break
        
        # Person Check
        if NAME_REGEX.search(val) or REGEX["PER_COMMON"].search(val):
            person_hits += 1
        
        # City Check
        if CITY_LIKE_PATTERN.match(val) and len(val.split()) <= 3:
            city_hits += 1

    # Cálculos de Proporção
    person_ratio = person_hits / total
    structured_ratio = structured_hits / total
    city_ratio = city_hits / total

    score += int(person_ratio * 60)
    score += int(structured_ratio * 40)
    
    # Penalidades Dinâmicas
    if city_ratio > 0.45:
        score -= 50
        reasons.append("likely_city_column")
    if unique_vals < 0.25 and person_ratio < 0.5:
        score -= 30
        reasons.append("low_cardinality")

    # Decisão
    threshold = 50 if "natureza" in col_clean or "tipo" in col_clean else 35
    decision = score >= threshold

    debug_log(f"[SCORE] {col_name} | Score: {score} | Decision: {decision}")
    
    return {
        "score": score,
        "decision": decision,
        "reasons": reasons,
        "metrics": {"person": person_ratio, "structured": structured_ratio, "unique": unique_vals}
    }

# =========================================================
# INTERFACES DE ANONIMIZAÇÃO
# =========================================================
def _detect_entities(text: str, anon_loc: bool):
    found = []
    # Regex
    for typ, pat in REGEX.items():
        if typ in {"COORD", "LOC"} and not anon_loc: continue
        for m in pat.finditer(text):
            found.append((m.start(), m.end(), m.group(), typ))
    
    # NER Dinâmico (GLiNER)
    if len(text) >= GLINER_MIN_TEXT:
        try:
            preds = get_gliner().predict_entities(text, GLINER_LABELS, threshold=0.35)
            for e in preds:
                lbl = e["label"].lower()
                typ = "PER" if "person" in lbl else "LOC" if "address" in lbl else "ORG"
                if typ == "LOC" and not anon_loc: continue
                found.append((e["start"], e["end"], e["text"], typ))
        except: pass

    # Resolve sobreposições (Garante que o maior match vença)
    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, v, t))
            last = e
    return clean

def anonymize_text(text: str, anon_loc: bool = True) -> str:
    if not isinstance(text, str) or len(text) < 3: return text
    entities = _detect_entities(text, anon_loc)
    if not entities: return text

    result, last = [], 0
    for s, e, v, t in entities:
        result.extend([text[last:s], _get_fake(v, t)])
        last = e
    result.append(text[last:])
    return "".join(result)

def anonymize_value(col_name: str, val, anon_location: bool = True):
    if val is None or isinstance(val, (int, float, bool)):
        return val, None
    
    v_str = str(val).strip()
    col_clean = col_name.lower()

    # Atalhos para colunas óbvias
    if any(x in col_clean for x in ["cpf", "cnpj", "documento"]):
        return _get_fake(v_str, "CPF"), "STRUCT"
    if "nome" in col_clean and "cidade" not in col_clean:
        return _get_fake(v_str, "PER"), "STRUCT"
    
    new_v = anonymize_text(v_str, anon_location)
    return new_v, ("TEXT" if new_v != v_str else None)

import re

def should_anonymize_column(col_name, samples):
    """
    Analisa se uma coluna deve ser processada pelo anonimizador.
    Retorna True para dados sensíveis e False para dados comuns/técnicos.
    """
    col_lower = str(col_name).lower()
    
    # 1. LISTA NEGRA: Palavras-chave que sempre pedem anonimização
    keywords = [
        'nome', 'email', 'phone', 'tel', 'celular', 'cpf', 'cnpj', 'rg', 
        'endereco', 'address', 'birth', 'nasc', 'senha', 'password', 
        'social', 'cliente', 'usuario', 'user', 'razao_social'
    ]
    if any(k in col_lower for k in keywords):
        return True

    # 2. LISTA BRANCA: Colunas que sabemos que NÃO são PII (ganho de performance)
    ignore_keywords = ['id', 'uuid', 'pk', 'fk', 'status', 'created_at', 'updated_at', 'timestamp', 'date']
    # Só ignora se o nome for EXATAMENTE um desses ou terminar com _id
    if col_lower in ignore_keywords or col_lower.endswith('_id'):
        return False

    # 3. ANÁLISE DE CONTEÚDO (Regex rápido na amostra)
    if samples:
        sample_text = " ".join([str(s) for s in samples if s is not None])
        
        # Padrões comuns de PII no Brasil
        patterns = {
            "email": r'[\w\.-]+@[\w\.-]+',
            "cpf": r'\d{3}\.?\d{3}\.?\d{3}-?\d{2}',
            "data": r'\d{2}/\d{2}/\d{4}'
        }
        
        for p in patterns.values():
            if re.search(p, sample_text):
                return True

    # Se não caiu em nenhum critério, assume que não precisa (ou defina como True por segurança)
    return False

def reset_memory():
    global _MAPPING_CACHE, _USED_FAKES
    _MAPPING_CACHE.clear()
    _USED_FAKES.clear()
    _normalize.cache_clear()