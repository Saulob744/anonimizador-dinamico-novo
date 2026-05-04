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
GLINER_MIN_TEXT = 80
DEBUG_MODE = True

GLINER_LABELS = [
    "person",
    "email",
    "phone number",
    "address",
    "organization"
]

# =========================================================
# REGEX PRINCIPAIS
# =========================================================
REGEX = {
    "CPF": re.compile(r"\b(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(\d{2}\)|\d{2})\s?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "LOC": re.compile(
        r"\b(Rua|Av|Avenida|Alameda|Travessa|Pca|Praca)\s+([A-ZÀ-Ü0-9][^\s,]+(\s+[A-ZÀ-Ü0-9][^\s,]+){0,4})\b",
        re.IGNORECASE,
    ),
    "COORD": re.compile(
        r"^\s*-?(?:90(?:\.0+)?|[0-8]?\d(?:\.\d+)?)\s*,\s*-?(?:180(?:\.0+)?|1[0-7]\d(?:\.\d+)?|\d{1,2}(?:\.\d+)?)\s*$"
    ),
    "LAT": re.compile(r"^\s*-?(?:90(?:\.0+)?|[0-8]?\d(?:\.\d+)?)\s*$"),
    "LONG": re.compile(
        r"^\s*-?(?:180(?:\.0+)?|1[0-7]\d(?:\.\d+)?|\d{1,2}(?:\.\d+)?)\s*$"
    ),
    "CODE": re.compile(r"\b(?=[A-Za-z-]*\d)(?=[0-9-]*[A-Za-z])[A-Za-z0-9-]{5,}\b"),
    "PER_COMMON": re.compile(
        r"\b(maria|jos[eé]|jo[aã]o|ana|carlos|paulo|pedro|lucas|luiz|gabriel|rafael|fernando|roberto|mariana|patricia|camila|amanda|bruna|julia|marcos|diego|ricardo|gustavo)\b",
        re.IGNORECASE,
    ),
}

NAME_REGEX = re.compile(
    r"\b([A-ZÀ-Ü][a-zA-ZÀ-Üà-ü]{1,}(?:\s+(?:d[eao]s?|D[EAO]S?|[A-ZÀ-Ü][a-zA-ZÀ-Üà-ü]{1,}\.?)){1,3})\b"
)

UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"
)

DOC_PATTERN = re.compile(r"\b(cpf|rg|documentos?|cnpj|cnh|passaporte)\b")
NAME_COL_PATTERN = re.compile(r"\b(nome|nomes|razao social)\b")

# =========================================================
# HEURÍSTICAS DE SENSIBILIDADE
# =========================================================
SENSITIVE_HINTS = re.compile(
    r"\b(nome|mae|pai|filiacao|suspeito|autor|vitima|indiciado|cpf|rg|telefone|email|endereco|usuario|funcionario|servidor|pessoa)\b",
    re.IGNORECASE,
)

NON_SENSITIVE_HINTS = re.compile(
    r"\b(natureza|crime|tipo|status|descricao|historico|categoria|municipio|cidade|bairro|marca|modelo|cor|orgao|setor|departamento|processo|protocolo|codigo|id)\b",
    re.IGNORECASE,
)

CITY_LIKE_PATTERN = re.compile(
    r"^[A-ZÀ-Ü][a-zà-ü]+(?:\s[A-ZÀ-Ü][a-zà-ü]+)?$"
)

# =========================================================
# DEBUG
# =========================================================
def debug_log(msg):
    if DEBUG_MODE:
        logger.warning(msg)

# =========================================================
# NORMALIZAÇÃO
# =========================================================
@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text:
        return ""

    text = "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )

    return re.sub(r"[^\w\s]", "", text.upper().strip())


def _fingerprint(value: str) -> str:
    parts = sorted(
        p for p in _normalize(value).split()
        if p not in {"DA", "DE", "DO", "DAS", "DOS"}
    )

    return hashlib.sha256(" ".join(parts).encode()).hexdigest()

# =========================================================
# GERAÇÃO DE FAKES
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

    while True:
        fake.seed_instance(seed + attempts)
        random.seed(seed + attempts)

        try:
            if typ == "UUID":
                val = str(fake.uuid4())
            elif typ in {"PER", "NAME"}:
                val = fake.name().upper()
            elif typ == "PER_COMMON":
                val = fake.first_name().upper()
            elif typ == "CPF":
                val = fake.cpf()
            elif typ == "EMAIL":
                val = fake.email()
            elif typ == "PLATE":
                val = fake.license_plate().upper()
            elif typ == "LOC":
                prefix = value.split()[0].upper() if " " in value else "RUA"
                val = f"{prefix} {fake.name().upper()}"
            elif typ == "COORD":
                lat, lon = map(float, value.split(","))
                val = f"{lat + random.uniform(-0.003, 0.003):.4f}, {lon + random.uniform(-0.003, 0.003):.4f}"
            elif typ in {"LAT", "LONG"}:
                val = f"{float(value) + random.uniform(-0.003, 0.003):.4f}"
            else:
                val = "".join(
                    random.choices(
                        string.ascii_uppercase + string.digits,
                        k=max(5, len(value))
                    )
                )
        except Exception:
            val = value

        if typ in {"CPF", "UUID", "PER", "NAME"} and val in _USED_FAKES:
            attempts += 1
            if attempts > 50:
                break
            continue

        if typ in {"CPF", "UUID", "PER", "NAME"}:
            _USED_FAKES.add(val)

        _MAPPING_CACHE[cache_key] = val
        return val

# =========================================================
# GLINER
# =========================================================
def get_gliner():
    global _gliner_model

    if _gliner_model is None:
        _gliner_model = GLiNER.from_pretrained("urchade/gliner_base")

    return _gliner_model

# =========================================================
# SCORE DE SENSIBILIDADE
# =========================================================
def score_column_sensitivity(col_name: str, sample_values) -> dict:
    """Score robusto para decidir se uma coluna merece anonimização estrutural."""
    score = 0
    reasons = []

    col_clean = str(col_name).lower().replace("_", " ").replace("-", " ")

    # =====================================================
    # CAMADA 1 — NOME DA COLUNA
    # =====================================================
    if SENSITIVE_HINTS.search(col_clean):
        score += 40
        reasons.append("sensitive_column_name")

    if NON_SENSITIVE_HINTS.search(col_clean):
        score -= 50
        reasons.append("non_sensitive_column_name")

    # =====================================================
    # AMOSTRAS
    # =====================================================
    valid_samples = [
        str(v).strip()
        for v in sample_values
        if v is not None and str(v).strip()
    ][:50]

    if not valid_samples:
        return {
            "score": score,
            "decision": False,
            "reasons": reasons,
        }

    total = len(valid_samples)

    # =====================================================
    # MÉTRICAS
    # =====================================================
    person_hits = 0
    structured_hits = 0
    city_hits = 0
    categorical_hits = 0
    short_text_hits = 0
    long_text_hits = 0
    unique_values = set()

    for val in valid_samples:
        unique_values.add(val.lower())

        word_count = len(val.split())

        # -------------------------
        # Texto curto / categórico
        # -------------------------
        if len(val) < 15:
            short_text_hits += 1

        if len(val) > 80:
            long_text_hits += 1

        # -------------------------
        # Estrutura categórica
        # -------------------------
        if word_count <= 2 and val.istitle():
            categorical_hits += 1

        # -------------------------
        # Regex estruturado
        # -------------------------
        for typ, pat in REGEX.items():
            if pat.search(val):
                if typ in {"CPF", "EMAIL", "PHONE", "LOC"}:
                    structured_hits += 1
                break

        # -------------------------
        # Nome detectado
        # -------------------------
        if NAME_REGEX.search(val):
            person_hits += 1

        if REGEX["PER_COMMON"].search(val):
            person_hits += 1

        # -------------------------
        # Cidade/localidade
        # -------------------------
        if CITY_LIKE_PATTERN.match(val) and word_count <= 2:
            city_hits += 1

        # -------------------------
        # IA apenas em textos maiores
        # -------------------------
        if len(val) >= GLINER_MIN_TEXT:
            try:
                preds = get_gliner().predict_entities(
                    val,
                    ["person"],
                    threshold=0.40,
                )
                if preds:
                    person_hits += 2
            except Exception:
                pass

    # =====================================================
    # RATIOS
    # =====================================================
    person_ratio = person_hits / total
    structured_ratio = structured_hits / total
    city_ratio = city_hits / total
    categorical_ratio = categorical_hits / total
    short_ratio = short_text_hits / total
    long_ratio = long_text_hits / total
    unique_ratio = len(unique_values) / total

    avg_words = sum(len(v.split()) for v in valid_samples) / total

    # =====================================================
    # CAMADA 2 — PONTUAÇÃO POSITIVA
    # =====================================================
    score += int(person_ratio * 55)
    score += int(structured_ratio * 40)

    if long_ratio > 0.30:
        score += 15
        reasons.append("long_text_pattern")

    # =====================================================
    # CAMADA 3 — PENALIDADES
    # =====================================================

    # Coluna majoritariamente cidade/local
    if city_ratio >= 0.50:
        score -= 60
        reasons.append("city_like_pattern")

    # Coluna categórica
    if categorical_ratio >= 0.60:
        score -= 35
        reasons.append("categorical_pattern")

    # Texto curto demais
    if short_ratio >= 0.70:
        score -= 25
        reasons.append("short_text_pattern")

    # Pouca diversidade
    if unique_ratio < 0.30:
        score -= 20
        reasons.append("low_uniqueness")

    # Média de palavras muito baixa
    if avg_words < 2:
        score -= 20
        reasons.append("low_nominal_pattern")

    # Se parece operacional e não pessoal
    if (
        city_ratio > 0.40
        and person_ratio < 0.30
        and structured_ratio < 0.20
    ):
        score -= 50
        reasons.append("operational_column_pattern")

    # =====================================================
    # CAMADA 4 — REGRAS DE BLOQUEIO
    # =====================================================

    force_block = False

    if NON_SENSITIVE_HINTS.search(col_clean):
        force_block = True
        reasons.append("forced_non_sensitive_block")

    if city_ratio >= 0.70:
        force_block = True
        reasons.append("forced_city_block")

    if categorical_ratio >= 0.75 and person_ratio < 0.25:
        force_block = True
        reasons.append("forced_categorical_block")

    # =====================================================
    # DECISÃO FINAL
    # =====================================================
    decision = False if force_block else score >= 35

    # =====================================================
    # DEBUG
    # =====================================================
    debug_log(
        f"[COLUMN SCORE] {col_name} | "
        f"Score={score} | "
        f"Decision={decision} | "
        f"Person={person_ratio:.2f} | "
        f"Structured={structured_ratio:.2f} | "
        f"City={city_ratio:.2f} | "
        f"Categorical={categorical_ratio:.2f} | "
        f"Unique={unique_ratio:.2f} | "
        f"AvgWords={avg_words:.2f} | "
        f"Reasons={reasons} | "
        f"Samples={valid_samples[:10]}"
    )

    return {
        "score": score,
        "decision": decision,
        "reasons": reasons,
        "person_ratio": round(person_ratio, 2),
        "structured_ratio": round(structured_ratio, 2),
        "city_ratio": round(city_ratio, 2),
        "categorical_ratio": round(categorical_ratio, 2),
        "unique_ratio": round(unique_ratio, 2),
        "avg_words": round(avg_words, 2),
    }

def should_anonymize_column(col_name: str, sample_values) -> bool:
    try:
        return score_column_sensitivity(col_name, sample_values)["decision"]
    except Exception as e:
        debug_log(f"[ERROR SCORE] {col_name}: {e}")
        return True

# =========================================================
# DETECÇÃO
# =========================================================
def _detect_all(text: str, anon_loc: bool):
    found = []

    for typ, pat in REGEX.items():
        if typ in {"COORD", "LOC"} and not anon_loc:
            continue

        for match in pat.finditer(text):
            found.append((match.start(), match.end(), match.group(), typ))

    for match in NAME_REGEX.finditer(text):
        found.append((match.start(), match.end(), match.group(), "PER"))

    if len(text) >= GLINER_MIN_TEXT:
        try:
            preds = get_gliner().predict_entities(
                text,
                GLINER_LABELS,
                threshold=0.30,
            )

            for entity in preds:
                lbl = entity["label"].lower()

                typ = (
                    "PER" if "person" in lbl else
                    "LOC" if "address" in lbl else
                    "ORG"
                )

                if typ == "LOC" and not anon_loc:
                    continue

                found.append(
                    (entity["start"], entity["end"], entity["text"], typ)
                )

        except Exception:
            pass

    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))

    clean = []
    last = -1

    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, v, t))
            last = e

    return clean

# =========================================================
# TEXTO
# =========================================================
def anonymize_text(text: str, anon_loc: bool = True) -> str:
    if not isinstance(text, str):
        return text

    text = text.strip()

    if len(text) < 3:
        return text

    entities = _detect_all(text, anon_loc)

    if not entities:
        return text

    result = []
    last = 0

    for s, e, v, t in entities:
        result.extend([text[last:s], _get_fake(v, t)])
        last = e

    result.append(text[last:])

    return "".join(result)

# =========================================================
# VALOR UNITÁRIO
# =========================================================
def anonymize_value(col_name: str, val, anon_location: bool = True):
    if (
        val is None
        or isinstance(val, (int, float, bool))
        or type(val).__name__ in {"date", "datetime", "Timestamp"}
    ):
        return val, None

    v_str = str(val).strip()
    col_clean = col_name.lower().replace("_", " ").replace("-", " ")

    if DOC_PATTERN.search(col_clean):
        return _get_fake(v_str, "CPF"), "TEXT"

    if NAME_COL_PATTERN.search(col_clean):
        return _get_fake(v_str, "PER"), "TEXT"

    if UUID_PATTERN.match(v_str):
        return _get_fake(v_str, "UUID"), "UUID"

    new_v = anonymize_text(v_str, anon_location)

    return new_v, ("TEXT" if new_v != v_str else None)

# =========================================================
# RESET
# =========================================================
def reset_memory():
    global _MAPPING_CACHE, _USED_FAKES
    _MAPPING_CACHE.clear()
    _USED_FAKES.clear()
    _normalize.cache_clear()

# =========================================================
# DATAFRAME
# =========================================================
# =========================================================
# SCORE DE SENSIBILIDADE DE COLUNA (VERSÃO REFORÇADA)
# =========================================================
def score_column_sensitivity(col_name: str, sample_values) -> dict:
    """
    Decide se uma coluna realmente merece anonimização,
    reduzindo falsos positivos como:
    - cidades
    - bairros
    - naturezas criminais
    - categorias repetitivas
    - descrições curtas não pessoais
    """

    score = 0
    reasons = []

    col_clean = str(col_name).lower().replace("_", " ").replace("-", " ")

    # -----------------------------------------------------
    # PESO PELO NOME DA COLUNA
    # -----------------------------------------------------
    if SENSITIVE_HINTS.search(col_clean):
        score += 40
        reasons.append("sensitive_column_name")

    if NON_SENSITIVE_HINTS.search(col_clean):
        score -= 50
        reasons.append("non_sensitive_column_name")

    # -----------------------------------------------------
    # AMOSTRAS VÁLIDAS
    # -----------------------------------------------------
    valid_samples = [
        str(v).strip()
        for v in sample_values
        if v is not None and str(v).strip()
    ][:100]

    if not valid_samples:
        debug_log(f"[COLUMN SCORE] {col_name} | EMPTY SAMPLE")
        return {
            "score": score,
            "decision": False,
            "reasons": reasons,
        }

    total = len(valid_samples)

    # -----------------------------------------------------
    # MÉTRICAS
    # -----------------------------------------------------
    person_hits = 0
    structured_hits = 0
    city_hits = 0
    repetitive_hits = 0
    short_text_hits = 0
    categorical_hits = 0

    normalized_values = [_normalize(v) for v in valid_samples]
    unique_ratio = len(set(normalized_values)) / total

    for val in valid_samples:
        val_clean = val.strip()

        # -----------------------------
        # REGEX estruturado
        # -----------------------------
        for typ, pat in REGEX.items():
            if pat.search(val_clean):
                if typ in {"CPF", "EMAIL", "PHONE", "LOC"}:
                    structured_hits += 1
                break

        # -----------------------------
        # Nome provável
        # -----------------------------
        if NAME_REGEX.search(val_clean):
            person_hits += 1

        if REGEX["PER_COMMON"].search(val_clean):
            person_hits += 1

        # -----------------------------
        # IA GLINER (somente textos médios)
        # -----------------------------
        if 15 <= len(val_clean) <= 120:
            try:
                preds = get_gliner().predict_entities(
                    val_clean,
                    ["person"],
                    threshold=0.45,
                )

                if preds:
                    person_hits += 3

            except Exception:
                pass

        # -----------------------------
        # Cidade / localidade provável
        # -----------------------------
        if (
            CITY_LIKE_PATTERN.match(val_clean)
            and len(val_clean.split()) <= 3
            and not REGEX["PER_COMMON"].search(val_clean)
        ):
            city_hits += 1

        # -----------------------------
        # Texto curto demais
        # -----------------------------
        if len(val_clean.split()) <= 2:
            short_text_hits += 1

        # -----------------------------
        # Categoria típica
        # -----------------------------
        if len(val_clean) < 30 and not any(ch.isdigit() for ch in val_clean):
            categorical_hits += 1

    # -----------------------------------------------------
    # RATIOS
    # -----------------------------------------------------
    person_ratio = person_hits / total
    structured_ratio = structured_hits / total
    city_ratio = city_hits / total
    short_ratio = short_text_hits / total
    categorical_ratio = categorical_hits / total

    # -----------------------------------------------------
    # SCORE POSITIVO
    # -----------------------------------------------------
    score += int(person_ratio * 60)
    score += int(structured_ratio * 35)

    # -----------------------------------------------------
    # SCORE NEGATIVO
    # -----------------------------------------------------
    if city_ratio > 0.40:
        score -= 45
        reasons.append("city_like_pattern")

    if short_ratio > 0.75:
        score -= 25
        reasons.append("mostly_short_values")

    if categorical_ratio > 0.70:
        score -= 20
        reasons.append("categorical_values")

    if unique_ratio < 0.35:
        score -= 20
        reasons.append("high_repetition")

    # -----------------------------------------------------
    # PROTEÇÃO EXTRA:
    # se nome da coluna já sugere natureza/tipo,
    # exigir score muito maior
    # -----------------------------------------------------
    threshold = 35

    if NON_SENSITIVE_HINTS.search(col_clean):
        threshold = 55

    # -----------------------------------------------------
    # DECISÃO FINAL
    # -----------------------------------------------------
    decision = score >= threshold

    # -----------------------------------------------------
    # DEBUG COMPLETO
    # -----------------------------------------------------
    debug_log(
        f"[COLUMN SCORE] "
        f"Column='{col_name}' | "
        f"Score={score} | Threshold={threshold} | Decision={decision} | "
        f"PersonRatio={person_ratio:.2f} | "
        f"StructuredRatio={structured_ratio:.2f} | "
        f"CityRatio={city_ratio:.2f} | "
        f"ShortRatio={short_ratio:.2f} | "
        f"UniqueRatio={unique_ratio:.2f} | "
        f"Reasons={reasons} | "
        f"Samples={valid_samples[:10]}"
    )

    return {
        "score": score,
        "decision": decision,
        "reasons": reasons,
        "person_ratio": round(person_ratio, 2),
        "structured_ratio": round(structured_ratio, 2),
        "city_ratio": round(city_ratio, 2),
        "short_ratio": round(short_ratio, 2),
        "unique_ratio": round(unique_ratio, 2),
        "threshold": threshold,
    }