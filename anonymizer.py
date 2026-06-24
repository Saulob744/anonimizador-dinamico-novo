import re
import os
import random
import string
import unicodedata
import hashlib
import logging
import requests
import html
import hmac
import json
from functools import lru_cache
from faker import Faker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s')
logger = logging.getLogger(__name__)

SECRET_SALT = os.getenv("ANONYMIZER_SECRET_SALT", "SaltSeguroSESP2026_Producao!")

# =========================================================
# CARREGAMENTO DA IA
# =========================================================
try:
    import spacy
    nlp = spacy.load("pt_core_news_lg", disable=["parser", "attribute_ruler", "lemmatizer", "tok2vec"])
except OSError:
    logger.error("🚨 Modelo spaCy 'pt_core_news_lg' não encontrado. Instale-o com: python -m spacy download pt_core_news_lg")
    nlp = None

http_session = requests.Session()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3:latest")

_MAPPING_CACHE = {}
_OLLAMA_CACHE = {} 

HTML_TAG_REGEX = re.compile(r'<[^>]+>')
HTML_ENTITY_REGEX = re.compile(r'&[a-zA-Z0-9#]+;')

NOME_MAIUSCULO_REGEX = re.compile(
    r"\b(?:[A-ZÀ-Ÿ]{2,})"                
    r"(?:\s+(?:DE|DA|DO|DOS|DAS|E))?"    
    r"(?:\s+[A-ZÀ-Ÿ]{2,}){1,8}\b"        
)

TERMOS_PROIBIDOS = {
    "EXAMES COMPLEMENTARES", "HISTORICO", "COR DOS OLHOS", "RACA", "COR",
    "ESTATURA", "TALHE", "CONCLUSAO", "POLICIA CIENTIFICA", "INSTITUTO MEDICO",
    "SECRETARIA DE SEGURANCA", "SESP", "BOLETIM DE OCORRENCIA", "MEDICINA LEGAL"
}

CONTEXT_NAME_REGEX = re.compile(r"\b(vitima|autor|paciente|sr|sra|dr|dra|perito|legista|motorista)\s*[:\-]?\s+([A-ZÀ-Ÿ]{2,20}(?:\s+[A-ZÀ-Ÿ]{2,20}){1,4})\b", re.IGNORECASE)

@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper().strip()

# =========================================================
# OLLAMA
# =========================================================
def _ask_llm_batch(candidates: list) -> list:
    if not candidates: return []
    unknowns, approved = [], []
    
    for c in set(candidates):
        key = f"PER_JUDGE:{c.upper()}"
        if key in _OLLAMA_CACHE:
            if _OLLAMA_CACHE[key]: approved.append(c)
        else:
            unknowns.append(c)
            
    if not unknowns: return approved
    
    prompt = (
        "Atue como um analista forense e filtro de dados.\n"
        "Abaixo está uma lista de expressões extraídas de laudos do IML/Polícia Científica.\n"
        "Selecione APENAS os nomes próprios completos de PESSOAS REAIS (ex: vítimas, médicos, peritos, envolvidos).\n"
        "REGRA DE EXCLUSÃO CRÍTICA: Você DEVE REJEITAR termos médicos, lesões, anatomia (ex: MEMBROS, ABDOMEN, HEMORRAGIA, TRAUMATISMO, FRATURA), ações (ex: FOTOS EM ANEXO, LAPAROTOMIA, ACIDENTE DE TRANSITO), ou indicações de direção (DIREITA, ESQUERDA).\n"
        f"Lista para análise: {unknowns}\n"
        "Retorne o resultado ESTRITAMENTE no formato JSON: {\"nomes_reais\": [\"Nome 1\", \"Nome 2\"]}"
    )
    
    try:
        payload = {
            "model": OLLAMA_MODEL, 
            "prompt": prompt, 
            "format": "json", 
            "stream": False, 
            "options": {"temperature": 0.0, "num_predict": 150} 
        }
        resp = http_session.post(OLLAMA_URL, json=payload, timeout=40)
        
        if resp.status_code == 200:
            data = json.loads(resp.json().get("response", ""))
            reais = [str(n).upper() for n in data.get("nomes_reais", [])]
            
            for c in unknowns:
                valido = any(c.upper() == n or c.upper() in n for n in reais)
                _OLLAMA_CACHE[f"PER_JUDGE:{c.upper()}"] = valido
                if valido: 
                    approved.append(c)
    except Exception as e:
        logger.warning(f"⚠️ Falha no Ollama. Ignorando lote preventivamente. Erro: {e}")
        for c in unknowns: 
            _OLLAMA_CACHE[f"PER_JUDGE:{c.upper()}"] = False
        
    return approved

# =========================================================
# GERAÇÃO PSEUDONIMIZADA
# =========================================================
def _get_consistent_fake_name(original_name: str) -> str:
    norm_val = _normalize(original_name)
    cache_key = f"PER:{norm_val}"
    
    if cache_key in _MAPPING_CACHE: 
        return _MAPPING_CACHE[cache_key]

    hash_bytes = hmac.new(SECRET_SALT.encode('utf-8'), norm_val.encode('utf-8'), hashlib.sha256).digest()
    seed_int = int.from_bytes(hash_bytes[:8], byteorder='big')
    
    local_fake = Faker("pt_BR")
    local_fake.seed_instance(seed_int)
    
    fake_name = local_fake.name().upper()
    _MAPPING_CACHE[cache_key] = fake_name
    return fake_name

# =========================================================
# MOTOR PRINCIPAL DE ANONIMIZAÇÃO
# =========================================================
def anonymize_value(col_name: str, val, anon_location: bool = True):
    if not isinstance(val, str) or len(val) < 3 or not nlp: 
        return val, None
        
    text = str(val)
    vault = {}
    
    # --- FASE 1: ESCUDO HTML ---
    def shield(match):
        token = f" __SHLD{len(vault)}__ "
        vault[token.strip()] = match.group(0)
        return token

    safe_text = HTML_TAG_REGEX.sub(shield, text)
    safe_text = HTML_ENTITY_REGEX.sub(shield, safe_text)
    
    suspects = []
    
    doc = nlp(safe_text.title())
    for ent in doc.ents:
        if ent.label_ == "PER":
            clean_name = ent.text.upper().strip(".,;:?!() \n'\"")
            if len(clean_name) > 3 and "__SHLD" not in clean_name:
                suspects.append(clean_name)
                
    for match in CONTEXT_NAME_REGEX.finditer(safe_text):
        name = match.group(2).strip().upper()
        if len(name) > 3 and "__SHLD" not in name:
            suspects.append(name)
            
    for match in NOME_MAIUSCULO_REGEX.finditer(safe_text):
        name = match.group().strip()
        if "__SHLD" not in name and name not in TERMOS_PROIBIDOS:
            suspects.append(name)

    unique_suspects = list(set(suspects))
    valid_names = _ask_llm_batch(unique_suspects)
    
    # --- NOVA FASE: EXPANSAO DE CO-REFERENCIA 
    local_replacements = {}
    
    for name in valid_names:
        fake_name = _get_consistent_fake_name(name)
        local_replacements[name] = fake_name
        
     
        parts_real = name.split()
        parts_fake = fake_name.split()
        
        if len(parts_real) > 1 and len(parts_fake) > 1:
            primeiro_real = parts_real[0]
            ultimo_real = parts_real[-1]
            primeiro_falso = parts_fake[0]
            ultimo_falso = parts_fake[-1]
            
         
          
            if len(ultimo_real) > 3 and ultimo_real not in TERMOS_PROIBIDOS:
                if ultimo_real not in local_replacements:
                    local_replacements[ultimo_real] = ultimo_falso
                    
            if len(primeiro_real) > 3 and primeiro_real not in TERMOS_PROIBIDOS:
                if primeiro_real not in local_replacements:
                    local_replacements[primeiro_real] = primeiro_falso


    sorted_targets = sorted(local_replacements.keys(), key=len, reverse=True)
    
    res = safe_text
    for target in sorted_targets:
        if target not in res:
            continue
            
        fake_target = local_replacements[target]
        pattern = re.compile(rf"\b{re.escape(target)}\b")
        
        res, count = pattern.subn(fake_target, res)
        if count > 0:
            logger.info(f"🕵️ [TROCA IA | {col_name}] '{target}' ➡️ '{fake_target}'")
        
    for token, original in vault.items():
        res = res.replace(f" {token} ", original).replace(token, original)
        
    return res, ("TEXT" if res != text else None)

def process_chunk_parallel(rows, modo, anon_geo, target_columns):
    processed = []
    for r in rows:
        row_dict = dict(r)
        for col, old in row_dict.items():
            if col in target_columns:
                new_val, _ = anonymize_value(col, old)
                row_dict[col] = new_val
        processed.append(row_dict)
    return processed
