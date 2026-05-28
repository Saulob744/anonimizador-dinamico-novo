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

# =========================================================
# CONFIGURAÇÃO E SEGURANÇA (LGPD)
# =========================================================
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s')
logger = logging.getLogger(__name__)

SECRET_SALT = os.getenv("ANONYMIZER_SECRET_SALT", "MudarParaUmSaltAltamenteComplexoESecreto123!")

try:
    import spacy
    nlp = spacy.load("pt_core_news_lg")
    logger.debug("spaCy carregado com sucesso.")
except OSError:
    logger.warning("spaCy não encontrado. A detecção dependerá apenas de Regex e IA LLM.")
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
    "DATE_TIME": re.compile(
        r"^\d{2,4}[/\-]\d{2}[/\-]\d{2,4}(?:\s+\d{2}:\d{2}(?:\:\d{2})?)?$|" 
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?$|"               
        r"^\d{2}:\d{2}(?:\:\d{2})?$"                                         
    ),
    "COORD": re.compile(r"-?\d{1,2}[.,]\d+\s*[,;]?\s*-?\d{1,3}[.,]\d+"), 
    "COORD_SINGLE": re.compile(r"^-?\d{1,3}[.,]\d{4,}$|^-\d{5,10}$"), 
    "EMAIL": re.compile(r"[\w\.-]++@[\w\.-]+", re.IGNORECASE),
    "CPF": re.compile(r"\b\d{3}[.\-\s]*\d{3}[.\-\s]*\d{3}[.\-\s]*\d{2}\b|\b\d{11}\b"),
    "RG": re.compile(r"\b(?:[A-Z]{2}[-\s]*)?\d{1,3}[.\-\s]*\d{3}[.\-\s]*\d{3}[-\s]*[0-9A-Z]\b|\b\d{5,14}\b", re.IGNORECASE),
    "PLATE": re.compile(r"\b[A-Z]{3}[-\s]?\d[A-Z0-9]\d{2}\b|\b[A-Z]{3}[-\s]?\d{4}\b", re.IGNORECASE),
    "PHONE": re.compile(r"\b(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{2,3}\)?[\s-]?)?\d{4,5}[\s-]?\d{4}\b"),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[a-fA-F0-9]{1,4}:){7}[a-fA-F0-9]{1,4}\b"),
    "CHASSI": re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE), 
    "GENERIC_CODE": re.compile(r'\b(?:(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_/\.\-]{6,64}|\d{6,64})\b'),
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
# 1. O COMITÊ DE CLASSIFICAÇÃO 
# =========================================================
class AegisClassifier:
    
    def _analyze_with_regex(self, samples: list) -> dict:
        scores = Counter()
        total = len(samples)
        if total == 0: return scores
        for s in samples:
            for tag, padrao in REGEX.items():
                if padrao.search(s):
                    scores[tag] += 1
            if NAME_FALLBACK_REGEX.search(s):
                scores["FORMATO_NOME"] += 1
        return {tag: f"{(count/total)*100:.0f}%" for tag, count in scores.items()}

    def _analyze_with_spacy(self, samples: list) -> dict:
        if not nlp: return {}
        scores = Counter()
        total = len(samples)
        for s in samples:
            if len(s) > 200: continue
            doc = nlp(s.title())
            for ent in doc.ents:
                scores[ent.label_] += 1
        return {tag: f"{(count/total)*100:.0f}%" for tag, count in scores.items()}

    def _ultimate_safety_fallback(self, col_name: str, amostras: list) -> str:
        prompt = f"""
Você é a última linha de defesa de privacidade.
Analise a coluna '{col_name}' e as amostras: {' | '.join(amostras)}

PERGUNTA:
Existe ALGUMA possibilidade de que estes dados contenham informações únicas, identificadores pessoais, relatos ou dados de pessoas (mesmo que misturados)?

Responda APENAS 'SIM' ou 'NAO'.
"""
        logger.warning(f"🚨 [FALLBACK CRÍTICO] Acionando última linha de defesa para '{col_name}'...")
        try:
            payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0, "num_predict": 5}}
            resp = requests.post(OLLAMA_URL, json=payload, timeout=15)
            if resp.status_code == 200:
                ans = resp.json().get("response", "").strip().upper()
                if "SIM" in ans:
                    return "TEXTO_LIVRE"
                else:
                    return "IGNORAR"
        except Exception as e:
            logger.error(f"❌ Erro na Rede de Segurança. Fail-Safe ativado. Erro: {e}")
            
        return "TEXTO_LIVRE"

    def get_column_tag(self, col_name: str, samples: list) -> str:
        amostras_limpas = [str(s).strip() for s in samples if s is not None and str(s).strip()]
        if not amostras_limpas: return "IGNORAR"
        
        amostras_unicas = list(set(amostras_limpas))[:15]

        if all(REGEX["DATE_TIME"].fullmatch(s) for s in amostras_unicas):
            return "IGNORAR"

        regex_report = self._analyze_with_regex(amostras_unicas)
        spacy_report = self._analyze_with_spacy(amostras_unicas)

        # =================================================================
        #  FAST-TRACK MATEMÁTICO 
        # =================================================================
        if regex_report.get("CPF") and int(regex_report["CPF"].strip("%")) > 60: return "CPF"
        if regex_report.get("PLATE") and int(regex_report["PLATE"].strip("%")) > 60: return "PLACA"
        if regex_report.get("EMAIL") and int(regex_report["EMAIL"].strip("%")) > 60: return "EMAIL"
        if regex_report.get("PHONE") and int(regex_report["PHONE"].strip("%")) > 60: return "PHONE"
        if regex_report.get("IP") and int(regex_report["IP"].strip("%")) > 60: return "IGNORAR" 

        prompt = f"""
Sua missão é classificar a coluna '{col_name}' baseando-se nas amostras e na análise.

[ANALISE ESTRUTURAL]
Regex detectou: {regex_report}
NLP detectou: {spacy_report}
Amostras reais: {' | '.join(amostras_unicas)}

[REGRA CRÍTICA - LGPD]
1. Nomes de Cidades, Estados, Bairros e Ruas NÃO SÃO PESSOAS. Escolha IGNORAR.
2. Cores, Marcas e Modelos de veículos NÃO SÃO SENSÍVEIS. Escolha IGNORAR.
3. Nomes compostos e sobrenomes (ex: Silva, Abreu Silva, Carlos) SÃO PESSOAS. Se a Regex acusar "FORMATO_NOME", escolha NOME_SOLTO.

Escolha APENAS UMA tag correspondente da lista abaixo:
IGNORAR - (Cidades, locais, datas, IDs numéricos, status, cores, marcas de carro, modelos)
NOME_SOLTO - (Nomes próprios de pessoas físicas)
CPF - (Documentos, CPFs, RGs, CNPJs)
EMAIL - (Endereços de email)
PHONE - (Números de telefone)
COORD - (Latitudes e longitudes)
PLACA - (Placas de veículos automotores, carros, motos)
TEXTO_LIVRE - (Textos longos, relatos, observações, histórico)

Sua resposta deve conter APENAS o nome da tag.
"""
        logger.info(f"⚖️ [COMITÊ AEGIS] Avaliando a coluna '{col_name}'...")
        try:
            payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0, "num_predict": 10}}
            resp = requests.post(OLLAMA_URL, json=payload, timeout=20)
            if resp.status_code == 200:
                tag = resp.json().get("response", "").strip().upper()
                
                tags_validas = ["IGNORAR", "NOME_SOLTO", "CPF", "EMAIL", "PHONE", "COORD", "PLACA", "TEXTO_LIVRE"]
                for tv in tags_validas:
                    if tv in tag: 
                        return tv
        except Exception as e:
            logger.warning(f"❌ Fallback acionado para '{col_name}'. Erro: {e}")

        if spacy_report.get("PER") and int(spacy_report["PER"].strip("%")) > 50: return "NOME_SOLTO"
        
        return self._ultimate_safety_fallback(col_name, amostras_unicas)

_aegis_engine = AegisClassifier()

def define_column_policy(col_name: str, sample_values: list) -> str:
    if col_name in _COLUMN_POLICIES: 
        return _COLUMN_POLICIES[col_name]
        
    politica = _aegis_engine.get_column_tag(col_name, sample_values)
    
    logger.info(f"✅ Política Definida Mestre para '{col_name}': {politica}")
    _COLUMN_POLICIES[col_name] = politica
    return politica

# =========================================================
# 2. JUIZ DE ENTIDADES & TEXTO LIVRE
# =========================================================
def _is_valid_entity_ollama(text: str, tag: str) -> bool:
    if len(text.strip()) < 3: return False
    
    cache_key = f"{tag}:{text.upper()}"
    if cache_key in _OLLAMA_CACHE: return _OLLAMA_CACHE[cache_key]
        
    if tag in ["PER", "NOME_SOLTO"]:
        prompt = f"A expressão '{text}' é o nome próprio de uma pessoa física humana? Regra: Nomes de ruas, bairros, cidades ou DATAS/HORAS são 'NAO'. Responda APENAS 'SIM' ou 'NAO'."
    else:
        return True 
    
    try:
        payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0, "num_predict": 5}}
        resp = requests.post(OLLAMA_URL, json=payload, timeout=18, proxies={"http": "", "https": ""})
        if resp.status_code == 200:
            is_valid = "SIM" in resp.json().get("response", "").strip().upper()
            _OLLAMA_CACHE[cache_key] = is_valid 
            return is_valid
    except Exception as e:
        logger.warning(f"Timeout no Ollama para '{text}'. Erro: {e}")
        
    return False

@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper().strip()

def _is_hallucination_basic(ent_text: str) -> bool:
    ent_text = ent_text.strip(".,;:-\n '\"")
    if not ent_text or len(ent_text) <= 2: return True
    if any(c.isdigit() for c in ent_text) and any(c in ['.', ',', '-'] for c in ent_text): return True
    
    termos_proibidos = {
        "CPF", "RG", "CNPJ", "CEP", "DOC", "DOCUMENTO", "TEL", "CEL", "TELEFONE", 
        "CELULAR", "PIX", "CHAVE", "PLACA", "DR", "DRA", "SR", "SRA", "SISTEMA", 
        "RELATORIO", "PROJETO", "PGTO", "PAGAMENTO", "EFETUADO", "TRANSFERENCIA", 
        "ESTORNO", "DEVOLUCAO", "VALOR", "TARIFA", "TAXA", "VIATURA", "VEICULO",
        "CARRO", "MOTO", "MODELO", "MARCA", "CHEVROLET", "FIAT", "VOLKSWAGEN", 
        "FORD", "HONDA", "TOYOTA", "HYUNDAI", "RENAULT", "NISSAN", "JEEP", "PEUGEOT",
        "ASSALTO", "ROUBO", "FURTO", "CRIME", "DELITO", "OCORRENCIA", "BO", "TRAFICO", "EXTORSAO"
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
        if typ == "DATE_TIME": continue 
        if not anon_loc and typ in ["COORD", "COORD_SINGLE"]: 
            continue
            
        for match in pat.finditer(text):
            val = match.group()
            if typ == "GENERIC_CODE" and (not any(c.isdigit() for c in val) or len(val) < 6):
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

    secret_bytes = SECRET_SALT.encode('utf-8')
    data_bytes = norm_val.encode('utf-8')
    hmac_hash = hmac.new(secret_bytes, data_bytes, hashlib.sha256).hexdigest()
    seed_int = int(hmac_hash[:8], 16)
    
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
    elif typ in ["COORD", "COORD_SINGLE"]: 
        val = re.sub(r"-?\d{1,3}[.,]\d+", lambda m: apply_gps_jitter(m.group(0)), value)
    elif typ == "GENERIC_CODE": 
        val = "".join([str(local_rand.randint(0, 9)) if c.isdigit() else (local_rand.choice(string.ascii_uppercase) if c.isalpha() else c) for c in clean_value])
    else: val = fake.word().upper()

    _MAPPING_CACHE[cache_key] = val
    return val

# =========================================================
# 4. ORQUESTRAÇÃO DE ANONIMIZAÇÃO
# =========================================================
def anonymize_value(col_name: str, val, anon_location: bool = True):
    try:
        if val is None or not str(val).strip(): return val, None
        text = str(val).strip()
        
        if len(text) < 3 or REGEX["DATE_TIME"].fullmatch(text): 
            return text, None
            
        if col_name not in _COLUMN_POLICIES:
            politica = define_column_policy(col_name, [text])
        else:
            politica = _COLUMN_POLICIES[col_name]

        politica_execucao = politica

        if politica_execucao not in ["TEXTO_LIVRE", "IGNORAR"]:
            if len(text) > 50 or len(text.split()) > 4:
                politica_execucao = "TEXTO_LIVRE"

        if politica_execucao == "IGNORAR":
            return text, None
            
        if politica_execucao == "NOME_SOLTO":
            if _is_valid_entity_ollama(text, "NOME_SOLTO"):
                fake_val = _get_fake(text, "NOME_SOLTO")
                return fake_val, ("TEXT" if fake_val != text else None)
            else:
                return text, None
      
        if politica_execucao in ["COORD", "COORD_SINGLE"]:
            if anon_location:
                fake_val = _get_fake(text, politica_execucao)
                return fake_val, ("TEXT" if fake_val != text else None)
            else:
                return text, None
            
        if politica_execucao == "PLACA":
            fake_val = _get_fake(text, "PLATE")
            return fake_val, ("TEXT" if fake_val != text else None)

        if politica_execucao in ["CPF", "RG", "EMAIL", "PLATE", "PHONE", "IP", "CHASSI", "GENERIC_CODE"]:
            fake_val = _get_fake(text, politica_execucao)
            return fake_val, ("TEXT" if fake_val != text else None)
            
        if politica_execucao == "TEXTO_LIVRE":
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

        return text, None

    except Exception as e:
        logger.error(f"⚠️ Erro ao aplicar mascara na coluna '{col_name}': {e}", exc_info=True)
        return str(val), None 

def reset_memory():
    _MAPPING_CACHE.clear()
    _COLUMN_POLICIES.clear()
    _OLLAMA_CACHE.clear()
    _DYNAMIC_SAMPLES.clear()