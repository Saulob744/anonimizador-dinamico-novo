import re, random, string, unicodedata, hashlib, logging
from functools import lru_cache
from faker import Faker
from gliner import GLiNER

logger = logging.getLogger(__name__)

_MAPPING_CACHE, _USED_FAKES = {}, set()
fake = Faker("pt_BR")
_gliner_model = None

CACHE_LIMIT = 500000
GLINER_MIN_TEXT = 80

GLINER_LABELS = ["person", "email", "phone number", "address", "organization"]

# Regex principais
REGEX = {
    "CPF": re.compile(r"\b(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(\d{2}\)|\d{2})\s?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "LOC": re.compile(
        r"\b(Rua|Av|Avenida|Alameda|Travessa|Pca|Praca)\s+([A-ZÀ-Ü0-9][^\s,]+(\s+[A-ZÀ-Ü0-9][^\s,]+){0,4})\b",
        re.IGNORECASE
    ),
    "COORD": re.compile(
        r"^\s*-?(?:90(?:\.0+)?|[0-8]?\d(?:\.\d+)?)\s*,\s*-?(?:180(?:\.0+)?|1[0-7]\d(?:\.\d+)?|\d{1,2}(?:\.\d+)?)\s*$"
    ),
    "LAT": re.compile(
        r"^\s*-?(?:90(?:\.0+)?|[0-8]?\d(?:\.\d+)?)\s*$"
    ),
    "LONG": re.compile(
        r"^\s*-?(?:180(?:\.0+)?|1[0-7]\d(?:\.\d+)?|\d{1,2}(?:\.\d+)?)\s*$"
    ),
    "CODE": re.compile(r"\b(?=[A-Za-z-]*\d)(?=[0-9-]*[A-Za-z])[A-Za-z0-9-]{5,}\b"),
    "PER_COMMON": re.compile(
        r"\b(maria|jos[eé]|jo[aã]o|ant[oô]nio|francisco|ana|carlos|paulo|pedro|lucas|luiz|marcos|lu[ií]s|gabriel|rafael|daniel|marcelo|bruno|eduardo|felipe|raimundo|rodrigo|manoel|mateus|andr[eé]|fernando|f[aá]bio|leonardo|gustavo|guilherme|leandro|tiago|thiago|[aâ]nderson|ricardo|m[aá]rcio|jorge|alexandre|roberto|edson|diego|v[ií]tor|francisca|ant[oô]nia|adriana|juliana|m[aá]rcia|fernanda|patr[ií]cia|aline|sandra|camila|amanda|bruna|j[eé]ssica|let[ií]cia|j[uú]lia|luciana|vanessa|mariana|gabriela|vera|vit[oó]ria|larissa|cl[aá]udia|beatriz|rita|luana|s[oô]nia|renata|eliane|josefa|simone|nat[aá]lia|michele|tatiane|s[ií]lvia|f[aá]tima|terezinha|margarida)\b",
        re.IGNORECASE
    )
}

NAME_REGEX = re.compile(
    r"\b([A-ZÀ-Ü][a-zA-ZÀ-Üà-ü]{1,}(?:\s+(?:d[eao]s?|D[EAO]S?|[A-ZÀ-Ü][a-zA-ZÀ-Üà-ü]{1,}\.?)){1,3})\b"
)

UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"
)

DOC_PATTERN = re.compile(r"\b(cpf|rg|documentos?|cnpj|cnh|passaporte)\b")
NAME_COL_PATTERN = re.compile(r"\b(nome|nomes|razao social)\b")


# Normalização
@lru_cache(maxsize=100000)
def _normalize(t: str) -> str:
    if not t:
        return ""
    t = "".join(
        c for c in unicodedata.normalize("NFKD", t)
        if not unicodedata.combining(c)
    )
    return re.sub(r"[^\w\s]", "", t.upper().strip())


def _fingerprint(v: str) -> str:
    parts = sorted(
        p for p in _normalize(v).split()
        if p not in {"DA", "DE", "DO", "DAS", "DOS"}
    )
    return hashlib.sha256(" ".join(parts).encode()).hexdigest()


# Geração fake
def _get_fake(value: str, typ: str) -> str:
    global _MAPPING_CACHE

    if len(_MAPPING_CACHE) > CACHE_LIMIT:
        _MAPPING_CACHE.clear()

    ckey = f"{typ}:{_normalize(value)}"
    if ckey in _MAPPING_CACHE:
        return _MAPPING_CACHE[ckey]

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
                val = (
                    f"{lat + random.uniform(-0.003, 0.003):.4f}, "
                    f"{lon + random.uniform(-0.003, 0.003):.4f}"
                )

            elif typ in {"LAT", "LONG"}:
                val = f"{float(value) + random.uniform(-0.003, 0.003):.4f}"

            else:
                val = "".join(
                    random.choices(
                        string.ascii_uppercase + string.digits,
                        k=max(5, len(value))
                    )
                )

        except:
            val = value

        if typ in {"CPF", "UUID", "PER", "NAME"} and val in _USED_FAKES:
            attempts += 1
            if attempts > 50:
                break
            continue

        if typ in {"CPF", "UUID", "PER", "NAME"}:
            _USED_FAKES.add(val)

        _MAPPING_CACHE[ckey] = val
        return val


# Modelo IA
def get_gliner():
    global _gliner_model
    if _gliner_model is None:
        _gliner_model = GLiNER.from_pretrained("urchade/gliner_base")
    return _gliner_model


# Detecção
def _detect_all(text: str, anon_loc: bool):
    found = []

    for typ, pat in REGEX.items():
        if typ in {"COORD", "LOC"} and not anon_loc:
            continue

        for m in pat.finditer(text):
            found.append((m.start(), m.end(), m.group(), typ))

    for m in NAME_REGEX.finditer(text):
        found.append((m.start(), m.end(), m.group(), "PER"))

    # IA apenas em textos relevantes
    if len(text) >= GLINER_MIN_TEXT:
        try:
            preds = get_gliner().predict_entities(
                text,
                GLINER_LABELS,
                threshold=0.30
            )

            for e in preds:
                lbl = e["label"].lower()

                typ = (
                    "PER" if "person" in lbl else
                    "LOC" if "address" in lbl else
                    "ORG"
                )

                if typ == "LOC" and not anon_loc:
                    continue

                found.append(
                    (e["start"], e["end"], e["text"], typ)
                )

        except:
            pass

    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))

    clean = []
    last = -1

    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, v, t))
            last = e

    return clean


# Texto livre
def anonymize_text(text: str, anon_loc: bool = True) -> str:
    if not isinstance(text, str):
        return text

    text = text.strip()

    if len(text) < 3:
        return text

    entities = _detect_all(text, anon_loc)

    if not entities:
        return text

    res = []
    last = 0

    for s, e, v, t in entities:
        res.extend([text[last:s], _get_fake(v, t)])
        last = e

    res.append(text[last:])

    return "".join(res)


# Valor individual
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


# Reset
def reset_memory():
    global _MAPPING_CACHE, _USED_FAKES
    _MAPPING_CACHE.clear()
    _USED_FAKES.clear()
    _normalize.cache_clear()


# Inferência de coluna
def infer_column_type(sample_values) -> str:
    clean_sample = [
        str(v).strip()
        for v in sample_values
        if v is not None and str(v).strip()
    ]

    if not clean_sample:
        return "TEXT"

    total = len(clean_sample)
    counts = {key: 0 for key in REGEX.keys()}

    for val in clean_sample:
        for typ, pat in REGEX.items():
            if pat.search(val):
                counts[typ] += 1
                break

    for typ, count in counts.items():
        if (count / total) > 0.70:
            return typ

    return "TEXT"


# Coluna DataFrame
def anonymize_dataframe_column(df, col_name: str, anon_location: bool = True):
    import pandas as pd

    col = df[col_name]

    if (
        pd.api.types.is_datetime64_any_dtype(col)
        or pd.api.types.is_bool_dtype(col)
    ):
        return col

    serie_valida = col.dropna()

    if serie_valida.empty:
        return col

    serie_str = serie_valida.astype(str).str.strip()

    if serie_str.str.len().mean() < 3:
        return col

    col_clean = str(col_name).lower().replace("_", " ").replace("-", " ")

    if DOC_PATTERN.search(col_clean):
        tipo_inferred = "CPF"

    elif NAME_COL_PATTERN.search(col_clean):
        tipo_inferred = "PER"

    else:
        tipo_inferred = infer_column_type(
            serie_str.head(100).tolist()
        )

    if tipo_inferred in {
        "CPF", "CNPJ", "PER",
        "EMAIL", "PHONE",
        "PLATE", "LAT",
        "LONG", "COORD"
    }:
        return col.apply(
            lambda x: (
                _get_fake(str(x).strip(), tipo_inferred)
                if pd.notnull(x) else x
            )
        )

    return col.apply(
        lambda x: (
            anonymize_text(str(x), anon_location)
            if pd.notnull(x) else x
        )
    )