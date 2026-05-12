import re
import random
import string
import unicodedata
import hashlib
import logging
import os
import requests
import json
from functools import lru_cache
from faker import Faker
import spacy
from spacy.cli import download

# Configurações de LOG e Ambiente
# Mude para logging.INFO em produção para limpar o terminal
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s:%(name)s:%(message)s')
logger = logging.getLogger(__name__)

# =========================================================
# CONFIGURAÇÕES GLOBAIS
# =========================================================
_MAPPING_CACHE = {}
_USED_FAKES = set()
fake = Faker("pt_BR")

CACHE_LIMIT = 500000
DEBUG_MODE = True

# Inicialização segura do spaCy
try:
    nlp = spacy.load("pt_core_news_sm")
    logger.info("[SPACY] Modelo linguístico carregado com sucesso.")
except OSError:
    logger.warning("[SPACY] Modelo ausente. Rodando em modo de segurança (sem NLP profunda).")
    nlp = None

# Blacklist definitiva para evitar que termos técnicos virem nomes
MEDICAL_LEGAL_BLACKLIST = {
    "exame", "cavidade", "oral", "torax", "abdominal", "cervical", "ombro", 
    "cotovelo", "esquerdo", "direito", "membro", "membros", "inferior", "inferiores", 
    "superior", "superiores", "afundamento", "hemorragia", "interna", "externo", 
    "aguda", "sistema", "circulatorio", "respiratorio", "vasos", "base", 
    "hospital", "clinica", "boletim", "ocorrencia", "internamento", "distrito", 
    "ficha", "encaminhamento", "acidente", "transito", "medindo", "aproximadamente", 
    "regional", "processo", "penal", "delegacia", "atropelamento", "historico",
    "animal", "silvestre", "colidir", "tancredo", "neves", "coronel", "vivida",
    "hist", "rico", "sinistro", "guaxinim", "battle", "politrauma", "forame", "magno", 
    "encef", "cranio", "craniano", "sinal", "infarto", "miocardio", "agudo", "gradil", 
    "costal", "cavidades", "pleurais", "interface", "toracoabdominal", "toracoabdominais",
    "achado", "achados", "corporal", "geral", "santa", "tereza", "formosa", "oeste", 
    "ceo", "periorbit", "revestimento", "cut", "nio", "local", "patol", "cef", "vitima", "autor",
    "rio", "janeiro", "sao", "paulo", "curitiba", "parana", "brasil", "rua", "avenida", "praça","viatura", "veiculo", "veículo", "centro", "bairro", "rua", "avenida", 
    "via", "publica", "pública", "desconhecido", "indivíduo", "elemento", 
    "local", "estabelecimento", "cidade", "estado", "município", "distrito"
}

# =========================================================
# REGEX DE DADOS ESTRUTURADOS
# =========================================================
REGEX = {
    "CPF": re.compile(r"\b(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(\d{2}\)|\d{2})\s?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "RG": re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}-?[0-9X]\b"),
    "COORD": re.compile(r"^\s*-?(?:90(?:\.0+)?|[0-8]?\d(?:\.\d+)?)\s*,\s*-?(?:180(?:\.0+)?|1[0-7]\d(?:\.\d+)?|\d{1,2}(?:\.\d+)?)\s*$"),
}

# Regex de apoio para nomes: Aceita ALL CAPS, Title Case, Mixed e abreviações com ponto (ex: D.)
NAME_REGEX = re.compile(
    r"\b("
    r"[A-ZÀ-Ü][a-zA-ZÀ-Üà-ü\'\.]+"                                     
    r"(?:\s+(?:de|da|do|dos|das|e|DE|DA|DO|DOS|DAS|E)\s+|\s+)"       
    r"[A-ZÀ-Ü][a-zA-ZÀ-Üà-ü\'\.]+"                                    
    r"(?:\s+[A-ZÀ-Ü][a-zA-ZÀ-Üà-ü\'\.]+){0,4}"                       
    r")\b"
)
PREFIX_TRIMMER = re.compile(r"^(ao|a|para|dr\.?|dra\.?|sr\.?|sra\.?|em|na|no|de|do|da|vitima|autor|paciente)\s+", re.IGNORECASE)

# =========================================================
# IA: LLM LOCAL (OLLAMA) E NLP (SPACY)
# =========================================================
def _text_needs_llm(text: str) -> bool:
    """Árbitro Dinâmico: Usa NLP para analisar a gramática."""
    if not text: return False
    text_clean = str(text).strip()
    
    if len(text_clean) < 3:
        return False
        
    text_lower = text_clean.lower()
    trigger_words = ["relat", "fugi", "agredi", "vitim", "vítim", "autor", "suspeit", "pres", "ocorrencia", "ocorrência", "evadi", "furt", "roub", "envolvi", "aborda"]
    
    if any(w in text_lower for w in trigger_words):
        return True

    # 🔥 CORREÇÃO: Se for texto em minúsculo com 2+ palavras, manda pra IA!
    # A Regex não pega minúsculas, então a IA precisa atuar.
    if text_clean.islower() and len(text_clean.split()) >= 2:
        return True

    if nlp:
        doc = nlp(text_clean)
        propn_count = sum(1 for token in doc if token.pos_ == "PROPN")
        pron_count = sum(1 for token in doc if token.pos_ == "PRON")
                
        if pron_count >= 1 or propn_count >= 1:
            return True
            
        if any(ent.label_ == "PER" for ent in doc.ents):
            return True

    if len(text_clean.split()) >= 4:
        return True

    return False

def _ask_local_llm(text: str) -> list:
    """Consulta o Ollama APENAS para extrair os nomes reais encontrados."""
    prompt = f"""Você é um extrator de nomes próprios para LGPD.
Extraia APENAS os nomes completos de PESSOAS do texto abaixo.

REGRAS:
1. Ignore cargos, locais, objetos ou jargões.
2. Não invente nomes falsos. Apenas extraia os reais.
3. Retorne APENAS um JSON: {{"nomes": ["NOME 1", "NOME 2"]}}

Texto: {text}
"""
    try:
        response = requests.post("http://localhost:11434/api/generate", json={
            "model": "llama3", 
            "prompt": prompt,
            "format": "json", 
            "stream": False,
            "options": {"temperature": 0.0}
        }, timeout=120)
        
        if response.status_code == 200:
            return json.loads(response.json().get("response", "{}")).get("nomes", [])
        return []
    except Exception as e:
        logger.error(f"Erro na IA: {e}")
        return []

# =========================================================
# UTILITÁRIOS DE APOIO
# =========================================================
@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", "", text.upper().strip())

def _is_hallucination(ent_text: str) -> bool:
    """Verifica se o termo detectado é lixo ou jargão técnico."""
    # 🔥 Corrigido: Remoção do islower() e tolerância para nomes de 3 letras (ex: Ana)
    if not ent_text or len(str(ent_text)) <= 2:
        return True
    words = [w.strip(string.punctuation).lower() for w in str(ent_text).split()]
    if any(w in MEDICAL_LEGAL_BLACKLIST for w in words):
        return True
    return False

# =========================================================
# GERAÇÃO DE DADOS FALSOS
# =========================================================
def _get_fake(value: str, typ: str) -> str:
    global _MAPPING_CACHE
    
    if len(_MAPPING_CACHE) >= CACHE_LIMIT:
        first_key = next(iter(_MAPPING_CACHE))
        del _MAPPING_CACHE[first_key]
        
    norm_val = _normalize(value)
    cache_key = f"{typ}:{norm_val}"
    
    if cache_key in _MAPPING_CACHE: 
        return _MAPPING_CACHE[cache_key]

    seed = int(hashlib.sha256(norm_val.encode()).hexdigest()[:8], 16)
    fake.seed_instance(seed)
    random.seed(seed)

    if typ == "PER": val = f"{fake.first_name()} {fake.last_name()}".upper()
    elif typ == "CPF": val = fake.cpf()
    elif typ == "EMAIL": val = fake.email()
    elif typ == "PLATE": val = fake.license_plate().upper()
    elif typ == "COORD":
        lat, lon = map(float, value.split(","))
        val = f"{lat + random.uniform(-0.003, 0.003):.4f}, {lon + random.uniform(-0.003, 0.003):.4f}"
    else:
        val = "".join(random.choices(string.ascii_uppercase + string.digits, k=len(value)))

    _MAPPING_CACHE[cache_key] = val
    return val

# =========================================================
# MOTOR DE DETECÇÃO HÍBRIDA
# =========================================================
def _detect_all(text: str, anon_loc: bool):
    found = []

    # 1. Regex: Dados Estruturados
    for typ, pat in REGEX.items():
        for match in pat.finditer(text):
            found.append((match.start(), match.end(), match.group(), typ))

    # 2. Regex de Nomes: Casos óbvios
    for match in NAME_REGEX.finditer(text):
        if not _is_hallucination(match.group()):
            found.append((match.start(), match.end(), match.group(), "PER"))

    # 3. Inteligência Artificial: Agora apenas para capturar nomes difíceis/minúsculos
    if _text_needs_llm(text):
        nomes_reais = _ask_local_llm(text)
        
        for nome_real in nomes_reais:
            nome_limpo = PREFIX_TRIMMER.sub("", str(nome_real)).strip()
            
            if _is_hallucination(nome_limpo): 
                continue
            
            # Aqui não injetamos mais o fake no cache. 
            # Deixamos o _get_fake gerar um nome único baseado no hash do nome_limpo.
            
            # Localiza no texto original para substituição exata
            padrao = re.compile(rf"\b{re.escape(nome_limpo)}\b", re.IGNORECASE)
            for match in padrao.finditer(text):
                found.append((match.start(), match.end(), match.group(), "PER"))

    # 4. Tratamento de Sobreposições
    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, v, t))
            last = e
    return clean

# =========================================================
# FUNÇÕES DE INTERFACE COM O APP.PY
# =========================================================
def anonymize_text(text: str, anon_loc: bool = True) -> str:
    if not isinstance(text, str) or len(text) < 3: return text
    entities = _detect_all(text, anon_loc)
    if not entities: return text

    result, last = [], 0
    for s, e, v, t in entities:
        result.extend([text[last:s], _get_fake(v, t)])
        last = e
    result.append(text[last:])
    return "".join(result)

def anonymize_value(col_name: str, val, anon_location: bool = True):
    if val is None or not isinstance(val, str):
        return val, None
    new_v = anonymize_text(val, anon_location)
    return new_v, ("TEXT" if new_v != val else None)

def should_anonymize_column(col_name: str, sample_values) -> bool:
    c = col_name.lower()
    if any(k in c for k in ["nome", "vitima", "autor", "cpf", "rg", "placa"]):
        return True
    return False

def reset_memory():
    _MAPPING_CACHE.clear()
    _USED_FAKES.clear()

# =========================================================
# GLINER CACHE
# =========================================================

_GLINER_MODEL = None

def get_gliner():
    global _GLINER_MODEL
    if _GLINER_MODEL is not None:
        return _GLINER_MODEL
    try:
        from gliner import GLiNER
        logger.info("[GLINER] carregando modelo")
        _GLINER_MODEL = GLiNER.from_pretrained("urchade/gliner_medium-v2.1")
        logger.info("[GLINER] modelo carregado")
        return _GLINER_MODEL
    except Exception as e:
        logger.warning(f"[GLINER DISABLED] {e}")
        _GLINER_MODEL = False
        return _GLINER_MODEL