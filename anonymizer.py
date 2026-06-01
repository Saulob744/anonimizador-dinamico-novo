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
from collections import Counter, defaultdict
from functools import lru_cache
from faker import Faker

# =========================================================
# CONFIGURAÇÃO E SEGURANÇA 
# =========================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s')
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
_NEGATIVE_PATTERN_CACHE = set()
_COLUMN_SAMPLES_ACCUMULATOR = defaultdict(list)

fake = Faker("pt_BR")

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "llama3:latest"

# =========================================================
# REGEX ESTRUTURAIS (MALEÁVEIS / FLEXÍVEIS)
# =========================================================
REGEX = {
    "DATE_TIME": re.compile(
        r"^\d{2,4}[/\-]\d{2}[/\-]\d{2,4}(?:\s+\d{2}:\d{2}(?:\:\d{2})?)?$|" 
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?$|"               
        r"^\d{2}:\d{2}(?:\:\d{2})?$"                                        
    ),
    "COORD": re.compile(r"-?\d{1,3}[.,]\d{2,}\s*[,;|/\\_]+\s*-?\d{1,3}[.,]\d{2,}"), 
    "COORD_SINGLE": re.compile(r"(?<!\d)-?\d{1,3}[.,]\d{4,}(?!\d)"), 
    "EMAIL": re.compile(r"[\w\.-]+@[\w\.-]+", re.IGNORECASE),
    "CPF": re.compile(r"(?<!\d)\d{3}[.\-\s_]*\d{3}[.\-\s_]*\d{3}[.\-\s_]*\d{2}(?!\d)|(?<!\d)\d{11}(?!\d)"),
    "RG": re.compile(r"(?<![A-Za-z0-9])(?:[A-Z]{2}[-\s_]*)?\d{1,3}[.\-\s_]*\d{3}[.\-\s_]*\d{3}[-\s_]*[0-9A-Z](?![A-Za-z0-9])|(?<!\d)\d{5,14}(?!\d)", re.IGNORECASE),
    "PLATE": re.compile(r"(?<![A-Z])[A-Z]{3}[-.\s_]*\d[A-Z0-9][-.\s_]*\d{2}(?![A-Za-z0-9])|(?<![A-Z])[A-Z]{3}[-.\s_]*\d{4}(?![A-Za-z0-9])", re.IGNORECASE),
    "PHONE": re.compile(r"(?<!\d)(?:\+?55[\s-]*)?(?:\(?\d{2,3}\)?[\s-]*)?\d{4,5}[\s-]?\d{4}(?!\d)"),
    "IP": re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)|(?<![A-Fa-f0-9])(?:[a-fA-F0-9]{1,4}:){7}[a-fA-F0-9]{1,4}(?![A-Fa-f0-9])"),
    "CHASSI": re.compile(r"(?<![A-Za-z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9])", re.IGNORECASE), 
    "GENERIC_CODE": re.compile(r'(?<![A-Za-z0-9])(?:(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_/\.\-:#@~]{6,64}|\d{6,64})(?![A-Za-z0-9])'),
}

CONTEXT_NAME_REGEX = re.compile(r"\b(nome|cliente|paciente|sr|sra|dr|dra|atendente|consultor|gerente|responsavel|usuario|agente|vendedor|motorista|funcionario|titular|favorecido)\s*[:\-]?\s+([A-ZÀ-Ÿa-zà-ÿ]{2,20}(?:\s+[A-ZÀ-Ÿa-zà-ÿ]{2,20}){0,4})\b", re.IGNORECASE)

# =========================================================
# HELPER JSON SEGURO
# =========================================================
def _extract_json_from_llm(response_text: str) -> dict:
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if match:
            try: return json.loads(match.group(0))
            except: pass
    return {}

# =========================================================
# 1. CLASSIFICAÇÃO AVANÇADA 
# =========================================================
class AegisClassifier:
    
    def get_column_tag(self, col_name: str, samples: list) -> str:
        amostras_limpas = [str(s).strip() for s in samples if s is not None and str(s).strip()]
        if not amostras_limpas: return "IGNORAR"

        amostras_unicas = list(set(amostras_limpas))[:15]
        
        if all(REGEX["DATE_TIME"].fullmatch(s) for s in amostras_unicas):
            return "IGNORAR"

        propostas = Counter()
        total_amostras = len(amostras_unicas)
        
        for s in amostras_unicas:
            for tag, padrao in REGEX.items():
                if tag in ["DATE_TIME", "GENERIC_CODE", "COORD_SINGLE"]: continue
                if padrao.search(s):
                    propostas[tag] += 1
                    
        for tag, count in propostas.items():
            if (count / total_amostras) >= 0.6:
                logger.info(f"⚡ Fast-track Regex ativado: '{col_name}' classificada como {tag}.")
                if tag == "PLATE": return "PLACA"
                return tag

      
        amostras_str = " | ".join(amostras_unicas)
        prompt = f"""Atue como um Arquiteto de Dados Especialista em LGPD.
Sua missão é classificar a semântica da coluna '{col_name}' baseando-se no conjunto destas amostras reais:
[{amostras_str}]

REGRA DE DESAMBIGUAÇÃO CRÍTICA (LEIA COM ATENÇÃO):
1. Nomes de Cidades, Estados, Municípios e Bairros (ex: 'Santos', 'São Bernardo', 'Dumont', 'Jaú', 'Rio de Janeiro') NÃO são dados pessoais. Devem ser classificados como IGNORAR. O nome da coluna (ex: 'cidade', 'city', 'local', 'endereco') é a maior dica.
2. Códigos internos, status, booleanos, profissões, marcas, modelos de carro ou documentos genéricos devem ser IGNORAR.
3. Se a coluna for EXCLUSIVAMENTE composta por Nomes Próprios de Seres Humanos, classifique como NOME_SOLTO.
4. Se houver frases longas, relatos ou texto misturado, classifique como TEXTO_LIVRE.

Responda OBRIGATORIAMENTE em JSON:
{{
    "raciocinio": "Explique a relação entre o nome da coluna '{col_name}' e as amostras. Deixe claro se encontrou municípios/locais.",
    "categoria": "IGNORAR" ou "NOME_SOLTO" ou "TEXTO_LIVRE" ou "GENERIC_CODE"
}}"""

        logger.info(f"⚖️ [AEGIS IA] Avaliando a semântica de conjunto da coluna '{col_name}'...")
        try:
            payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "format": "json", "options": {"temperature": 0.0}}
            resp = requests.post(OLLAMA_URL, json=payload, timeout=125)
            
            if resp.status_code == 200:
                result = _extract_json_from_llm(resp.json().get("response", "{}"))
                raciocinio = result.get("raciocinio", "Sem raciocínio")
                categoria = result.get("categoria", "TEXTO_LIVRE").strip().upper()
                
                logger.info(f"🧠 [IA PENSOU]: {raciocinio}")
                
                tags_validas = ["IGNORAR", "NOME_SOLTO", "TEXTO_LIVRE", "GENERIC_CODE"]
                if categoria in tags_validas:
                    return categoria
                
        except Exception as e:
            logger.warning(f"❌ LLM falhou ao analisar contexto semântico. Erro: {e}")

        avg_words = sum(len(s.split()) for s in amostras_unicas) / len(amostras_unicas)
        return "TEXTO_LIVRE" if avg_words > 3 else "IGNORAR"


_aegis_engine = AegisClassifier()

def define_column_policy(col_name: str, sample_values: list) -> str:
    if col_name in _COLUMN_POLICIES: 
        return _COLUMN_POLICIES[col_name]
        
    valid_samples = [str(s).strip() for s in sample_values if s and str(s).strip()]
    if not valid_samples:
        return "IGNORAR"
        
    cache_key = f"{col_name}_{os.getpid()}"
    _COLUMN_SAMPLES_ACCUMULATOR[cache_key].extend(valid_samples)
    
    if len(_COLUMN_SAMPLES_ACCUMULATOR[cache_key]) < 15:
        return "TEXTO_LIVRE"
    politica = _aegis_engine.get_column_tag(col_name, _COLUMN_SAMPLES_ACCUMULATOR[cache_key])
    
    logger.info(f"✅ Política Definida Mestre para '{col_name}': {politica}")
    _COLUMN_POLICIES[col_name] = politica
    
    del _COLUMN_SAMPLES_ACCUMULATOR[cache_key]
    return politica

# =========================================================
# 2. JUIZ DE ENTIDADES E PROCESSAMENTO DE TEXTO LIVRE
# =========================================================
def _get_skeleton(text: str) -> str:
    return re.sub(r'\d+', '[NUM]', text.upper())

def _is_valid_entity_ollama(text: str, tag: str) -> bool:
    if len(text.strip()) < 3: return False
    
    cache_key = f"{tag}:{text.upper()}"
    if cache_key in _OLLAMA_CACHE: return _OLLAMA_CACHE[cache_key]
    
    esqueleto = _get_skeleton(text)
    if esqueleto in _NEGATIVE_PATTERN_CACHE:
        return False
        
    if tag in ["PER", "NOME_SOLTO"]:
        prompt = f"""Atue como um filtro rigoroso de LGPD. Analise a expressão: '{text}'.
Ela é EXCLUSIVAMENTE o nome próprio de uma pessoa física humana real?

Regras de Rejeição (is_pessoa = false):
- Cargos ou profissões (ex: 'Motorista', 'Médico')
- Nomes de ruas, cidades, bairros ou locais geográficos (ex: 'Santos', 'Mariana', 'São Bernardo')
- Jargões corporativos ou marcas.

Responda OBRIGATORIAMENTE em JSON:
{{
    "raciocinio": "sua analise breve",
    "is_pessoa": true ou false
}}"""
    else:
        return True 
    
    try:
        payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "format": "json", "options": {"temperature": 0.0, "num_predict": 10}}
        resp = requests.post(OLLAMA_URL, json=payload, timeout=28, proxies={"http": "", "https": ""})
        
        if resp.status_code == 200:
            result = _extract_json_from_llm(resp.json().get("response", "{}"))
            is_valid = bool(result.get("is_pessoa", False))
            
            _OLLAMA_CACHE[cache_key] = is_valid 
            if not is_valid:
                _NEGATIVE_PATTERN_CACHE.add(esqueleto)
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
        if not anon_loc and typ in ["COORD", "COORD_SINGLE"]: continue
            
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
                val_clean = ent.text.strip()
                start_adj = ent.text.find(val_clean)
                start = ent.start_char + (start_adj if start_adj != -1 else 0)
                texto_original = text[start : start + len(val_clean)] 
                
                if not _is_hallucination_basic(texto_original): 
                    if _is_valid_entity_ollama(texto_original, "PER"):
                        found.append((start, start + len(val_clean), texto_original, "PER"))

    found.sort(key=lambda x: (x[0], -(x[1] - x[0]), PRIORITY.get(x[3], 50)))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, text[s:e], t))
            last = e
    return clean

# =========================================================
# 3. MÁQUINA DE FALSOS DADOS E CRIPTOGRAFIA
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

    secret_bytes = SECRET_SALT.encode('utf-8')
    data_bytes = norm_val.encode('utf-8')
    hmac_hash = hmac.new(secret_bytes, data_bytes, hashlib.sha256).hexdigest()
    seed_int = int(hmac_hash[:8], 16)
    
    fake.seed_instance(seed_int)
    local_rand = random.Random(seed_int)
    
    if typ in ["PER", "NOME_SOLTO"]: val = f"{fake.first_name()} {fake.last_name()}".upper()
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
            is_phrase = False
            if len(text.split()) > 3:
                if nlp and any(token.pos_ in ["VERB", "AUX"] for token in nlp(text.lower()[:100])):
                    is_phrase = True
                elif len(text) > 40: 
                    is_phrase = True
            
            if is_phrase:
                politica_execucao = "TEXTO_LIVRE"

        # -----------------------------------------------------------------
        # TRATAMENTO PADRONIZADO E MASSIVO 
        # -----------------------------------------------------------------
        if politica_execucao == "IGNORAR":
            return text, None
            
        if politica_execucao == "NOME_SOLTO":
            fake_val = _get_fake(text, "NOME_SOLTO")
            return fake_val, ("TEXT" if fake_val != text else None)
      
        if politica_execucao in ["COORD", "COORD_SINGLE"]:
            if anon_location:
                fake_val = _get_fake(text, politica_execucao)
                return fake_val, ("TEXT" if fake_val != text else None)
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
        logger.error(f"⚠️ Erro crítico na máscara da coluna '{col_name}': {e}", exc_info=True)
        return str(val), None 

def reset_memory():
    _MAPPING_CACHE.clear()
    _COLUMN_POLICIES.clear()
    _OLLAMA_CACHE.clear()
    _NEGATIVE_PATTERN_CACHE.clear()
    _COLUMN_SAMPLES_ACCUMULATOR.clear()