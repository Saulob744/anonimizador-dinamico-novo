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
# CONFIGURAÇÃO
# =========================================================
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s')
logger = logging.getLogger(__name__)

try:
    import spacy
    nlp = spacy.load("pt_core_news_lg")
    logger.debug("spaCy carregado com sucesso.")
except OSError:
    logger.warning("spaCy não encontrado. A detecção dependerá apenas de Regex.")
    nlp = None

_MAPPING_CACHE = {}
_COLUMN_POLICIES = {} 
_OLLAMA_CACHE = {} 
_DYNAMIC_SAMPLES = {} 

fake = Faker("pt_BR")

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "llama3:latest"

# =========================================================
# REGEX ESTRUTURAIS
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
    "GENERIC_CODE": re.compile(r'\b[A-Za-z0-9_/\.\-]{6,64}\b'),
}

CONTEXT_NAME_REGEX = re.compile(r"\b(nome|cliente|paciente|sr|sra|dr|dra|atendente|consultor|gerente|responsavel|usuario|agente|vendedor|motorista|funcionario|titular|favorecido)\s*[:\-]?\s+([A-ZÀ-Ÿa-zà-ÿ]{2,20}(?:\s+[A-ZÀ-Ÿa-zà-ÿ]{2,20}){0,4})\b", re.IGNORECASE)
PREFIX_TRIMMER = re.compile(r"^(ao|a|para|dr\.?|dra\.?|sr\.?|sra\.?|senhor|senhora|em|na|no|de|do|da|vitima|autor|paciente|soldado|policial|rua|avenida|trevo|cia|agente|vendedor|motorista|funcionario)\s+", re.IGNORECASE)

NAME_FALLBACK_REGEX = re.compile(
    r"\b(?:[A-ZÀ-Ÿ][a-zà-ÿ]+|[A-ZÀ-Ÿ]{2,})"                
    r"(?:\s+(?:de|da|do|dos|das|e|DE|DA|DO|DOS|DAS|E))?"   
    r"\s+(?:[A-ZÀ-Ÿ][a-zà-ÿ]+|[A-ZÀ-Ÿ]{2,})"               
    r"(?:\s+(?:[A-ZÀ-Ÿ][a-zà-ÿ]+|[A-ZÀ-Ÿ]{2,}))*\b(?!\:)"        
)

# =========================================================
# 1. O JUIZ BINÁRIO (OLLAMA SIM/NÃO INDIVIDUAL)
# =========================================================
def _is_valid_entity_ollama(text: str, tag: str) -> bool:
    """O Ollama atua como juiz para textos suspeitos, avaliando SIM ou NÃO."""
    if len(text.strip()) < 3: return False
    
    # Memória RAM para não julgar a mesma palavra duas vezes
    cache_key = f"{tag}:{text.upper()}"
    if cache_key in _OLLAMA_CACHE:
        return _OLLAMA_CACHE[cache_key]
        
    if tag == "PER":
        pergunta = f"A expressão '{text}' é o NOME PRÓPRIO de uma pessoa humana real?"
    elif tag == "GENERIC_CODE":
        pergunta = f"A expressão '{text}' parece ser um código de identificação, chassi ou senha sensível?"
    else:
        return True 

    prompt = f"{pergunta}\nResponda APENAS com a palavra 'SIM' ou 'NAO', sem pontuação e sem explicação."
    
    logger.debug(f"🧠 IA Julgando [{tag}] -> '{text}'...")
    try:
        payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0, "num_predict": 5}}
        resp = requests.post(OLLAMA_URL, json=payload, timeout=8, proxies={"http": "", "https": ""})
        
        if resp.status_code == 200:
            answer = resp.json().get("response", "").strip().upper()
            is_valid = "SIM" in answer
            _OLLAMA_CACHE[cache_key] = is_valid 
            logger.debug(f"Veredito: {is_valid}")
            return is_valid
    except Exception as e:
        logger.warning(f"Timeout no Ollama para '{text}'. Erro: {e}")
        
    return False

# =========================================================
# 2. PERFILAMENTO E AMOSTRAGEM DE COLUNA
# =========================================================
def _classify_single_value(text: str) -> str:
    text = str(text).strip()
    words = text.split()
    
    if not text or len(text) < 3: return "IGNORAR"
    if len(words) >= 3 or len(text) > 40: return "TEXTO_LIVRE"
        
    for typ, pat in REGEX.items():
        if pat.match(text): return typ
            
    if any(c.isalpha() for c in text) and any(c.isdigit() for c in text) and len(text) >= 6:
        return "GENERIC_CODE"
        
    return "DESCONHECIDO"

def _ollama_fallback_check(amostra: list) -> str:
    prompt = (
        f"Analise esta amostra de dados: {amostra}\n"
        f"Existe a presença de dados sensíveis ou informações pessoais (nomes próprios, documentos, contatos, chaves, senhas)?\n"
        f"Responda APENAS com a palavra 'SIM' ou 'NAO'."
    )
    logger.debug(f"🧠 Acionando Ollama Fallback para amostra indecisa de coluna: {amostra[:3]}")
    try:
        payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0, "num_predict": 5}}
        resp = requests.post(OLLAMA_URL, json=payload, timeout=5, proxies={"http": "", "https": ""})
        if resp.status_code == 200:
            if "SIM" in resp.json().get("response", "").strip().upper(): return "TEXTO_LIVRE" 
    except Exception as e:
        return "TEXTO_LIVRE"
    return "IGNORAR" 

def define_column_policy(col_name: str, sample_values: list) -> str:
    if col_name in _COLUMN_POLICIES: return _COLUMN_POLICIES[col_name]
        
    valid_samples = [str(v).strip() for v in sample_values if v is not None and str(v).strip() != ""]
    if not valid_samples:
        _COLUMN_POLICIES[col_name] = "IGNORAR"
        return "IGNORAR"

    # Usa 30 amostras para votação robusta
    amostras_teste = random.sample(valid_samples, min(30, len(valid_samples)))
    resultados = [_classify_single_value(v) for v in amostras_teste]
    votos = Counter(resultados)
    
    logger.debug(f"📊 Votação da coluna '{col_name}': {votos}")
    
    votos_claros = {k: v for k, v in votos.items() if k not in ["DESCONHECIDO", "IGNORAR"]}
    
    if votos_claros:
        if "TEXTO_LIVRE" in votos_claros: politica = "TEXTO_LIVRE"
        else: politica = max(votos_claros, key=votos_claros.get)
    else:
        politica = _ollama_fallback_check(amostras_teste)
        
    logger.info(f"✅ Política Definida para '{col_name}': {politica}")
    _COLUMN_POLICIES[col_name] = politica
    return politica

# =========================================================
# 3. MOTOR DE TEXTO LIVRE (Frases)
# =========================================================
@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper().strip()

def _is_hallucination_basic(ent_text: str) -> bool:
    """Bloqueio pesado contra falsos positivos (ex: PGTO EFETUADO)"""
    ent_text = ent_text.strip(".,;:-\n '\"")
    if not ent_text or len(ent_text) <= 2: return True
    if any(c.isdigit() for c in ent_text) and any(c in ['.', ',', '-'] for c in ent_text): return True
    
    termos_proibidos = {
        "CPF", "RG", "CNPJ", "CEP", "DOC", "DOCUMENTO", "TEL", "CEL", "TELEFONE", 
        "CELULAR", "PIX", "CHAVE", "PLACA", "DR", "DRA", "SR", "SRA", "SISTEMA", 
        "RELATORIO", "PROJETO", "PGTO", "PAGAMENTO", "EFETUADO", "TRANSFERENCIA", 
        "ESTORNO", "DEVOLUCAO", "VALOR", "TARIFA", "TAXA", "VIATURA", "VEICULO"
    }
    texto_norm = _normalize(ent_text)
    partes = texto_norm.split()
    
    if all(p in termos_proibidos for p in partes): return True
    if texto_norm in termos_proibidos or any(texto_norm.startswith(t + " ") for t in termos_proibidos): return True
    return False

def _detect_all(text: str, anon_loc: bool):
    found = []
    PRIORITY = { "EMAIL": 1, "IP": 1, "CPF": 1, "CHASSI": 1, "RG": 2, "PHONE": 2, "PLATE": 2, "COORD": 3, "COORD_SINGLE": 3, "PER": 4, "GENERIC_CODE": 99 }
    
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
                    
            found.append((match.start(), match.end(), val, typ))

    for match in CONTEXT_NAME_REGEX.finditer(text):
        val = match.group(2).strip()
        if not _is_hallucination_basic(val):
            if _is_valid_entity_ollama(val, "PER"):
                found.append((match.start(2), match.end(2), text[match.start(2):match.end(2)], "PER"))

    if nlp:
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
                texto_original = text[start : start + len(val_clean)] 
                
                if not _is_hallucination_basic(texto_original): 
                    if _is_valid_entity_ollama(texto_original, "PER"):
                        found.append((start, start + len(val_clean), texto_original, "PER"))

    for match in NAME_FALLBACK_REGEX.finditer(text):
        val = match.group().strip()
        if not _is_hallucination_basic(val):
            if _is_valid_entity_ollama(val, "PER"):
                found.append((match.start(), match.end(), val, "PER"))

    found.sort(key=lambda x: (x[0], -(x[1] - x[0]), PRIORITY.get(x[3], 50)))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, text[s:e], t))
            last = e
    return clean

# =========================================================
# 4. GERAÇÃO DE FAKES E GPS JITTER
# =========================================================
def apply_gps_jitter(coord_str):
    try:
        c = float(coord_str.replace(',', '.'))
        return f"{c + random.uniform(-0.003, 0.003):.6f}"
    except ValueError:
        return coord_str

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
    elif typ in ["COORD", "COORD_SINGLE"]: 
        val = re.sub(r"-?\d{1,3}[.,]\d+", lambda m: apply_gps_jitter(m.group(0)), value)
    elif typ == "GENERIC_CODE": 
        val = "".join([random.choice(string.digits) if c.isdigit() else (random.choice(string.ascii_uppercase) if c.isalpha() else c) for c in clean_value])
    else: val = fake.word().upper()

    _MAPPING_CACHE[cache_key] = val
    return val

# =========================================================
# 5. ORQUESTRAÇÃO FINAL
# =========================================================
def anonymize_value(col_name: str, val, anon_location: bool = True):
    try:
        if val is None or not str(val).strip(): return val, None
        text = str(val)
        
        if len(text) < 3: 
            return text, None
            
        # --- PERFILAMENTO ---
        if col_name not in _COLUMN_POLICIES:
            if col_name not in _DYNAMIC_SAMPLES:
                _DYNAMIC_SAMPLES[col_name] = []
            
            _DYNAMIC_SAMPLES[col_name].append(text)
            
            # Testa a amostra após 30 linhas
            if len(_DYNAMIC_SAMPLES[col_name]) >= 30 or len(text) > 50:
                define_column_policy(col_name, _DYNAMIC_SAMPLES[col_name])
            else:
                politica = "TEXTO_LIVRE" 
        else:
            politica = _COLUMN_POLICIES[col_name]

        # --- ROTEAMENTO EXECUTOR ---
        if politica == "IGNORAR":
            return text, None
            
        if politica in ["CPF", "RG", "EMAIL", "PLATE", "PHONE", "IP", "CHASSI", "GENERIC_CODE", "NOME_SOLTO"]:
            fake_val = _get_fake(text, politica)
            return fake_val, ("TEXT" if fake_val != text else None)
            
        if politica == "TEXTO_LIVRE":
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
    _COLUMN_POLICIES.clear()
    _OLLAMA_CACHE.clear()
    _DYNAMIC_SAMPLES.clear()