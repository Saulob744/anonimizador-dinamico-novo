import re
import random
import string
import unicodedata
import hashlib
import logging
import requests
import html
from functools import lru_cache
from faker import Faker
from collections import Counter

# =========================================================
# CONFIGURAÇÃO DE LOGS (Essencial para debugar)
# =========================================================
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s')
logger = logging.getLogger(__name__)

logger.debug("Iniciando carregamento das ferramentas de anonimização...")

try:
    import spacy
    nlp = spacy.load("pt_core_news_lg")
    logger.debug("spaCy carregado com sucesso.")
except OSError:
    logger.warning("spaCy não encontrado. A detecção de nomes dependerá apenas de Regex e Ollama.")
    nlp = None

_MAPPING_CACHE = {}
fake = Faker("pt_BR")

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "llama3:latest"

# =========================================================
# REGEX ESTRUTURAIS (Os Caçadores)
# =========================================================
REGEX = {
    "COORD": re.compile(r"-?\d{1,2}[.,]\d+\s*[,;]?\s*-?\d{1,3}[.,]\d+"), 
    "COORD_SINGLE": re.compile(r"^-?\d{1,3}[.,]\d{4,}$|^-\d{5,10}$"), 
    "EMAIL": re.compile(r"[\w\.-]+@[\w\.-]+", re.IGNORECASE),
    "CPF": re.compile(r"\b\d{3}[.\-\s]*\d{3}[.\-\s]*\d{3}[.\-\s]*\d{2}\b|\b\d{11}\b"),
    "RG": re.compile(r"\b(?:[A-Z]{2}[-\s]*)?\d{1,3}[.\-\s]*\d{3}[.\-\s]*\d{3}[-\s]*[0-9A-Z]\b|\b\d{5,14}\b", re.IGNORECASE),
    "PLATE": re.compile(r"\b[A-Z]{3}[-\s]?\d[A-Z0-9]\d{2}\b|\b[A-Z]{3}[-\s]?\d{4}\b", re.IGNORECASE),
    "PHONE": re.compile(r"\b(?:\+?55\s*)?(?:\(?\d{2,3}\)?[\s-]*)?\d{4,5}[-\s]*\d{4}\b"),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[a-fA-F0-9]{1,4}:){7}[a-fA-F0-9]{1,4}\b"),
    "CHASSI": re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE), 
    "GENERIC_CODE": re.compile(r'\b[A-Za-z0-9_/\.\-]{5,64}\b'),
}

CONTEXT_NAME_REGEX = re.compile(r"\b(nome|cliente|paciente|sr|sra|dr|dra|atendente|consultor|gerente|responsavel|usuario|agente|vendedor|motorista|funcionario|titular|favorecido)\s*[:\-]?\s+([A-ZÀ-Ÿa-zà-ÿ]{2,20}(?:\s+[A-ZÀ-Ÿa-zà-ÿ]{2,20}){0,4})\b", re.IGNORECASE)
PREFIX_TRIMMER = re.compile(r"^(ao|a|para|dr\.?|dra\.?|sr\.?|sra\.?|senhor|senhora|em|na|no|de|do|da|vitima|autor|paciente|soldado|policial|rua|avenida|trevo|cia|agente|vendedor|motorista|funcionario)\s+", re.IGNORECASE)

# ⚡ O NOVO CAÇADOR DE NOMES: Pega Title Case, ALL CAPS e Mistos (Ex: Carol MENDES)
NAME_FALLBACK_REGEX = re.compile(
    r"\b(?:[A-ZÀ-Ÿ][a-zà-ÿ]+|[A-ZÀ-Ÿ]{2,})"                # Primeiro nome (Ex: Joao, CAROL, MARIA)
    r"(?:\s+(?:de|da|do|dos|das|e|DE|DA|DO|DOS|DAS|E))?"   # Conectivo opcional
    r"\s+(?:[A-ZÀ-Ÿ][a-zà-ÿ]+|[A-ZÀ-Ÿ]{2,})"               # Segundo nome
    r"(?:\s+(?:[A-ZÀ-Ÿ][a-zà-ÿ]+|[A-ZÀ-Ÿ]{2,}))*\b"        # Outros sobrenomes
)

# =========================================================
# 🤖 OLLAMA: O JUIZ BINÁRIO (SIM / NÃO)
# =========================================================
@lru_cache(maxsize=100000)
def _is_valid_entity_ollama(text: str, tag: str) -> bool:
    """Usa o Ollama para confirmar se uma string duvidosa é realmente o que parece ser."""
    if len(text.strip()) < 3: 
        return False
        
    if tag == "PER":
        pergunta = f"A expressão '{text}' é o NOME PRÓPRIO de uma pessoa humana real?"
    elif tag == "GENERIC_CODE":
        pergunta = f"A expressão '{text}' parece ser um código de identificação, chassi, protocolo ou senha sensível?"
    elif tag == "TEXTO_LIVRE":
        pergunta = f"O texto '{text}' contém dados sensíveis (nomes, documentos, endereços)?"
    else:
        return True 

    prompt = (
        f"{pergunta}\n"
        f"Responda APENAS com a palavra 'SIM' ou 'NAO', sem pontuação e sem nenhuma explicação."
    )
    
    logger.debug(f"Julgando [{tag}] -> '{text}'...")
    try:
        payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0, "num_predict": 5}}
        resp = requests.post(OLLAMA_URL, json=payload, timeout=5, proxies={"http": "", "https": ""})
        
        if resp.status_code == 200:
            answer = resp.json().get("response", "").strip().upper()
            logger.debug(f"Ollama respondeu: {answer}")
            return "SIM" in answer
    except requests.exceptions.RequestException as e:
        logger.warning(f"Timeout/Erro no Ollama. Usando fallback (False). Erro: {e}")
        
    return False

# =========================================================
# MOTOR DE EXTRAÇÃO E CLASSIFICAÇÃO
# =========================================================
@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper().strip()

def _is_hallucination_basic(ent_text: str) -> bool:
    ent_text = ent_text.strip(".,;:-\n '\"")
    if not ent_text or len(ent_text) <= 2: return True
    if any(c.isdigit() for c in ent_text) and any(c in ['.', ',', '-'] for c in ent_text): return True
    
    termos_proibidos = {"CPF", "RG", "CNPJ", "CEP", "DOC", "DOCUMENTO", "TEL", "CEL", "TELEFONE", "CELULAR", "PIX", "CHAVE", "PLACA", "DR", "DRA", "SR", "SRA", "SISTEMA", "RELATORIO", "PROJETO"}
    texto_norm = _normalize(ent_text)
    if texto_norm in termos_proibidos or any(texto_norm.startswith(t + " ") for t in termos_proibidos): return True
    return False

def _detect_all(text: str, anon_loc: bool):
    found = []
    
    PRIORITY = { "EMAIL": 1, "IP": 1, "CPF": 1, "CHASSI": 1, "RG": 2, "PHONE": 2, "PLATE": 2, "COORD": 3, "COORD_SINGLE": 3, "PER": 4, "GENERIC_CODE": 99 }
    
    # 1. Busca por Regex (Documentos, Emails, GPS)
    for typ, pat in REGEX.items():
        if not anon_loc and typ in ["COORD", "COORD_SINGLE"]: 
            continue
            
        for match in pat.finditer(text):
            val = match.group()
            
            if typ == "GENERIC_CODE":
                if not any(c.isdigit() for c in val) or len(val) < 6: 
                    continue
                if not _is_valid_entity_ollama(val, "GENERIC_CODE"):
                    continue
                    
            logger.debug(f"🎯 Encontrado via REGEX: {val} [{typ}]")
            found.append((match.start(), match.end(), val, typ))

    # 2. Busca de Nomes por Contexto
    for match in CONTEXT_NAME_REGEX.finditer(text):
        val = match.group(2).strip()
        if not _is_hallucination_basic(val):
            if _is_valid_entity_ollama(val, "PER"):
                logger.debug(f"🎯 Nome validado via Contexto: {val} [PER]")
                found.append((match.start(2), match.end(2), text[match.start(2):match.end(2)], "PER"))

    # 3. Busca Inteligente (spaCy) - com blindagem de casing
    if nlp:
        # Se for tudo minúsculo OU a maioria maiúsculo, convertemos em TitleCase pro spaCy ler direito
        is_bad_casing = sum(1 for c in text if c.isupper()) > len(text) * 0.5 or text.islower()
        doc = nlp(text.title() if is_bad_casing else text)
        for ent in doc.ents:
            if ent.label_ == "PER":
                if any(token.pos_ in ["VERB", "PRON", "PUNCT", "SYM"] for token in ent): continue
                
                prefix_match = PREFIX_TRIMMER.search(ent.text)
                offset = prefix_match.end() if prefix_match else 0
                val_raw = ent.text[offset:]
                val_clean = val_raw.strip()
                start_adj = val_raw.find(val_clean)
                start = ent.start_char + offset + (start_adj if start_adj != -1 else 0)
                texto_original = text[start : start + len(val_clean)] # Pega o texto bruto como foi digitado
                
                if not _is_hallucination_basic(texto_original): 
                    if _is_valid_entity_ollama(texto_original, "PER"):
                        logger.debug(f"🎯 Nome validado via spaCy: {texto_original} [PER]")
                        found.append((start, start + len(val_clean), texto_original, "PER"))

    # 4. Busca de Nomes "Pega-Tudo" (A nova Rede de Segurança para caixas variadas)
    for match in NAME_FALLBACK_REGEX.finditer(text):
        val = match.group().strip()
        if not _is_hallucination_basic(val):
            # Pergunta ao juiz, pois a Regex pode ter pego uma cidade (ex: São Paulo) acidentalmente
            if _is_valid_entity_ollama(val, "PER"):
                logger.debug(f"🎯 Nome validado via Regex de Fallback: {val} [PER]")
                found.append((match.start(), match.end(), val, "PER"))

    # Ordenação e fusão dos fragmentos. Pedaços maiores ou de maior prioridade engolem os menores.
    found.sort(key=lambda x: (x[0], -(x[1] - x[0]), PRIORITY.get(x[3], 50)))
    
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

    fake.seed_instance(int(hashlib.sha256(norm_val.encode()).hexdigest()[:8], 16))
    
    if typ in ["PER", "NOME_SOLTO"]: val = fake.name().upper()
    elif typ == "CPF": val = fake.cpf()
    elif typ == "RG": val = fake.numerify('##.###.###-#') 
    elif typ in ["PLATE", "PLACA"]: val = fake.license_plate().upper()
    elif typ == "EMAIL": val = fake.email().lower()
    elif typ == "PHONE": val = fake.phone_number()
    elif typ == "IP": val = fake.ipv4()
    elif typ == "CHASSI": val = "".join(random.choices("ABCDEFGHJKLMNPRSTUVWXYZ0123456789", k=17))
    elif typ == "GENERIC_CODE": 
        val = "".join([random.choice(string.digits) if c.isdigit() else (random.choice(string.ascii_uppercase) if c.isalpha() else c) for c in clean_value])
    else: val = fake.word().upper()

    logger.debug(f"🔄 Troca gerada: '{value}' -> '{val}'")
    _MAPPING_CACHE[cache_key] = val
    return val

# =========================================================
# ORQUESTRAÇÃO FINAL
# =========================================================
def anonymize_value(col_name: str, val, anon_location: bool = True):
    try:
        if val is None or not str(val).strip(): return val, None
        text = str(val)
        
        if len(text) < 3: 
            return text, None
        
        entities = _detect_all(text, anon_location)
        if not entities: 
            return text, None
        
        result, last = [], 0
        for s, e, v, t in entities:
            if s < last: continue
            result.append(text[last:s])
            result.append(_get_fake(v, t))
            last = e
        result.append(text[last:])
        texto_final = "".join(result)
        
        return texto_final, ("TEXT" if texto_final != text else None)
    except Exception as e:
        logger.error(f"⚠️ Erro ao aplicar mascara na coluna '{col_name}': {e}", exc_info=True)
        return str(val), None 

def reset_memory():
    _MAPPING_CACHE.clear()