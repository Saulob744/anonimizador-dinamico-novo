import re, random, string, unicodedata, hashlib, logging
from faker import Faker

logger = logging.getLogger(__name__)

# ==================================================
# CONFIGURAÇÕES DE IA (ENSEMBLE: GLiNER + SPACY)
# ==================================================
fake = Faker("pt_BR")

# 1. GLiNER (Zero-Shot Contextual NER)
try:
    from gliner import GLiNER
    _gliner_model = None
    GLINER_LABELS = ["person", "first name", "email", "phone number", "address", "organization"]
    GLINER_AVAILABLE = True
except ImportError:
    GLINER_AVAILABLE = False

def get_gliner():
    global _gliner_model
    if GLINER_AVAILABLE and _gliner_model is None:
        logger.info("Carregando modelo GLiNER na memória...")
        _gliner_model = GLiNER.from_pretrained("urchade/gliner_base")
    return _gliner_model

# 2. spaCy (Análise Sintática e Morfológica em Português)
try:
    import spacy
    _spacy_model = None
    _spacy_failed = False  # 🚀 NOVA FLAG: Evita o spam de avisos
    
    def get_spacy():
        global _spacy_model, _spacy_failed
        
        if _spacy_failed:
            return None # Se já falhou na primeira vez, não tenta de novo
            
        if _spacy_model is None:
            try:
                logger.info("Carregando modelo spaCy (pt_core_news_sm)...")
                _spacy_model = spacy.load("pt_core_news_sm")
            except OSError:
                logger.warning("⚠️ Modelo spaCy não encontrado. Desativando módulo silenciosamente. Para usar, rode: python -m spacy download pt_core_news_sm")
                _spacy_failed = True # Marca que falhou para ficar quieto nas próximas linhas
                return None
        return _spacy_model
except ImportError:
    def get_spacy(): return None

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

# 🚀 CAÇADOR DE NOMES SOLTOS E DIMINUTIVOS (Ignora maiúsculas/minúsculas)
_nomes = "maria|joao|joão|ana|jose|josé|carlos|paulo|lucas|marcos|luiz|luis|fernanda|julia|pedro|carol|jorge|antonio|francisco|aline|bruna|camila|rafael|gabriel|rodrigo|thiago|bruno|amanda|jessica|leticia|letícia|diego|marcelo|gustavo|guilherme|felipe|larissa|vitoria|vitória|renato|eduardo|leonardo|victor|vitor|matheus|mateus|enzo|valentina|miguel|arthur|heitor|alice|laura|sophia|davi|lorenzo|theo|bernardo|isaque|isabella|manuela|giovanna|helena|henrique|luiza|mariana|beatriz|roberto|ricardo|fernando|patricia|juliana|marcia|renata|claudia"
COMMON_NAMES = re.compile(rf"\b({_nomes})(z?inh[oa]|it[oa]|ão)?\b", re.IGNORECASE)

# 🚀 ÂNCORAS DE PESSOA (Contexto)
PERSON_ANCHORS = re.compile(r"\b(v[íi]tima|suspeito|autor|indiv[íi]duo|senhor|senhora|sr\.?|sra\.?|testemunha)\s+([A-ZÀ-Üa-zà-ü]+)\b", re.IGNORECASE)

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
        base = _normalize(value).split()[0] if value.split() else "LOC"
        return f"{base}_REGIAO_{random.randint(10, 99)}"

    if typ == "CODE":
        return "".join(random.choices(string.ascii_uppercase + string.digits, k=max(5, len(value))))

    return "".join(random.choices(string.ascii_uppercase + string.digits, k=10))

# ==================================================
# MOTOR PESADO (TEXTO LIVRE)
# ==================================================
def _resolve_type(raw_type: str) -> str:
    t = raw_type.lower()
    if any(k in t for k in ["person", "name", "suspect", "victim", "employee", "per"]): return "PER"
    if "email" in t: return "EMAIL"
    if "phone" in t: return "PHONE"
    if any(k in t for k in ["organization", "org"]): return "ORG"
    if any(k in t for k in ["location", "address", "loc"]): return "LOC"
    return "UNK"

def _detect_all(text: str):
    found = []
    
    # 1. Âncoras de Pessoas (Pega nomes soltos baseado na palavra anterior)
    for m in PERSON_ANCHORS.finditer(text):
        nome_alvo = m.group(2)
        if len(nome_alvo) > 2:
            found.append((m.start(2), m.end(2), nome_alvo, "PER"))

    # 2. Caçador de Diminutivos e Nomes Comuns
    for m in COMMON_NAMES.finditer(text):
        found.append((m.start(), m.end(), m.group(), "PER"))

    # 3. Regex Padrão (Documentos, Códigos)
    for typ, pattern in REGEX.items():
        for m in pattern.finditer(text): found.append((m.start(), m.end(), m.group(), typ))

    # 4. Nomes Compostos (Regex Clássico)
    for m in NAME_REGEX.finditer(text):
        raw = m.group()
        if len(_canonical(raw).split()) >= 2:
            found.append((m.start(), m.end(), raw, "PER"))

    # 5. Inteligência Artificial: spaCy (Gramática)
    spacy_model = get_spacy()
    if spacy_model:
        doc = spacy_model(text)
        for ent in doc.ents:
            if ent.label_ == "PER" or ent.label_ == "LOC":
                found.append((ent.start_char, ent.end_char, ent.text, _resolve_type(ent.label_)))

    # 6. Inteligência Artificial: GLiNER (Contexto Zero-Shot)
    gliner_model = get_gliner()
    if gliner_model and len(text) > 10:
        try:
            preds = gliner_model.predict_entities(text, GLINER_LABELS, threshold=0.35)
            for e in preds:
                found.append((e["start"], e["end"], e["text"], _resolve_type(e["label"])))
        except: pass

    # 🚀 RESOLUÇÃO DE CONFLITOS (A Magia do Ensemble)
    # Ordena por início. Se houver sobreposição, o maior texto "engole" o menor.
    clean_found, last_end = [], -1
    for s, e, v, typ in sorted(found, key=lambda x: (x[0], -(x[1]-x[0]))):
        if s >= last_end:
            clean_found.append((s, e, v, typ))
            last_end = e
            
    return clean_found

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

def reset_memory():
    fake.seed_instance(42)

# ==================================================
# ROTEADOR DINÂMICO POR PERFIL DO DADO (DATA PROFILING)
# ==================================================
def anonymize_value(col_name: str, val, anon_location: bool = True):
    # --------------------------------------------------
    # CAMADA 1: TIPOS NATIVOS DO BANCO
    # --------------------------------------------------
    if val is None or isinstance(val, (int, float, bool)) or type(val).__name__ in ['date', 'datetime', 'Timestamp']:
        return val, None

    val_str = str(val).strip()
    length = len(val_str)
    if length == 0: return val_str, None

    # --------------------------------------------------
    # CAMADA 2: PADRÕES EXATOS E BLINDAGEM DE BD
    # --------------------------------------------------
    if UUID_PATTERN.match(val_str):
        return _get_fake(val_str, "UUID"), "UUID"

    if PURE_COORD_PATTERN.match(val_str):
        return (alter_geo_precision(val_str, 3), "COORD") if anon_location else (val_str, None)

    # --------------------------------------------------
    # CAMADA 3: GEOMETRIA DO DADO (Inteligência Central)
    # --------------------------------------------------
    words = val_str.split()
    word_count = len(words)

    # A) TEXTOS LONGOS (Observações e Históricos)
    # Tem mais de 50 letras ou 7 palavras? Manda para as IAs lerem a frase!
    if length > 50 or word_count > 7:
        new_val = anonymize_text(val_str)
        return new_val, ("TEXT" if new_val != val_str else None)

    # B) CÓDIGOS E IDENTIFICADORES (Chassis, B.O.s, RGs com letras)
    # Poucas palavras, mas possui números no meio das letras.
    if word_count <= 2 and any(char.isdigit() for char in val_str):
        new_val = anonymize_text(val_str)
        return new_val, ("TEXT" if new_val != val_str else None)

    # C) STRINGS CURTAS E CATEGÓRICAS (Usa a coluna para desempatar)
    c = col_name.lower()

    # C.1 - Desempate Categorial Seguros (Não anonimiza)
    if any(k in c for k in ["crime", "tipo", "status", "situacao", "estado", "cidade", "natureza", "sexo", "genero", "profissao", "cor", "marca", "modelo"]):
        return val_str, None

    # C.2 - Desempate Estrutural (Troca tudo)
    if any(k in c for k in ["nome", "vitima", "suspeito", "autor", "pessoa", "testemunha"]):
        return _get_fake(val_str, "PER"), "PER"
        
    if any(k in c for k in ["endereco", "rua", "logradouro", "bairro", "local"]):
        return _get_fake(val_str, "LOC"), "LOC"
        
    if any(k in c for k in ["email", "mail"]):
        return _get_fake(val_str, "EMAIL"), "EMAIL"

    # C.3 - Rede de Segurança Final
    # Caiu aqui? É uma string curta (ex: "Faca de cozinha", "Maconha", "Cleber"). 
    # Por segurança, mandamos para o motor ler a palavra.
    new_val = anonymize_text(val_str)
    return new_val, ("TEXT" if new_val != val_str else None)