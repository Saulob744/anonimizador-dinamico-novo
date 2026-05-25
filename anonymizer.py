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

# =========================================================
# CONFIGURAÇÃO INICIAL
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
_NEGATIVE_PATTERN_CACHE = set()

fake = Faker("pt_BR")

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "llama3:latest"

# =========================================================
# REGEX ESTRUTURAIS 
# =========================================================

REGEX = {
    "COORD": re.compile(r"-?\d{1,2}[.,]\d+\s*[,;]?\s*-?\d{1,3}[.,]\d+"),
    "DATA_TIME": re.compile(
        r"\b\d{2,4}[/\-]\d{2}[/\-]\d{2,4}(?:\s+\d{2}:\d{2}(?:\:\d{2})?)?\b|" 
        r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b|"              
        r"\b\d{2}:\d{2}(?:\:\d{2})?\b|"                                        
        r"\b(?:19|20)\d{2,4}\b"                                              
    ),    
    "COORD_SINGLE": re.compile(r"^-?\d{1,3}[.,]\d{4,}$|^-\d{5,10}$"),
    "ID_NUMERICO": re.compile(r"^-?\d+$"),
    "NUMERO_DECIMAL": re.compile(r"^-?\d+[.,]\d+$"),
    "EMAIL": re.compile(r"[\w\.-]+@[\w\.-]+\.\w+", re.IGNORECASE),
    # Regex de CPF mais flexível ignorando caracteres não numéricos ao redor
    "CPF": re.compile(r"(?:\D|^)(\d{3}[.\-\s]*\d{3}[.\-\s]*\d{3}[.\-\s]*\d{2})(?:\D|$)"),
    "RG": re.compile(r"(?:\D|^)(?:[A-Z]{2}[-\s]*)?\d{1,3}[.\-\s]*\d{3}[.\-\s]*\d{3}[-\s]*[0-9A-Z](?:\D|$)"),
    "PLATE": re.compile(r"\b[A-Z]{3}[-\s]?\d[A-Z0-9]\d{2}\b|\b[A-Z]{3}[-\s]?\d{4}\b", re.IGNORECASE),
    "PHONE": re.compile(r"(?:\D|^)(?:\+?55\s*)?(?:\(?\d{2,3}\)?[\s-]*)?\d{4,5}[-\s]*\d{4}(?:\D|$)"),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[a-fA-F0-9]{1,4}:){7}[a-fA-F0-9]{1,4}\b"),
    "CHASSI": re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE), 
    "GENERIC_CODE": re.compile(r'\b[A-Za-z0-9_/\.\-]{6,64}\b'),
}

NAME_FALLBACK_REGEX = re.compile(
    r"\b[A-ZÀ-Ÿ][a-zà-ÿ]+" 
    r"(?:\s+(?:de|da|do|dos|das|e))?"      
    r"(?:\s+[A-ZÀ-Ÿ][a-zà-ÿ]+){1,5}\b"      
)

def _get_skeleton(text: str) -> str:
    return re.sub(r'\d+', '[NUM]', text.upper())

# =========================================================
# 1. MOTOR DE COMUNICAÇÃO COM A IA
# =========================================================
def _ask_ollama_sim_nao(pergunta: str, cache_key: str) -> bool:

    if cache_key in _OLLAMA_CACHE: 
        return _OLLAMA_CACHE[cache_key]
    
    termo_avaliado = cache_key.split(":")[-1] 
    esqueleto = _get_skeleton(termo_avaliado)
    
    if esqueleto in _NEGATIVE_PATTERN_CACHE:
        return False
    
    # Instrução forte e restritiva para a IA
    prompt = f"{pergunta} Responda APENAS 'SIM' ou 'NAO'. Não use pontuação, nem explicações."
    logger.debug(f"🧠 IA Perguntando: {pergunta}")
    
    try:
        payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0}}
        resp = requests.post(OLLAMA_URL, json=payload, timeout=40)
        
        if resp.status_code == 200:
            resposta_ia = resp.json().get("response", "").strip().upper()
            is_valid = "SIM" in resposta_ia
            
            _OLLAMA_CACHE[cache_key] = is_valid 
            
            if not is_valid:
                _NEGATIVE_PATTERN_CACHE.add(esqueleto)
                
            return is_valid
    except Exception as e:
        logger.warning(f"Erro no Ollama: {e}. Retornando Falso por segurança.")
        
    return False

# =========================================================
# 2. INTELIGÊNCIA DE PERFILAMENTO DA COLUNA 
# =========================================================
def define_column_policy(col_name: str, samples: list) -> str:
    """
    Avalia a coluna com um sistema de pontuação e no máximo 2 perguntas para a IA.
    1 ponto para Sensibilidade + 1 ponto para Padrão Regex.
    """
    if col_name in _COLUMN_POLICIES:
        return _COLUMN_POLICIES[col_name]

    valid_samples = [str(s).strip() for s in samples if str(s).strip()]
    if not valid_samples:
        return "IGNORAR"

    amostra_conjunta = " | ".join(valid_samples[:10])

    # Inicia a nossa balança de pontos
    pontos_sensivel = 0
    pontos_padrao = 0
    melhor_tipo_regex = None

    # ---------------------------------------------------------
    # PERGUNTA 1: Avaliação de Sensibilidade 
    # ---------------------------------------------------------
    pergunta_1 = f"Avalie as amostras '{amostra_conjunta}'. Elas contêm informações pessoais sensíveis (como CPFs, emails, nomes de pessoas, telefones)? Responda apenas SIM ou NAO."
    
    is_sensitive = _ask_ollama_sim_nao(pergunta_1, f"SENSIVEL:{col_name}")

    if is_sensitive:
        pontos_sensivel = 1
        logger.debug(f"⚖️ Balança: Coluna '{col_name}' ganhou 1 ponto de sensibilidade.")
    else:
        logger.debug(f"⚖️ Balança: IA disse NÃO para sensibilidade. Ignorando a coluna '{col_name}'.")
        _COLUMN_POLICIES[col_name] = "IGNORAR"
        return "IGNORAR"

    # ---------------------------------------------------------
    # AVALIAÇÃO DE REGEX 
    # ---------------------------------------------------------
    placar_regex = {key: 0 for key in REGEX.keys()}
    
    for amostra in valid_samples[:10]:
        for typ, pat in REGEX.items():
            if pat.search(amostra):
                placar_regex[typ] += 1

   
    if placar_regex:
        melhor_tipo_regex = max(placar_regex, key=placar_regex.get)
        if placar_regex[melhor_tipo_regex] > 0:
            pontos_padrao = 1
            logger.debug(f"⚖️ Balança: Coluna ganhou 1 ponto pelo Regex formato '{melhor_tipo_regex}'.")

    # ---------------------------------------------------------
    # PERGUNTA 2:
    # ---------------------------------------------------------
    if pontos_sensivel == 1 and pontos_padrao == 1:
        # Se achou algo que não consideramos sensível (como um ID de sistema puro), podemos ignorar
        if melhor_tipo_regex in ["DATA_TIME", "ID_NUMERICO", "NUMERO_DECIMAL"]:
            logger.debug(f"⏩ Regex cravou dado numérico/temporal '{melhor_tipo_regex}'. Ignorando.")
            _COLUMN_POLICIES[col_name] = "IGNORAR"
            return "IGNORAR"

        pergunta_2 = f"Os dados sensíveis '{amostra_conjunta}' parecem ser predominantemente do tipo '{melhor_tipo_regex}'? Responda apenas SIM ou NAO."
        confirma_tipo = _ask_ollama_sim_nao(pergunta_2, f"CONFIRMA:{melhor_tipo_regex}:{col_name}")

        if confirma_tipo:
            logger.debug(f"✅ Classificação cravada pela IA: '{melhor_tipo_regex}'.")
            _COLUMN_POLICIES[col_name] = melhor_tipo_regex
            return melhor_tipo_regex
        else:
            logger.debug(f"⚠️ IA negou que o formato seja '{melhor_tipo_regex}'. Caindo para TEXTO_LIVRE.")

    # Fallback: Se é sensível (1 pt) mas não confirmou o formato Regex (0 pts na pergunta 2)
    _COLUMN_POLICIES[col_name] = "TEXTO_LIVRE"
    return "TEXTO_LIVRE"

# =========================================================
# FUNÇÕES DE APOIO E GERAÇÃO DE DADOS 
# =========================================================
@lru_cache(maxsize=1000)
def _normalize(text: str) -> str:
    if not text: return ""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper().strip()

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

    try:
        seed_hex = hashlib.sha256(norm_val.encode()).hexdigest()[:8]
        fake.seed_instance(int(seed_hex, 16))
    except Exception:
        fake.seed_instance(random.randint(1, 99999))
    
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
    else: val = fake.word().upper()

    _MAPPING_CACHE[cache_key] = val
    return val

# =========================================================
# ORQUESTRAÇÃO FINAL
# =========================================================
def anonymize_value(col_name: str, val, anon_location: bool = True):
    try:
        if val is None: return val, None
        text = str(val).strip()
        
        # Ignora campos muito curtos ou que sejam puramente ID/Data
        if len(text) < 3 or REGEX["DATA_TIME"].fullmatch(text) or REGEX["ID_NUMERICO"].fullmatch(text): 
            return text, None
            
        # Aciona a nova inteligência de pontuação
        politica = define_column_policy(col_name, [text])

        if politica == "IGNORAR":
            return text, None
            
        # Se cravou um formato específico, gera o Fake direto
        if politica in ["CPF", "RG", "EMAIL", "PLATE", "PHONE", "IP", "CHASSI", "GENERIC_CODE", "COORD", "COORD_SINGLE"]:
            fake_val = _get_fake(text, politica)
            return fake_val, ("TEXT" if fake_val != text else None)
            
        # Se for TEXTO_LIVRE, você pode implementar a busca de entidades soltas aqui no futuro
        # Por simplificação, se ele não cravou um regex, e é sensível, substituímos por um nome falso base.
        fake_val_texto = _get_fake(text, "PER")
        return fake_val_texto, "TEXT"

    except Exception as e:
        logger.error(f"⚠️ Erro na coluna '{col_name}': {e}")
        return str(val), None 

def reset_memory():
    _MAPPING_CACHE.clear()
    _COLUMN_POLICIES.clear()
    _OLLAMA_CACHE.clear()
    _NEGATIVE_PATTERN_CACHE.clear()
