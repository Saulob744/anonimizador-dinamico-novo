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

# =========================================================
# CONFIGURAÇÕES GLOBAIS & LOGS
# =========================================================
# ⚡ AJUSTE: Nível alterado para ERROR. O sistema ficará mudo, 
# exceto se houver um erro fatal que vá parar o pipeline.
logging.basicConfig(level=logging.ERROR, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

try:
    import spacy
    nlp = spacy.load("pt_core_news_lg")
except OSError:
    # Como a ausência do SpaCy não quebra o pipeline (ele tem fallback), 
    # mantemos apenas como warning silencioso que não para a execução.
    logger.warning("Modelo SpaCy não encontrado. Rodando em modo de contingência (apenas Regex).")
    nlp = None

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
    "CPF": re.compile(r"(?<!\d)(?:\d{3}[.\-\s]?\d{3}[.\-\s]?\d{3}[.\-\s]?\d{2}|\d{11})(?!\d)"),
    "PLATE": re.compile(r"\b[A-Z]{3}[-\s]?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "RG": re.compile(r"(?<![\d.,\-])(?:[A-Z]{2}[-\s]?)?\d{1,3}\.?\d{3}\.?\d{3}[-\s]?[0-9A-Z](?![\w.,])|(?<![\d.,\-])\d{5,11}(?![\d.,\-])", re.IGNORECASE),
    "PHONE": re.compile(r"(?<!\d)(?:\+?55\s?)?(?:\(?\d{2}\)?[\s-]?)?\d{4,5}[-\s]\d{4}(?!\d)"),
}


NAME_REGEX = re.compile(
    r"\b(?:[A-ZÀ-Ÿ][a-zà-ÿ]{1,20}|[A-ZÀ-Ÿ]{2,20})" # 
    r"(?:\s+(?:de|da|do|dos|das|e|DE|DA|DO|DOS|DAS|E))?" # Preposição opcional
    r"(?:\s+(?:[A-ZÀ-Ÿ][a-zà-ÿ]{1,20}|[A-ZÀ-Ÿ]{2,20})){1,5}\b" # 1 a 5 sobrenomes
)

PREFIX_TRIMMER = re.compile(r"^(ao|a|para|dr\.?|dra\.?|sr\.?|sra\.?|senhor|senhora|em|na|no|de|do|da|vitima|autor|paciente|soldado|policial|rua|avenida|trevo|cia)\s+", re.IGNORECASE)

# =========================================================
# JUIZ OLLAMA
# =========================================================
@lru_cache(maxsize=10000)
def _ask_ollama_type(text: str, tipo_dado: str) -> bool:
    prompts = {
        "CPF": f"Responda APENAS 'SIM' ou 'NAO': O texto '{text}' APARENTA SER um número de CPF válido?",
        "RG": f"Responda APENAS 'SIM' ou 'NAO': O texto '{text}' SE PARECE COM um RG, CNH ou documento de identidade?",
        "PLACA": f"Responda APENAS 'SIM' ou 'NAO': O texto '{text}' SE PARECE COM uma placa de veículo?",
        "NOME": (
            f"Responda APENAS 'SIM' ou 'NAO': O texto '{text}' SE PARECE COM um nome próprio de pessoa humana? "
            f"Responda 'NAO' se for uma cidade, estado, país, endereço, empresa ou termo médico."
        )
    }
    
    if tipo_dado not in prompts: return True 

    prompt = f"Você é um classificador focado em LGPD. {prompts[tipo_dado]}"
    proxies_vazios = {"http": "", "https": ""}
    
    try:
        response = requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}, timeout=40, proxies=proxies_vazios)
        if response.status_code == 200:
            answer = response.json().get("response", "").strip().upper()
            return "SIM" in answer or "YES" in answer
    except Exception:
        # Se a IA estiver offline ou der timeout, retorna False e segue a vida. Não quebra o pipeline.
        return False 
    return False

# =========================================================
# PROFILER INTELIGENTE DE COLUNAS
# =========================================================
def classify_cell(text: str) -> str:
    text = str(text).strip()
    words = text.split()
    
    if not text or len(text) < 3: return "IGNORAR"
    if len(text) > 80 or len(words) > 8: return "TEXTO_LIVRE"
        
    if REGEX["COORD"].search(text) or REGEX["COORD_SINGLE"].search(text): return "GPS"
    if REGEX["EMAIL"].search(text) or "@" in text: return "EMAIL"
    
    if REGEX["CPF"].search(text) and _ask_ollama_type(text, "CPF"): return "CPF"
    if REGEX["RG"].search(text) and _ask_ollama_type(text, "RG"): return "RG"
    if REGEX["PLATE"].search(text) and _ask_ollama_type(text, "PLACA"): return "PLACA"
    if REGEX["PHONE"].search(text): return "PHONE"
    
    if 3 <= len(text) <= 60 and 1 <= len(words) <= 6:
        if NAME_REGEX.search(text) and _ask_ollama_type(text, "NOME"): return "NOME_SOLTO"
        
    if len(words) >= 3: return "TEXTO_LIVRE"
    return "DESCONHECIDO"

def profile_column_type(col_name: str, values_sample: list) -> str:
    try:
        c = str(col_name).lower().strip()
        
        
        c_words = set(c.replace("_", " ").replace("-", " ").split())
        
        blacklist = {"cidade", "municipio", "bairro", "estado", "uf", "pais", "cep", "papel", "prefixo", "status", "situacao", "tipo", "cor", "marca", "modelo", "id", "uuid", "guid", "created_at", "updated_at"}
        
   
        if c in blacklist or "id" in c_words or c.endswith("id"):
            return "IGNORAR"
            
        if "pix" in c_words or "chave" in c_words:
            return "TEXTO_LIVRE"
            
        
        if any(k in c_words for k in ["lat", "latitude", "lon", "longitude", "gps", "coord", "coordenada", "geo", "loc"]): 
            return "GPS_SINGLE"
            
        if "cpf" in c_words or "cpf" in c: return "CPF"
        
       
        if "rg" in c_words or c == "rg": return "RG"
        
        if "placa" in c_words or "placa" in c: return "PLACA"
        if "email" in c_words or "mail" in c_words or "email" in c: return "EMAIL"
        if "renavam" in c_words or "renavam" in c: return "RENAVAM"
        if "matricula" in c_words or "matricula" in c: return "MATRICULA"
        
        # Telefones (evitando que "microfone" ative "fone")
        if "telefone" in c_words or "celular" in c_words or "fone" in c_words: return "PHONE"
        
        # Nomes (evitando que "fenomeno" ative "nome")
        if any(k in c_words for k in ["nome", "vitima", "autor", "condutor", "proprietario"]): return "NOME_SOLTO"

        # === Se falhou em prever pelo nome, manda a IA olhar os valores ===
        valid_strings = [str(v) for v in values_sample if v is not None and str(v).strip() != ""]
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

    except Exception as e:
        logger.critical(f"🚨 [ERRO FATAL] Colapso no Profiler na coluna '{col_name}': {e}")
        raise

    except Exception as e:
        # ⚡ AJUSTE: Erro fatal disparado APENAS se a lógica de profiling colapsar.
        logger.critical(f"🚨 [ERRO FATAL] Colapso no Profiler na coluna '{col_name}': {e}")
        raise # Interrompe o pipeline para o app.py capturar

# =========================================================
# O PENTE FINO
# =========================================================
@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper().strip()

def _is_hallucination(ent_text: str) -> bool:
    ent_text = ent_text.strip(".,;:-\n ")
    if not ent_text or len(ent_text) <= 2: return True
    if any(c.isdigit() for c in ent_text) and any(c in ['.', ',', '-'] for c in ent_text):
        return True
    if not any(c.isupper() for c in ent_text): return True
    if re.match(r"^([A-Z]\.){1,3}[A-Z]?$", ent_text.upper().strip()): return True
    
    termos_proibidos = {"CPF", "RG", "CNPJ", "CEP", "DOC", "DOCUMENTO", "TEL", "CEL", "TELEFONE", "CELULAR", "PIX", "CHAVE", "PLACA", "DR", "DRA", "SR", "SRA"}
    texto_norm = _normalize(ent_text).upper()
    if texto_norm in termos_proibidos or any(texto_norm.startswith(t + " ") for t in termos_proibidos): 
        return True

    locais_exatos = {"sao paulo", "rio de janeiro", "curitiba", "parana", "brasil", "minas gerais", "bahia", "santa catarina", "ceara", "pernambuco", "mato grosso", "goias", "amazonas", "espirito santo", "porto alegre", "belo horizonte", "salvador", "fortaleza", "recife", "brasilia", "campinas", "maringa", "londrina", "cascavel"}
    if _normalize(ent_text).lower() in locais_exatos: return True

    if not _ask_ollama_type(ent_text, "NOME"): return True 
    return False

def _detect_all(text: str, anon_loc: bool):
    found = []
    for typ, pat in REGEX.items():
        if not anon_loc and typ in ["COORD", "COORD_SINGLE"]: continue
        for match in pat.finditer(text):
            val = match.group()
            if typ in ["COORD", "COORD_SINGLE", "EMAIL", "PHONE"]: found.append((match.start(), match.end(), val, typ))
            elif _ask_ollama_type(val, typ): found.append((match.start(), match.end(), val, typ))

    if nlp:
        doc = nlp(text)
        for ent in doc.ents:
            if ent.label_ == "PER":
                nome_limpo = PREFIX_TRIMMER.sub("", ent.text).strip()
                if not _is_hallucination(nome_limpo): found.append((ent.start_char, ent.end_char, nome_limpo, "PER"))

    for match in NAME_REGEX.finditer(text):
        val = match.group().strip()
        val_clean = re.sub(r'\s+', ' ', val)
        if not _is_hallucination(val_clean): found.append((match.start(), match.end(), val_clean, "PER"))

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
    clean_value = html.unescape(re.sub(r'<[^>]+>', '', value)).strip()
    norm_val = _normalize(clean_value)
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

def anonymize_value(col_name: str, val, anon_location: bool = True):
    try:
        if val is None or not str(val).strip(): return val, None
        text = str(val)
        if len(text) < 3: return text, None
        
        entities = _detect_all(text, anon_location)
        result, last = [], 0
        for s, e, v, t in entities:
            result.append(text[last:s])
            result.append(_get_fake(v, t))
            last = e
        result.append(text[last:])
        new_v = "".join(result)
        return new_v, ("TEXT" if new_v != text else None)

    except Exception as e:
        
        logger.critical(f"🚨 [ERRO FATAL] Falha de processamento na coluna '{col_name}' com o valor: {str(val)[:50]}... Detalhe: {e}")
        raise 

def reset_memory():
    _MAPPING_CACHE.clear()