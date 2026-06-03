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
from collections import Counter
from functools import lru_cache
from faker import Faker

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s')
logger = logging.getLogger(__name__)

SECRET_SALT = os.getenv("ANONYMIZER_SECRET_SALT", "SaltDeEmergenciaApenasParaDesenvolvimento123!")
if SECRET_SALT == "SaltDeEmergenciaApenasParaDesenvolvimento123!":
    logger.warning("ANONYMIZER_SECRET_SALT ausente/fraco. Usando default inseguro.")

try:
    import spacy
    nlp = spacy.load("pt_core_news_lg")
except OSError:
    logger.warning("spaCy não encontrado.")
    nlp = None

_MAPPING_CACHE = {}
_COLUMN_POLICIES = {} 
_OLLAMA_CACHE = {} 
_NEGATIVE_PATTERN_CACHE = set()

fake = Faker("pt_BR")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3:latest")

REGEX = {
    "CPF": re.compile(r"\b\d{3}[.\-\s/_]*\d{3}[.\-\s/_]*\d{3}[.\-\s/_]*\d{2}\b|\b\d{11}\b"),
    "RG": re.compile(r"\b(?:[A-Za-z]{2}[-\s]*)?\d{1,3}[.\-\s]*\d{3}[.\-\s]*\d{3}[-\s]*[0-9A-Za-z]\b|\b\d{5,14}\b"),
    "EMAIL": re.compile(r"[\w\.-]+@[\w\.-]+\.\w+", re.IGNORECASE),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[a-fA-F0-9]{1,4}:){7}[a-fA-F0-9]{1,4}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}[-\s]*\d[A-Z0-9]\d{2}\b|\b[A-Z]{3}[-\s]*\d{4}\b", re.IGNORECASE),
    "PHONE": re.compile(r"\b(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{2,3}\)?[\s-]?)?\d{4,5}[\s-]?\d{4}\b"),
    "CHASSI": re.compile(r"\b(?:[A-HJ-NPR-Z0-9][-\s]*){17}\b", re.IGNORECASE),
    "DATE_TIME": re.compile(r"\b\d{2,4}[/\-]\d{2}[/\-]\d{2,4}(?:\s+\d{2}:\d{2}(?:\:\d{2})?)?\b|\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b|\b\d{2}:\d{2}(?:\:\d{2})?\b"),    
    "COORD": re.compile(r"\b-?\d{1,3}[.,]\d{4,}\s*[,;]\s*-?\d{1,3}[.,]\d{4,}\b"), 
    "COORD_SINGLE": re.compile(r"(?<!\d)-?\d{1,3}[.,]\d{4,}(?!\d)|(?<!\d)-\d{5,10}(?!\d)"),
    "DOC_GENERICO": re.compile(r"\b(?:\d[.\-\s]*){7,15}\b"),
    "GENERIC_CODE": re.compile(r'\b(?:(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_/\.\-]{5,128})\b'),
}

CONTEXT_NAME_REGEX = re.compile(r"\b(nome|cliente|paciente|sr|sra|dr|dra|atendente|consultor|gerente|responsavel|usuario|agente|vendedor|motorista|funcionario|titular|favorecido)\s*[:\-]?\s+([A-ZÀ-Ÿa-zà-ÿ]{2,20}(?:\s+[A-ZÀ-Ÿa-zà-ÿ]{2,20}){0,4})\b", re.IGNORECASE)
PREFIX_TRIMMER = re.compile(r"^(ao|a|para|dr\.?|dra\.?|sr\.?|sra\.?|senhor|senhora|em|na|no|de|do|da|vitima|autor|paciente|soldado|policial|rua|avenida|trevo|cia|agente|vendedor|motorista|funcionario)\s+", re.IGNORECASE)
NAME_FALLBACK_REGEX = re.compile(r"\b(?:[A-ZÀ-Ÿ][a-zà-ÿ]+|[A-ZÀ-Ÿ]{2,})(?:\s+(?:de|da|do|dos|das|e|DE|DA|DO|DOS|DAS|E))?\s+(?:[A-ZÀ-Ÿ][a-zà-ÿ]+|[A-ZÀ-Ÿ]{2,})(?:\s+(?:[A-ZÀ-Ÿ][a-zà-ÿ]+|[A-ZÀ-Ÿ]{2,}))*\b(?!\:)")

def _ask_llm_yes_no(prompt: str, cache_key: str) -> bool:
    if cache_key in _OLLAMA_CACHE:
        return _OLLAMA_CACHE[cache_key]

    try:
        logger.debug(f"Enviando para LLM (Cache Miss): {cache_key}")
        payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0, "num_predict": 5}}
        resp = requests.post(OLLAMA_URL, json=payload, timeout=15)
        if resp.status_code == 200:
            ans = resp.json().get("response", "").strip().upper()
            is_yes = "SIM" in ans
            _OLLAMA_CACHE[cache_key] = is_yes
            logger.debug(f"LLM Respondeu: {'SIM' if is_yes else 'NAO'}")
            return is_yes
    except Exception as e:
        logger.error(f"Falha na comunicação com LLM: {e}")
    
    return False

def _is_valid_entity_ollama(text: str, tag: str) -> bool:
    if len(text.strip()) < 3: return False
    
    esqueleto = re.sub(r'\d+', '[NUM]', text.upper())
    if esqueleto in _NEGATIVE_PATTERN_CACHE: 
        logger.debug(f"Rejeitado por Negative Cache: {text}")
        return False
        
    cache_key = f"{tag}:{text.upper()}"
    prompt = f"Avalie o dado '{text}'. Ele pertence DEFINITIVAMENTE à categoria '{tag}' ou identifica uma pessoa real? Responda APENAS 'SIM' ou 'NAO'."
    
    result = _ask_llm_yes_no(prompt, cache_key)
    if not result:
        _NEGATIVE_PATTERN_CACHE.add(esqueleto)
    return result

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
        if total == 0: return {}
        for s in samples:
            for tag, padrao in REGEX.items():
                if padrao.search(s):
                    scores[tag] += 1
        return {tag: (count/total)*100 for tag, count in scores.items()}

    def get_column_tag(self, col_name: str, samples: list) -> str:
        col_lower = col_name.lower().strip()
        
        if col_lower in ['cpf']: return "CPF"
        if col_lower in ['rg']: return "RG"
        if 'email' in col_lower: return "EMAIL"
        if 'placa' in col_lower: return "PLATE"
        
        amostras_limpas = [str(s).strip() for s in samples if s is not None and str(s).strip()]
        if not amostras_limpas: return "IGNORAR"
        amostras_unicas = list(set(amostras_limpas))[:15]

        media_palavras = sum(len(s.split()) for s in amostras_unicas) / len(amostras_unicas)
        tem_pontuacao_de_frase = any(re.search(r'[,.!?]\s+[A-Z]', s) for s in amostras_unicas)
        
        if media_palavras >= 4 or any(len(s) > 60 for s in amostras_unicas) or tem_pontuacao_de_frase:
            logger.info(f"Coluna '{col_name}' identificada como TEXTO_LIVRE por estrutura de frase.")
            return "TEXTO_LIVRE"

        col_lower = col_name.lower()
        if any(termo in col_lower for termo in {'cidade', 'municipio', 'estado', 'pais', 'uf', 'bairro', 'cep', 'status', 'tipo', 'categoria', 'marca', 'modelo', 'cor', 'produto', 'data'}):
            logger.info(f"Coluna {col_name} ignorada por heurística de nome.")
            return "IGNORAR"

        regex_scores = self._analyze_with_regex(amostras_unicas)
        top_regex_tag = max(regex_scores, key=regex_scores.get, default=None)

        if top_regex_tag and top_regex_tag != "DATE_TIME":
            policy_tag = self.FAST_TRACK_MAP.get(top_regex_tag)
            score = regex_scores[top_regex_tag]
            
            if score >= 80.0:
                logger.info(f"Regex cravou '{policy_tag}' com {score:.1f}% de precisão. IA ignorada.")
                return policy_tag

            logger.info(f"Regex sugeriu '{policy_tag}' ({score:.1f}%). Solicitando palavra final da LLM.")
            amostra_str = " | ".join(amostras_unicas[:5])
            prompt_confirmacao = f"A amostra de dados '{amostra_str}' corresponde à categoria '{policy_tag}'? Responda APENAS 'SIM' ou 'NAO'."
            
            if _ask_llm_yes_no(prompt_confirmacao, f"COL_{col_name}_{policy_tag}"):
                return policy_tag

        if nlp:
            per_count = sum(1 for s in amostras_unicas if any(ent.label_ == "PER" for ent in nlp(s.title()).ents))
            if (per_count / len(amostras_unicas)) * 100 >= 30.0:
                logger.info(f"spaCy encontrou possíveis Nomes na coluna '{col_name}'. Validando com LLM.")
                prompt_spacy = f"A amostra '{' | '.join(amostras_unicas[:5])}' contém nomes próprios de pessoas reais? Responda APENAS 'SIM' ou 'NAO'."
                if _ask_llm_yes_no(prompt_spacy, f"COL_{col_name}_NOME_SOLTO"):
                    return "NOME_SOLTO"

            logger.info(f"Fallback acionado para '{col_name}'. LLM decidirá se é TEXTO_LIVRE.")
            prompt_fallback = f"A amostra '{' | '.join(amostras_unicas[:5])}' possui qualquer dado pessoal identificável ou texto narrativo sensível? Responda APENAS 'SIM' ou 'NAO'."
            if _ask_llm_yes_no(prompt_fallback, f"COL_FALLBACK_{col_name}"):
                return "TEXTO_LIVRE"

        return "IGNORAR"

_aegis_engine = AegisClassifier()

# =========================================================================
    # MOTOR DINÂMICO DE REJEIÇÃO
    # =========================================================================
def is_dynamic_jargon(texto_candidato: str) -> bool:
        t_limpo = ''.join(c for c in unicodedata.normalize('NFD', texto_candidato.lower()) if unicodedata.category(c) != 'Mn')
        
        if not any(v in t_limpo for v in 'aeiouy'):
            return True

        if not nlp:
            return False

        doc = nlp(texto_candidato)

        for ent in doc.ents:
            if ent.label_ in ["ORG", "MISC", "LOC"]:
                return True

        for token in doc:
            if token.is_upper and len(token.text) <= 4:
                return True

            if token.pos_ in ["NUM", "SYM", "PUNCT", "VERB"]:
                if token.is_title:
                    continue
                return True
                
            morfologia = str(token.morph)
            if "VerbForm=Part" in morfologia or "VerbForm=Ger" in morfologia:
                return True

        return False
    # =========================================================================

def define_column_policy(col_name: str, sample_values: list) -> str:
    if col_name not in _COLUMN_POLICIES: 
        _COLUMN_POLICIES[col_name] = _aegis_engine.get_column_tag(col_name, sample_values)
    return _COLUMN_POLICIES[col_name]

@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper().strip()

def _detect_all(text: str, anon_loc: bool):
    found = []
    TRUSTED_TAGS = {"CPF", "RG", "EMAIL", "IP", "PLATE", "CHASSI", "PHONE", "COORD", "COORD_SINGLE"}
    
    INVALID_NAME_WORDS = re.compile(
        r"\b(rua|avenida|av|travessa|trav|praça|praca|alameda|rodovia|rod|bairro|lote|lt|quadra|qd|condominio|cond|edificio|ed|bloco|apartamento|apto|casa|km|br|centro|jardim|jd|parque|pq|vila|"
        r"pgto|pagamento|pago|efetuado|transferencia|transf|pix|banco|agencia|conta|boleto|valor|saldo|debito|credito|tarifa|taxa|estorno|cancelado|aprovado|rejeitado|compra|venda|op|"
        r"sistema|relatorio|projeto|arquivo|anexo|dados|info|usuario|senha|login|id|codigo|protocolo|veiculo|carro|moto|placa|chassi|renavam)\b", 
        re.IGNORECASE
    )
    for typ, pat in REGEX.items():
        if typ == "DATE_TIME" or (not anon_loc and typ in ["COORD", "COORD_SINGLE"]): continue
        for match in pat.finditer(text):
            val = match.group()
            if typ == "GENERIC_CODE" and (not any(c.isdigit() for c in val) or len(val) < 6): 
                continue
            if typ in TRUSTED_TAGS:
                found.append((match.start(), match.end(), val, typ))
            else:
                logger.debug(f"Regex encontrou {val} como {typ}. Solicitando IA.")
                if _is_valid_entity_ollama(val, typ):
                    found.append((match.start(), match.end(), val, typ))

    for match in CONTEXT_NAME_REGEX.finditer(text):
        val = match.group(2).strip()
        if len(val) >= 3 and not INVALID_NAME_WORDS.search(val) and _is_valid_entity_ollama(val, "PER"):
            found.append((match.start(2), match.end(2), text[match.start(2):match.end(2)], "PER"))

    for match in NAME_FALLBACK_REGEX.finditer(text):
        val = match.group().strip()
        if len(val) >= 3 and not INVALID_NAME_WORDS.search(val) and _is_valid_entity_ollama(val, "PER"):
            found.append((match.start(), match.end(), val, "PER"))

    if nlp:
        is_bad_casing = text.isupper() or text.islower()
        doc = nlp(text.title() if is_bad_casing else text)
        
        for ent in doc.ents:
            if ent.label_ == "PER":
                val_clean = ent.text.strip(".,;:?!() \n'\"")
                if len(val_clean) < 3 or any(char.isdigit() for char in val_clean) or INVALID_NAME_WORDS.search(val_clean): 
                    continue

                logger.debug(f"spaCy encontrou '{val_clean}'. Solicitando palavra final da LLM.")
                if _is_valid_entity_ollama(val_clean, "PER"):
                    start = text.find(val_clean, max(0, ent.start_char - 2))
                    if start != -1:
                        found.append((start, start + len(val_clean), val_clean, "PER"))

    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, text[s:e], t))
            last = e
    return clean

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
    elif typ == "DOC_GENERICO": 
        val = "".join([str(local_rand.randint(0, 9)) if c.isdigit() else c for c in clean_value])
    elif typ == "GENERIC_CODE": val = "".join([str(local_rand.randint(0, 9)) if c.isdigit() else (local_rand.choice(string.ascii_uppercase) if c.isalpha() else c) for c in clean_value])
    else: val = fake.word().upper()

    _MAPPING_CACHE[cache_key] = val
    return val

def anonymize_value(col_name: str, val, anon_location: bool = True):
    try:
        if val is None or not str(val).strip(): return val, None
        text = str(val).strip()
        
        politica_execucao = define_column_policy(col_name, [text])

        if politica_execucao == "IGNORAR":
            return text, None
            
        if politica_execucao in ["NOME_SOLTO", "COORD", "COORD_SINGLE", "PLACA", "CPF", "RG", "EMAIL", "PLATE", "PHONE", "IP", "CHASSI", "GENERIC_CODE", "DOC_GENERICO"]:
            fake_val = _get_fake(text, politica_execucao)
            return fake_val, ("TEXT" if fake_val != text else None)
            
        if politica_execucao == "TEXTO_LIVRE":
            logger.debug(f"Iniciando varredura profunda de texto livre na coluna {col_name}.")
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