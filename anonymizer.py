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
from collections import Counter
# =========================================================
# CONFIGURAÇÕES GLOBAIS & LOGS
# =========================================================
# ⚡ AJUSTE: Nível mantido em ERROR para silenciar ruídos, 
# mas agora NENHUM erro derrubará o pipeline.
logging.basicConfig(level=logging.ERROR, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

try:
    import spacy
    nlp = spacy.load("pt_core_news_lg")
except OSError:
    # Fallback caso o SpaCy não esteja instalado.
    logger.warning("Modelo SpaCy não encontrado. Rodando em modo de contingência (apenas Regex).")
    nlp = None

_MAPPING_CACHE = {}
fake = Faker("pt_BR")

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "llama3"

# =========================================================
# REGEX ESTRUTURAIS (ULTRA-FLEXÍVEIS)
# =========================================================
REGEX = {
    # Aceita coordenadas com ponto, vírgula, separadas por vírgula, ponto e vírgula ou espaço
    "COORD": re.compile(r"-?\d{1,2}[.,]\d+\s*[,;]?\s*-?\d{1,3}[.,]\d+"), 
    "COORD_SINGLE": re.compile(r"^-?\d{1,3}[.,]\d{4,}$|^-\d{5,10}$"), 
    "EMAIL": re.compile(r"[\w\.-]+@[\w\.-]+", re.IGNORECASE),
    # CPF hiper maleável: 11 dígitos seguidos ou com qualquer combinação de pontos, traços e espaços
    "CPF": re.compile(r"\b\d{3}[.\-\s]?\d{3}[.\-\s]?\d{3}[.\-\s]?\d{2}\b|\b\d{11}\b"),
    # Placas antigas e Mercosul com ou sem traço/espaço
    "PLATE": re.compile(r"\b[A-Z]{3}[-\s]?\d[A-Z0-9]\d{2}\b|\b[A-Z]{3}[-\s]?\d{4}\b", re.IGNORECASE),
    # RG flexível: aceita com UF na frente (SP-...), com pontos, ou uma tripa de números de 5 a 14 dígitos
    "RG": re.compile(r"\b(?:[A-Z]{2}[-\s]?)?\d{1,3}[.\-\s]?\d{3}[.\-\s]?\d{3}[-\s]?[0-9A-Z]\b|\b\d{5,14}\b", re.IGNORECASE),
    # Telefone com/sem DDI, com/sem DDD, com/sem formatacao
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2,3}\)?[\s-]?)?\d{4,5}[-\s]?\d{4}\b"),
}


NAME_REGEX = re.compile(
    r"\b(?:[A-ZÀ-Ÿ][a-zà-ÿ]{1,20}|[A-ZÀ-Ÿ]{2,20})" # Primeiro nome
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
        # 🛡️ ANTI-QUEBRA: Se a IA estiver offline ou der timeout, retorna False e segue a vida.
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
        
        # 1. BLACKLIST (O que ignorar imediatamente)
        blacklist = {
            "cidade", "municipio", "bairro", "estado", "uf", "pais", "cep", 
            "papel", "prefixo", "status", "situacao", "tipo", "cor", "marca", 
            "modelo", "id", "uuid", "guid", "created_at", "updated_at"
        }
        if c in blacklist or "id" in c_words or c.endswith("id"):
            return "IGNORAR"
            
        # 2. HEURÍSTICA DE NOME DE COLUNA (Detecção rápida)
        if any(k in c_words for k in ["pix", "chave"]): return "TEXTO_LIVRE"
        if any(k in c_words for k in ["lat", "latitude", "lon", "longitude", "gps", "coord", "coordenada", "geo", "loc"]): return "GPS_SINGLE"
        if any(k in c_words for k in ["telefone", "celular", "fone"]): return "PHONE"
        if any(k in c_words for k in ["nome", "vitima", "autor", "condutor", "proprietario"]): return "NOME_SOLTO"
        
        if "cpf" in c_words or "cpf" in c: return "CPF"
        if "rg" in c_words or c == "rg": return "RG"
        if "placa" in c_words or "placa" in c: return "PLACA"
        if "email" in c_words or "mail" in c_words or "email" in c: return "EMAIL"
        if "renavam" in c_words or "renavam" in c: return "RENAVAM"
        if "matricula" in c_words or "matricula" in c: return "MATRICULA"

        # 3. ANÁLISE DE AMOSTRA (Votação baseada em conteúdo)
        valid_strings = [str(v) for v in values_sample if v is not None and str(v).strip() != ""]
        if not valid_strings: 
            return "IGNORAR"
            
        sample_to_test = random.sample(valid_strings, min(5, len(valid_strings)))
        resultados = [classify_cell(text) for text in sample_to_test]
        
        votos = Counter(resultados)
        hits_validos = {k: v for k, v in votos.items() if k not in ["IGNORAR", "DESCONHECIDO"]}
        
        if hits_validos:
            if "TEXTO_LIVRE" in hits_validos: 
                return "TEXTO_LIVRE"
            return max(hits_validos, key=hits_validos.get)
            
        # 4. SALVAGUARDA DA IA (Última linha de defesa para não deixar vazar)
        amostra_str = " | ".join(sample_to_test)
        prompt_salvacao = (
            f"Você é um auditor rigoroso de LGPD. Analise esta amostra de dados: '{amostra_str}'. "
            f"Existe ALGUM dado sensível aqui (como nomes de pessoas, CPFs, contas ou contatos)? "
            f"Responda APENAS 'SIM' ou 'NAO'."
        )
        
        try:
            # Proxies vazios e timeout para não travar o pipeline se a IA demorar
            resp = requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": prompt_salvacao, "stream": False}, timeout=20, proxies={"http": "", "https": ""})
            if resp.status_code == 200:
                answer = resp.json().get("response", "").strip().upper()
                if "SIM" in answer or "YES" in answer:
                    logger.warning(f"🚨 SALVAGUARDA ATIVADA: IA pegou dados sensíveis na coluna '{col_name}'. Forçando TEXTO_LIVRE.")
                    return "TEXTO_LIVRE"
        except Exception as e_ia:
            logger.debug(f"Falha na IA de salvaguarda (Ignorado): {e_ia}")

        return "DESCONHECIDO"

    except Exception as e:
        # 🛡️ ANTI-QUEBRA
        logger.error(f"⚠️ [IGNORADO] Colapso no Profiler na coluna '{col_name}'. Motivo: {e}")
        return "IGNORAR"

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
    elif typ in ["COORD", "COORD_SINGLE", "GPS", "GPS_SINGLE"]: 
        val = f"{fake.latitude()}, {fake.longitude()}"
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
        # 🛡️ ANTI-QUEBRA: Se der erro na manipulação da string, não levanta exceção.
        # Ele avisa no log e retorna o valor original, garantindo que o banco receba o dado intacto.
        logger.error(f"⚠️ [IGNORADO] Falha ao mascarar texto livre na coluna '{col_name}'. Mantendo original. Motivo: {e}")
        return str(val), None 

def reset_memory():
    _MAPPING_CACHE.clear()