import re
import random
import string
import unicodedata
from faker import Faker
from gliner import GLiNER

# ==================================================
# CONFIG
# ==================================================
fake = Faker("pt_BR")

_gliner_model = None
_memory = {}

def get_gliner():
    global _gliner_model
    if _gliner_model is None:
        _gliner_model = GLiNER.from_pretrained("urchade/gliner_base")
    return _gliner_model


GLINER_LABELS = [
    "person",
    "email",
    "phone number",
    "address",
    "organization",
    "location"
]

# ==================================================
# REGEX
# ==================================================
REGEX = {
    "CPF": re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "CEP": re.compile(r"\b\d{5}-?\d{3}\b"),
}

# ==================================================
# NORMALIZAÇÃO + MEMÓRIA
# ==================================================
def _normalize(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"\b([A-Z])\.", r"\1", text)  # remove iniciais tipo D.
    return text.upper().strip()

def _fingerprint(name):
    parts = _normalize(name).split()

    # remove partículas irrelevantes dinamicamente
    parts = [p for p in parts if len(p) > 1]

    # cria assinatura estrutural
    return " ".join(parts)


def _get(val, cat, generator):
    key = _fingerprint(val) + cat

    if cat not in _memory:
        _memory[cat] = {}

    if key not in _memory[cat]:
        _memory[cat][key] = generator()

    return _memory[cat][key]

# ==================================================
# MERGE SEGURO
# ==================================================
def _resolve(items):
    items = sorted(items, key=lambda x: (x[0], -(x[1] - x[0])))

    out = []

    for s, e, v, t in items:
        if any(not (e <= rs or s >= re) for rs, re, _, _ in out):
            continue
        out.append((s, e, v, t))

    return sorted(out, key=lambda x: x[0])

# ==================================================
# GLINER (ROBUSTO)
# ==================================================
def _detect_gliner(text):
    found = []

    try:
        model = get_gliner()

        preds = model.predict_entities(text, GLINER_LABELS, threshold=0.30)
        preds += model.predict_entities(text.lower(), GLINER_LABELS, threshold=0.30)

        for ent in preds:
            label = ent["label"].lower()
            value = ent["text"].strip()

            if len(value) < 3:
                continue

            if "person" in label:
                typ = "PER"
            elif "email" in label:
                typ = "EMAIL"
            elif "phone" in label:
                typ = "PHONE"
            elif "address" in label:
                typ = "ADDRESS"
            elif "organization" in label:
                typ = "ORG"
            elif "location" in label:
                typ = "LOC"
            else:
                continue

            found.append((ent["start"], ent["end"], value, typ))

    except Exception:
        pass

    return found

# ==================================================
# HEURÍSTICA DE NOMES (REFINADA)
# ==================================================
STOPWORDS = {
    "CONDUTOR", "MOTORISTA", "RELATO", "REGISTRO", "OCORRENCIA",
    "CASO", "ABORDAGEM", "DENUNCIA", "VEICULO", "CARRO",
    "RUA", "CEP", "TELEFONE", "EMAIL", "NUMERO"
}


def _name_score(chunk):
    words = chunk.split()

    if len(words) < 2:
        return 0

    capital_ratio = sum(w[0].isupper() for w in words if w)
    score = capital_ratio / len(words)

    if len(words) > 5:
        score -= 0.5

    if any(len(w) < 2 for w in words):
        score -= 0.3

    return score


def _is_name(words):
    if len(words) < 2:
        return False

    if any(w.upper() in STOPWORDS for w in words):
        return False

    if any(any(c.isdigit() for c in w) for w in words):
        return False

    joined = " ".join(words)

    if len(joined) < 6:
        return False

    # evita palavras genéricas repetidas
    if words[0].upper() == words[-1].upper():
        return False

    return True


def _detect_names(text):
    found = []

    pattern = re.compile(
        r"\b([A-Za-zÀ-ÿ]{3,}(?:\s+[A-Za-zÀ-ÿ]{2,}){1,4})\b"
    )

    for m in pattern.finditer(text):
        chunk = m.group().strip()

        score = _name_score(chunk)

        if score < 0.65:
            continue

        found.append((m.start(), m.end(), chunk, "PER"))

    return found

# ==================================================
# DETECTOR FINAL (INTELIGENTE)
# ==================================================
def _detect_all(text):
    found = []

    # REGEX (prioridade máxima)
    for typ, pattern in REGEX.items():
        for m in pattern.finditer(text):
            found.append((m.start(), m.end(), m.group(), typ))

    # GLINER
    found.extend(_detect_gliner(text))

    # fallback só se necessário
    per_count = sum(1 for x in found if x[3] == "PER")

    if per_count < 2:
        found.extend(_detect_names(text))

    return _resolve(found)

# ==================================================
# ANONIMIZAÇÃO
# ==================================================
def anonymize_text(text):
    if not isinstance(text, str) or not text:
        return text

    entities = _detect_all(text)
    entities = sorted(entities, key=lambda x: x[0])

    result = []
    last = 0

    for s, e, v, typ in entities:

        if s < last:
            continue

        result.append(text[last:s])

        if typ == "PER":
            repl = _get(v, "PER", lambda: fake.name().upper())

        elif typ == "CPF":
            repl = _get(v, "CPF", fake.cpf)

        elif typ == "EMAIL":
            repl = _get(v, "EMAIL", fake.email)

        elif typ == "PHONE":
            repl = _get(v, "PHONE", fake.phone_number)

        elif typ == "PLATE":
            repl = _get(v, "PLATE", lambda: fake.license_plate().upper())

        elif typ == "CEP":
            repl = _get(v, "CEP", lambda: fake.postcode())

        elif typ in ("ADDRESS", "LOC"):
            repl = _get(v, "LOC", fake.address)

        elif typ == "ORG":
            repl = _get(v, "ORG", fake.company)

        else:
            repl = _get(
                v,
                typ,
                lambda: "".join(
                    random.choice(string.ascii_uppercase + string.digits)
                    for _ in range(len(v))
                )
            )

        result.append(repl)
        last = e

    result.append(text[last:])
    return "".join(result)

# ==================================================
# API BANCO
# ==================================================
def anonymize_value(col, val):
    if val is None:
        return val, None

    if isinstance(val, (int, float)) or col.lower().endswith("_id"):
        return val, None

    val_str = str(val)
    new_val = anonymize_text(val_str)

    return new_val, ("TEXT" if new_val != val_str else None)