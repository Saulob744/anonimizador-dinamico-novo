import re
import random
import string
import unicodedata
import hashlib
import logging
import requests
import html
import os
from functools import lru_cache
from faker import Faker

try:
    import spacy
    nlp = spacy.load("pt_core_news_lg")
except OSError:
    logging.error("Modelo SpaCy não encontrado! Rode: python -m spacy download pt_core_news_lg")
    nlp = None

# =========================================================
# CONFIGURAÇÕES GLOBAIS E INICIALIZAÇÃO
# =========================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_MAPPING_CACHE = {}
fake = Faker("pt_BR")

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "llama3"

# =========================================================
# REGEX ESTRUTURAIS 
# =========================================================
REGEX = {
    "COORD": re.compile(r"-?\d{1,2}\.\d+,\s*-?\d{1,3}\.\d+"),
    "COORD_SINGLE": re.compile(r"^-?\d{1,3}\.\d{4,}$|^-\d{5,10}$"), 
    "EMAIL": re.compile(r"[\w\.-]+@[\w\.-]+", re.IGNORECASE),
    "PHONE": re.compile(r"(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})[-\s]?\d{4}"),
    "CPF": re.compile(r"(?<!\d)(?:\d{3}[.\-\s]?\d{3}[.\-\s]?\d{3}[.\-\s]?\d{2}|\d{11})(?!\d)"),
    "PLATE": re.compile(r"\b[A-Z]{3}[-\s]?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "RG": re.compile(r"(?<![\d.,])(?:[A-Z]{2}[-\s]?)?\d{1,3}\.?\d{3}\.?\d{3}[-\s]?[0-9A-Z](?![\w.,])|(?<![\d.,])\d{5,11}(?![\d.,])", re.IGNORECASE),
}

NAME_REGEX = re.compile(
    r"(?<![a-zA-ZÀ-ÿ])"
    r"(?:"
        r"(?:[A-ZÀ-Ÿ]{2,}\s+(?:DE\s+|DA\s+|DO\s+|DOS\s+|DAS\s+|E\s+)?)+[A-ZÀ-Ÿ]{2,}"
        r"|"
        r"(?:[A-ZÀ-Ÿ][a-zà-ÿ]{1,}\s+(?:de\s+|da\s+|do\s+|dos\s+|das\s+|e\s+)?)+[A-ZÀ-Ÿ][a-zà-ÿ]{1,}"
    r")"
    r"(?![a-zA-ZÀ-ÿ])"
)

PREFIX_TRIMMER = re.compile(r"^(ao|a|para|dr\.?|dra\.?|sr\.?|sra\.?|senhor|senhora|em|na|no|de|do|da|soldado|policial)\s+", re.IGNORECASE)

# =========================================================
# JUIZ OLLAMA (O ÚNICO FILTRO SEMÂNTICO)
# =========================================================
@lru_cache(maxsize=10000)
def _ask_ollama_type(text: str, tipo_dado: str) -> bool:
    """Juiz LLM dinâmico. Decide se o dado é sensível pelo contexto."""
    prompts = {
        "CPF": f"Responda APENAS 'SIM' ou 'NAO': O texto '{text}' SE PARECE com um número de CPF?",
        "RG": f"Responda APENAS 'SIM' ou 'NAO': O texto '{text}' SE PARECE com um documento de identidade (RG, CNH, etc)?",
        "PLACA": f"Responda APENAS 'SIM' ou 'NAO': O texto '{text}' SE PARECE com uma placa de veículo?",
        "NOME": (
            f"Responda APENAS 'SIM' ou 'NAO': O texto '{text}' é um nome de PESSOA HUMANA? "
            f"Responda 'NAO' se for uma cidade, órgão público, termo médico, hospital ou empresa."
        )
    }
    
    if tipo_dado not in prompts: return True 

    prompt = f"Você é um classificador rigoroso. {prompts[tipo_dado]}"
    proxies_vazios = {"http": "", "https": ""}
    
    try:
        response = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL, "prompt": prompt, "stream": False
        }, timeout=40, proxies=proxies_vazios)
        
        if response.status_code == 200:
            answer = response.json().get("response", "").strip().upper()
            return "SIM" in answer or "YES" in answer
    except Exception:
        return False 
    return False

# =========================================================
# AVALIADORES
# =========================================================
def evaluate_cpf(text: str) -> bool: return bool(REGEX["CPF"].search(text))
def evaluate_rg(text: str) -> bool: return bool(REGEX["RG"].search(text))
def evaluate_placa(text: str) -> bool: return bool(REGEX["PLATE"].search(text))

def evaluate_nome(text: str) -> bool:
    words = text.split()
    if 3 <= len(text) <= 60 and 1 <= len(words) <= 6:
        if NAME_REGEX.search(text):
            return _ask_ollama_type(text, "NOME")
    return False

# =========================================================
# PROFILER DE COLUNAS 
# =========================================================
def classify_cell(text: str) -> str:
    """Classifica a célula testando formatos rigorosos antes dos formatos flexíveis."""
    text = str(text).strip()
    words = text.split()
    
    if not text or len(text) < 3: return "IGNORAR"
    if len(text) > 80 or len(words) > 8: return "TEXTO_LIVRE"
        
    # =========================================================
    # 1. DADOS EXATOS 
    # =========================================================
    if REGEX["COORD"].search(text) or REGEX["COORD_SINGLE"].search(text): return "GPS"
    if REGEX["EMAIL"].search(text) or "@" in text: return "EMAIL"
    if REGEX["PHONE"].search(text): return "PHONE"
    
    # =========================================================
    # 2. DADOS FLEXÍVEIS 
    # =========================================================
    if REGEX["CPF"].search(text) and _ask_ollama_type(text, "CPF"): return "CPF"
    if REGEX["RG"].search(text) and _ask_ollama_type(text, "RG"): return "RG"
    if REGEX["PLATE"].search(text) and _ask_ollama_type(text, "PLACA"): return "PLACA"
    
    if 3 <= len(text) <= 60 and 1 <= len(words) <= 6:
        if NAME_REGEX.search(text) and _ask_ollama_type(text, "NOME"): 
            return "NOME_SOLTO"
        
    if len(words) >= 3: return "TEXTO_LIVRE"
    
    return "DESCONHECIDO"

def profile_column_type(col_name: str, values_sample: list) -> str:
    """Classifica a coluna baseada inteiramente na análise da IA e Regex."""
    valid_strings = [str(v) for v in values_sample if v and not isinstance(v, (bool, int, float))]
    if not valid_strings: return "IGNORAR"
        
    sample_to_test = random.sample(valid_strings, min(5, len(valid_strings)))
    resultados = [classify_cell(text) for text in sample_to_test]
    
    from collections import Counter
    votos = Counter(resultados)
    
    hits_validos = {k: v for k, v in votos.items() if k not in ["IGNORAR", "DESCONHECIDO"]}
    if hits_validos:
        if "TEXTO_LIVRE" in hits_validos: return "TEXTO_LIVRE"
        return max(hits_validos, key=hits_validos.get)
        
    return "DESCONHECIDO"

# =========================================================
# MOTOR DE DETECÇÃO 
# =========================================================
@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper().strip()

def _is_hallucination(ent_text: str) -> bool:
    """Apenas checagens estruturais. O significado é com o Ollama."""
    ent_text = ent_text.strip(".,;:-\n ")
    if not ent_text or len(ent_text) <= 2: return True
    if not any(c.isupper() for c in ent_text): return True
    
    # Delegamos o julgamento final para o Ollama
    return not _ask_ollama_type(ent_text, "NOME")

def _detect_all(text: str, anon_loc: bool):
    found = []
    # 1. Regex (Dados Estruturados)
    for typ, pat in REGEX.items():
        if not anon_loc and typ in ["COORD", "COORD_SINGLE"]: continue
        for match in pat.finditer(text):
            val = match.group()
            if typ in ["COORD", "COORD_SINGLE", "EMAIL", "PHONE"] or _ask_ollama_type(val, typ):
                found.append((match.start(), match.end(), val, typ))

    # 2. IA/SpaCy (Nomes Próprios)
    if nlp:
        doc = nlp(text)
        for ent in doc.ents:
            if ent.label_ == "PER":
                nome_limpo = PREFIX_TRIMMER.sub("", ent.text).strip()
                if not _is_hallucination(nome_limpo):
                    found.append((ent.start_char, ent.end_char, nome_limpo, "PER"))

    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, text[s:e], t))
            last = e
    return clean

# =========================================================
# GERAÇÃO DE FAKES
# =========================================================
def _get_fake(value: str, typ: str) -> str:
    norm_val = _normalize(html.unescape(value))
    cache_key = f"{typ}:{norm_val}"
    if cache_key in _MAPPING_CACHE: return _MAPPING_CACHE[cache_key]

    seed = int(hashlib.sha256(norm_val.encode()).hexdigest()[:8], 16)
    fake.seed_instance(seed)
    
    if typ in ["PER", "NOME_SOLTO"]: val = fake.name().upper()
    elif typ == "CPF": val = fake.cpf()
    elif typ == "RG": val = fake.numerify('##.###.###-#') 
    elif typ in ["PLATE", "PLACA"]: val = fake.license_plate().upper()
    elif typ == "EMAIL": val = fake.email().lower()
    elif typ == "PHONE": val = fake.phone_number()
    elif typ == "RENAVAM": val = fake.numerify('###########')
    elif typ == "MATRICULA": val = fake.numerify('######')
    else: val = fake.word().upper()

    _MAPPING_CACHE[cache_key] = val
    return val

def anonymize_text(text: str, anon_loc: bool = True) -> str:
    entities = _detect_all(text, anon_loc)
    result, last = [], 0
    for s, e, v, t in entities:
        result.append(text[last:s])
        result.append(_get_fake(v, t))
        last = e
    result.append(text[last:])
    return "".join(result)

def anonymize_value(col_name: str, val, anon_location: bool = True):
    if val is None or not isinstance(val, str): return val, None
    new_v = anonymize_text(val, anon_location)
    return new_v, ("TEXT" if new_v != val else None)

def should_anonymize_column(col_name: str, sample_values) -> bool:
    c = col_name.lower()
    targets = ["nome", "vitima", "autor", "cpf", "rg", "placa", "condutor", "proprietario", "email", "mail", "contato"]
    return any(k in c for k in targets)

def reset_memory(): _MAPPING_CACHE.clear()