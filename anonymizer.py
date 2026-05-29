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
from collections import Counter
from functools import lru_cache
from faker import Faker

# =========================================================
# CONFIGURAÇÃO E SEGURANÇA 
# =========================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s')
logger = logging.getLogger(__name__)

SECRET_SALT = os.getenv("ANONYMIZER_SECRET_SALT")
if not SECRET_SALT or len(SECRET_SALT) < 16:
    logger.warning("⚠️ ANONYMIZER_SECRET_SALT ausente/fraco. Risco de segurança em produção.")
    SECRET_SALT = "SaltDeEmergenciaApenasParaDesenvolvimento123!"

try:
    import spacy
    nlp = spacy.load("pt_core_news_lg")
except OSError:
    logger.warning("spaCy não encontrado. Fallback apenas para Regex e LLM.")
    nlp = None

_MAPPING_CACHE = {}
_COLUMN_POLICIES = {} 
_OLLAMA_CACHE = {} 
_NEGATIVE_PATTERN_CACHE = set()

fake = Faker("pt_BR")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3:latest")

# =========================================================
# REGEX ESTRUTURAIS
# =========================================================
REGEX = {
    "DATE_TIME": re.compile(
        r"\b\d{2,4}[/\-]\d{2}[/\-]\d{2,4}(?:\s+\d{2}:\d{2}(?:\:\d{2})?)?\b|"
        r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b|"
        r"\b\d{2}:\d{2}(?:\:\d{2})?\b"
    ),
    "COORD": re.compile(r"-?\d{1,3}[.,]\d+\s*[,;]?\s*-?\d{1,3}[.,]\d+"), 
    "COORD_SINGLE": re.compile(r"(?<!\d)-?\d{1,3}[.,]\d{4,}(?!\d)|(?<!\d)-\d{5,10}(?!\d)"), 
    "EMAIL": re.compile(r"[\w\.-]+@[\w\.-]+\.\w+", re.IGNORECASE),
    "CPF": re.compile(r"\b\d{3}[.\-\s/_]*\d{3}[.\-\s/_]*\d{3}[.\-\s/_]*\d{2}\b|\b\d{11}\b"),
    "RG": re.compile(r"\b(?:[A-Za-z]{2}[-\s]*)?\d{1,3}[.\-\s]*\d{3}[.\-\s]*\d{3}[-\s]*[0-9A-Za-z]\b|\b\d{5,14}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}[-\s]*\d[A-Z0-9]\d{2}\b|\b[A-Z]{3}[-\s]*\d{4}\b", re.IGNORECASE),
    "PHONE": re.compile(r"\b(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{2,3}\)?[\s-]?)?\d{4,5}[\s-]?\d{4}\b"),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[a-fA-F0-9]{1,4}:){7}[a-fA-F0-9]{1,4}\b"),
    "CHASSI": re.compile(r"\b(?:[A-HJ-NPR-Z0-9][-\s]*){17}\b", re.IGNORECASE), 
    "DOC_GENERICO": re.compile(r"\b(?:\d[.\-\s]*){7,15}\b"),
    "GENERIC_CODE": re.compile(r'\b(?:(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_/\.\-]{5,128})\b'),
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
# 1. MOTOR DE CLASSIFICAÇÃO LGPD
# =========================================================
class AegisClassifier:
    def __init__(self):
        self.FAST_TRACK_MAP = {
            "CPF": "CPF", "RG": "RG", "DOC_GENERICO": "DOC_GENERICO",
            "CARTAO_CREDITO": "CARTAO_CREDITO", "PLATE": "PLACA", 
            "EMAIL": "EMAIL", "PHONE": "PHONE", "CHASSI": "CHASSI",
            "IP": "IP", "GENERIC_CODE": "GENERIC_CODE",
            "COORD": "COORD", "COORD_SINGLE": "COORD_SINGLE",
            "DATE_TIME": "IGNORAR"
        }

    def _analyze_with_regex(self, samples: list) -> dict:
        scores = Counter()
        total = len(samples)
        if total == 0: return scores
        for s in samples:
            for tag, padrao in REGEX.items():
                if padrao.search(s):
                    scores[tag] += 1
        return {tag: (count/total)*100 for tag, count in scores.items()}

    def _analyze_with_spacy(self, samples: list) -> float:
        if not nlp: return 0.0
        per_count = sum(1 for s in samples if len(s) <= 200 and any(ent.label_ == "PER" for ent in nlp(s.title()).ents))
        return (per_count / len(samples)) * 100 if samples else 0.0

    def _ultimate_safety_fallback(self, col_name: str, amostras: list) -> str:
        prompt = (
            f"Você é a última linha de defesa de privacidade de dados (LGPD).\n"
            f"A coluna '{col_name}' não pôde ser classificada. Amostras: {' | '.join(amostras[:5])}\n\n"
            f"PERGUNTA FINAL:\n"
            f"Existe ALGUMA possibilidade destas amostras conterem informações pessoais, nomes, relatos ou documentos?\n"
            f"Responda EXATAMENTE com a palavra 'SIM' ou 'NAO'."
        )
        try:
            payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0, "num_predict": 5}}
            resp = requests.post(OLLAMA_URL, json=payload, timeout=28)
            if resp.status_code == 200:
                ans = resp.json().get("response", "").strip().upper()
                return "TEXTO_LIVRE" if "SIM" in ans else "IGNORAR"
        except Exception as e:
            logger.error(f"Erro no Fallback IA ({col_name}): {e}")
            
        return "TEXTO_LIVRE" 

    def get_column_tag(self, col_name: str, samples: list) -> str:
        amostras_limpas = [str(s).strip() for s in samples if s is not None and str(s).strip()]
        if not amostras_limpas: return "IGNORAR"
        amostras_unicas = list(set(amostras_limpas))[:15]

        max_words = max((len(s.split()) for s in amostras_unicas), default=0)
        if max_words >= 5 or any(len(s) > 60 for s in amostras_unicas):
            return "TEXTO_LIVRE"
            
        if nlp and sum(1 for s in amostras_unicas[:5] if any(t.pos_ in ["VERB", "AUX"] for t in nlp(s.lower()[:100]))) >= 1:
            return "TEXTO_LIVRE"

        col_lower = col_name.lower()
        if any(termo in col_lower for termo in {'cidade', 'municipio', 'estado', 'pais', 'uf', 'bairro', 'cep', 'status', 'tipo', 'categoria', 'marca', 'modelo', 'cor', 'produto', 'data'}):
            return "IGNORAR"

        regex_scores = self._analyze_with_regex(amostras_unicas)
        for regex_key, policy_tag in self.FAST_TRACK_MAP.items():
            if regex_scores.get(regex_key, 0) >= 70.0:
                return policy_tag

        if self._analyze_with_spacy(amostras_unicas) >= 70.0:
            return "NOME_SOLTO"

        return self._ultimate_safety_fallback(col_name, amostras_unicas)

_aegis_engine = AegisClassifier()

def define_column_policy(col_name: str, sample_values: list) -> str:
    if col_name not in _COLUMN_POLICIES: 
        _COLUMN_POLICIES[col_name] = _aegis_engine.get_column_tag(col_name, sample_values)
    return _COLUMN_POLICIES[col_name]

# =========================================================
# 2. VALIDACÃO DE ENTIDADES E EXTRAÇÃO DE TEXTO
# =========================================================
def _is_valid_entity_ollama(text: str, tag: str) -> bool:
    if len(text.strip()) < 3: return False
    
    cache_key = f"{tag}:{text.upper()}"
    if cache_key in _OLLAMA_CACHE: return _OLLAMA_CACHE[cache_key]
    
    esqueleto = re.sub(r'\d+', '[NUM]', text.upper())
    if esqueleto in _NEGATIVE_PATTERN_CACHE: return False
        
    if tag in ["PER", "NOME_SOLTO"]:
        prompt = f"""Atue como um filtro rigoroso de LGPD. A expressão '{text}' é o NOME PRÓPRIO de uma pessoa real?
Regras de REJEIÇÃO (Responda 'NAO' se for):
- Papéis, cargos ou status (Suspeito, Abordado, Vítima, Autor, Testemunha, Condutor)
- Marcações de tempo (Segunda-feira, Sábado, Janeiro)
- Nomes de locais, objetos ou jargões.
Responda APENAS 'SIM' ou 'NAO'."""
    else:
        return True
    
    try:
        payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0, "num_predict": 5}}
        resp = requests.post(OLLAMA_URL, json=payload, timeout=28, proxies={"http": "", "https": ""})
        if resp.status_code == 200:
            is_valid = "SIM" in resp.json().get("response", "").strip().upper()
            _OLLAMA_CACHE[cache_key] = is_valid 
            if not is_valid: _NEGATIVE_PATTERN_CACHE.add(esqueleto)
            return is_valid
    except:
        pass
        
    return False

@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper().strip()

def _is_hallucination_basic(ent_text: str) -> bool:
    ent_text = ent_text.strip(".,;:-\n '\"")
    if not ent_text or len(ent_text) <= 2: return True
    if any(c.isdigit() for c in ent_text) and any(c in ['.', ',', '-'] for c in ent_text): return True
    
    termos_proibidos = {"CPF", "RG", "CNPJ", "CEP", "DOC", "TELEFONE", "CELULAR", "PIX", "CHAVE", "PLACA", "DR", "DRA", "SR", "SRA", "VIATURA", "VEICULO", "CARRO", "MOTO", "MODELO", "MARCA", "CHEVROLET", "FIAT", "VOLKSWAGEN", "FORD", "HONDA", "ASSALTO", "ROUBO", "FURTO", "CRIME", "DELITO", "OCORRENCIA", "BO"}
    texto_norm = _normalize(ent_text)
    
    return all(p in termos_proibidos for p in texto_norm.split()) or any(texto_norm.startswith(t + " ") for t in termos_proibidos) or texto_norm in termos_proibidos

def _detect_all(text: str, anon_loc: bool):
    found = []
    PRIORITY = { "EMAIL": 1, "IP": 1, "CPF": 1, "CHASSI": 1, "RG": 2, "PHONE": 2, "PLATE": 2, "COORD": 3, "COORD_SINGLE": 3, "PER": 4, "GENERIC_CODE": 99 }
    
    for typ, pat in REGEX.items():
        if typ == "DATE_TIME": continue 
        if not anon_loc and typ in ["COORD", "COORD_SINGLE"]: continue
            
        for match in pat.finditer(text):
            val = match.group()
            if typ == "GENERIC_CODE" and (not any(c.isdigit() for c in val) or len(val) < 6): continue
            found.append((match.start(), match.end(), val, typ))

    for match in CONTEXT_NAME_REGEX.finditer(text):
        val = match.group(2).strip()
        if not _is_hallucination_basic(val) and _is_valid_entity_ollama(val, "PER"):
            found.append((match.start(2), match.end(2), text[match.start(2):match.end(2)], "PER"))

    if nlp:
        is_bad_casing = sum(1 for c in text if c.isupper()) > len(text) * 0.5 or text.islower()
        doc = nlp(text.title() if is_bad_casing else text)
        for ent in doc.ents:
            if ent.label_ == "PER":
                if any(t.pos_ in ["VERB", "PRON", "PUNCT", "SYM", "NUM"] for t in ent) or (len(ent) == 1 and ent[0].pos_ in ["NOUN", "ADJ"]):
                    continue
                prefix_match = PREFIX_TRIMMER.search(ent.text)
                offset = prefix_match.end() if prefix_match else 0
                val_clean = ent.text[offset:].strip()
                start = ent.start_char + offset + (ent.text[offset:].find(val_clean) if ent.text[offset:].find(val_clean) != -1 else 0)
                texto_original = text[start : start + len(val_clean)] 
                
                if not _is_hallucination_basic(texto_original) and _is_valid_entity_ollama(texto_original, "PER"):
                    found.append((start, start + len(val_clean), texto_original, "PER"))

    for match in NAME_FALLBACK_REGEX.finditer(text):
        val = match.group().strip()
        if not _is_hallucination_basic(val) and _is_valid_entity_ollama(val, "PER"):
            found.append((match.start(), match.end(), val, "PER"))

    found.sort(key=lambda x: (x[0], -(x[1] - x[0]), PRIORITY.get(x[3], 50)))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, text[s:e], t))
            last = e
    return clean

# =========================================================
# 3. MOTOR DE PSEUDONIMIZAÇÃO
# =========================================================
def apply_gps_jitter(coord_str):
    try:
        return f"{float(coord_str.replace(',', '.')) + random.uniform(-0.003, 0.003):.6f}"
    except ValueError:
        return coord_str

def _get_fake(value: str, typ: str) -> str:
    clean_value = html.unescape(re.sub(r'<[^>]+>', '', value)).strip()
    norm_val = _normalize(clean_value)
    cache_key = f"{typ}:{norm_val}"
    
    if cache_key in _MAPPING_CACHE: return _MAPPING_CACHE[cache_key]

    seed_int = int(hmac.new(SECRET_SALT.encode('utf-8'), norm_val.encode('utf-8'), hashlib.sha256).hexdigest()[:16], 16)
    fake.seed_instance(seed_int)
    local_rand = random.Random(seed_int)
    
    if typ in ["PER", "NOME_SOLTO"]: val = fake.name().upper()
    elif typ == "CPF": val = fake.cpf()
    elif typ == "RG": val = fake.numerify('##.###.###-#') 
    elif typ in ["PLATE", "PLACA"]: val = fake.license_plate().upper()
    elif typ == "EMAIL": val = fake.email().lower()
    elif typ == "PHONE": val = fake.phone_number()
    elif typ == "IP": val = fake.ipv4()
    elif typ == "CHASSI": val = "".join(local_rand.choices("ABCDEFGHJKLMNPRSTUVWXYZ0123456789", k=17))
    elif typ in ["COORD", "COORD_SINGLE"]: val = re.sub(r"-?\d{1,3}[.,]\d+", lambda m: apply_gps_jitter(m.group(0)), value)
    elif typ == "GENERIC_CODE": val = "".join([str(local_rand.randint(0, 9)) if c.isdigit() else (local_rand.choice(string.ascii_uppercase) if c.isalpha() else c) for c in clean_value])
    else: val = fake.word().upper()

    _MAPPING_CACHE[cache_key] = val
    return val

# =========================================================
# 4. ORQUESTRAÇÃO DE ANONIMIZAÇÃO PRINCIPAL
# =========================================================
def anonymize_value(col_name: str, val, anon_location: bool = True):
    try:
        if val is None or not str(val).strip(): return val, None
        text = str(val).strip()
        
        if len(text) < 3 or REGEX["DATE_TIME"].fullmatch(text): 
            return text, None
            
        politica_execucao = define_column_policy(col_name, [text])

        if politica_execucao not in ["TEXTO_LIVRE", "IGNORAR"]:
            if len(text.split()) >= 5 or len(text) > 60:
                politica_execucao = "TEXTO_LIVRE"

        if politica_execucao == "IGNORAR":
            return text, None
            
        if politica_execucao == "NOME_SOLTO":
            if _is_valid_entity_ollama(text, "NOME_SOLTO"):
                fake_val = _get_fake(text, "NOME_SOLTO")
                return fake_val, ("TEXT" if fake_val != text else None)
            return text, None
      
        if politica_execucao in ["COORD", "COORD_SINGLE"]:
            if anon_location:
                fake_val = _get_fake(text, politica_execucao)
                return fake_val, ("TEXT" if fake_val != text else None)
            return text, None
            
        if politica_execucao == "PLACA":
            fake_val = _get_fake(text, "PLATE")
            return fake_val, ("TEXT" if fake_val != text else None)

        if politica_execucao in ["CPF", "RG", "EMAIL", "PLATE", "PHONE", "IP", "CHASSI", "GENERIC_CODE", "DOC_GENERICO"]:
            fake_val = _get_fake(text, politica_execucao)
            return fake_val, ("TEXT" if fake_val != text else None)
            
        if politica_execucao == "TEXTO_LIVRE":
            entities = _detect_all(text, anon_location)
            if not entities: return text, None
            
            result, last = [], 0
            for s, e, v, t in entities:
                if s < last: continue
                result.append(text[last:s])
                result.append(_get_fake(v, t))
                last = e
            result.append(text[last:])
            texto_final = "".join(result)
            return texto_final, ("TEXT" if texto_final != text else None)

        return text, None

    except Exception as e:
        logger.error(f"Erro na máscara da coluna '{col_name}': {e}")
        return str(val), None 

def reset_memory():
    _MAPPING_CACHE.clear()
    _COLUMN_POLICIES.clear()
    _OLLAMA_CACHE.clear()
    _NEGATIVE_PATTERN_CACHE.clear()