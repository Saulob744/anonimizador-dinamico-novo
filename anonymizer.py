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

# Configurações de LOG e Ambiente
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================================================
# CONFIGURAÇÕES GLOBAIS
# =========================================================
_MAPPING_CACHE = {}
_USED_FAKES = set()
fake = Faker("pt_BR")

CACHE_LIMIT = 500000
DEBUG_MODE = True

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
    "ceo", "periorbit", "revestimento", "cut", "nio", "local", "patol", "cef", "vitima", "autor"
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

# Regex de apoio para nomes e limpeza
NAME_REGEX = re.compile(r"\b([A-ZÀ-Ü][a-zà-ü]+(?:\s(?:de|da|do|dos|das)\s|\s)[A-ZÀ-Ü][a-zà-ü]+(?:\s[A-ZÀ-Ü][a-zà-ü]+){0,3})\b")
PREFIX_TRIMMER = re.compile(r"^(ao|a|para|dr\.?|dra\.?|sr\.?|sra\.?|em|na|no|de|do|da|vitima|autor|paciente)\s+", re.IGNORECASE)

# =========================================================
# IA: LLM LOCAL (OLLAMA)
# =========================================================
def _ask_local_llm(text: str) -> list:
    """Consulta o Ollama para extrair nomes próprios em formato JSON."""
    prompt = f"""
    Você é um perito em LGPD. Extraia APENAS os nomes próprios completos de PESSOAS do texto.
    REGRAS:
    1. IGNORE hospitais, cidades, ruas, jargões médicos e policiais.
    2. Retorne APENAS um JSON: {{"nomes": ["NOME 1", "NOME 2"]}}
    3. Se não houver nomes, retorne {{"nomes": []}}
    Texto: {text}
    """
    try:
        response = requests.post("http://localhost:11434/api/generate", json={
            "model": "llama3", # Certifique-se de que baixou este modelo
            "prompt": prompt,
            "format": "json",
            "stream": False
        }, timeout=45)
        return json.loads(response.json()["response"]).get("nomes", [])
    except:
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
    if not ent_text or ent_text.islower() or len(ent_text) <= 3:
        return True
    words = [w.strip(string.punctuation).lower() for w in ent_text.split()]
    if any(w in MEDICAL_LEGAL_BLACKLIST for w in words):
        return True
    return False

# =========================================================
# GERAÇÃO DE DADOS FALSOS
# =========================================================
def _get_fake(value: str, typ: str) -> str:
    global _MAPPING_CACHE
    norm_val = _normalize(value)
    cache_key = f"{typ}:{norm_val}"
    
    if cache_key in _MAPPING_CACHE: 
        return _MAPPING_CACHE[cache_key]

    # Seed determinística: garante que o mesmo valor original sempre gere o mesmo fake
    seed = int(hashlib.sha256(norm_val.encode()).hexdigest()[:8], 16)
    fake.seed_instance(seed)
    random.seed(seed)

    if typ == "PER": val = fake.name().upper()
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

    # 1. Regex: Dados Estruturados (CPF, Placas, etc)
    for typ, pat in REGEX.items():
        for match in pat.finditer(text):
            found.append((match.start(), match.end(), match.group(), typ))

    # 2. Regex: Nomes Óbvios (Title Case)
    for match in NAME_REGEX.finditer(text):
        if not _is_hallucination(match.group()):
            found.append((match.start(), match.end(), match.group(), "PER"))

    # 3. Inteligência Artificial: Ollama (Contexto)
    if len(text) > 40:
        nomes_ia = _ask_local_llm(text)
        for nome in nomes_ia:
            # Limpa prefixos como "Vitima " do retorno da IA
            nome_limpo = PREFIX_TRIMMER.sub("", nome).strip()
            if _is_hallucination(nome_limpo): continue
            
            # Localiza no texto original para substituição exata
            for match in re.finditer(re.escape(nome_limpo), text, re.IGNORECASE):
                found.append((match.start(), match.end(), match.group(), "PER"))

    # Ordenar e remover sobreposições
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
    # Lógica baseada no nome da coluna ou conteúdo
    c = col_name.lower()
    if any(k in c for k in ["nome", "vitima", "autor", "cpf", "rg", "placa"]):
        return True
    return False

def reset_memory():
    _MAPPING_CACHE.clear()
    _USED_FAKES.clear()