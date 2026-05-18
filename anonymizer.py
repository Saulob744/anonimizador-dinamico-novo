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

logging.basicConfig(level=logging.ERROR, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

try:
    import spacy
    nlp = spacy.load("pt_core_news_lg")
except OSError:
    nlp = None

_MAPPING_CACHE = {}
fake = Faker("pt_BR")

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "llama3"

# =========================================================
# REGEX ESTRUTURAIS 
# =========================================================
REGEX = {
    "COORD": re.compile(r"-?\d{1,2}[.,]\d+\s*[,;]?\s*-?\d{1,3}[.,]\d+"), 
    "COORD_SINGLE": re.compile(r"^-?\d{1,3}[.,]\d{4,}$|^-\d{5,10}$"), 
    "EMAIL": re.compile(r"[\w\.-]+@[\w\.-]+", re.IGNORECASE),
    "CPF": re.compile(r"\d{3}[.\-\s]*\d{3}[.\-\s]*\d{3}[.\-\s]*\d{2}|\b\d{11}\b"),
    "RG": re.compile(r"(?:[A-Z]{2}[-\s]*)?\d{1,3}[.\-\s]*\d{3}[.\-\s]*\d{3}[-\s]*[0-9A-Z]|\b\d{5,14}\b", re.IGNORECASE),
    "PLATE": re.compile(r"[A-Z]{3}[-\s]?\d[A-Z0-9]\d{2}|[A-Z]{3}[-\s]?\d{4}", re.IGNORECASE),
    "PHONE": re.compile(r"(?:\+?55\s*)?(?:\(?\d{2,3}\)?[\s-]*)?\d{4,5}[-\s]*\d{4}"),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[a-fA-F0-9]{1,4}:){7}[a-fA-F0-9]{1,4}\b"),
    "GENERIC_CODE": re.compile(r'[A-Za-z0-9_/\.\-]{5,64}'),
}

CONTEXT_NAME_REGEX = re.compile(r"\b(nome|cliente|paciente|sr|sra|dr|dra|atendente|consultor|gerente|responsavel|usuario|agente|vendedor|motorista|funcionario|titular|favorecido|com|por|para|de)\s*[:\-]?\s+([A-ZÀ-Ÿa-zà-ÿ]{2,20}(?:\s+[A-ZÀ-Ÿa-zà-ÿ]{2,20}){0,4})\b", re.IGNORECASE)
NAME_REGEX = re.compile(r"\b(?:[A-ZÀ-Ÿ][a-zà-ÿ]{1,20}|[A-ZÀ-Ÿ]{2,20})(?:\s+(?:de|da|do|dos|das|e|DE|DA|DO|DOS|DAS|E))?(?:\s+(?:[A-ZÀ-Ÿ][a-zà-ÿ]{1,20}|[A-ZÀ-Ÿ]{2,20})){1,5}\b")
PREFIX_TRIMMER = re.compile(r"^(ao|a|para|dr\.?|dra\.?|sr\.?|sra\.?|senhor|senhora|em|na|no|de|do|da|vitima|autor|paciente|soldado|policial|rua|avenida|trevo|cia|agente|vendedor|motorista|funcionario)\s+", re.IGNORECASE)

# =========================================================
# AGENTE DE TRIAGEM 
# =========================================================
def classify_cell(text: str) -> str:
    text = text.strip()
    words = text.split()
    if not text or len(text) < 3: return "IGNORAR"
    text_lower = text.lower()
    if re.search(r'\b(rua|av|avenida|praça|rodovia|br-\d{3}|cep|logradouro|quadra|loteamento|bairro|complemento|andar)\b', text_lower): return "ENDERECO"
    if len(words) >= 3 or len(text) > 40: return "TEXTO_LIVRE"
    if REGEX["COORD"].search(text) or REGEX["COORD_SINGLE"].search(text): return "GPS"
    if REGEX["EMAIL"].search(text): return "EMAIL"
    if REGEX["CPF"].search(text): return "CPF"
    if REGEX["RG"].search(text): return "RG"
    if REGEX["PLATE"].search(text): return "PLACA"
    if REGEX["IP"].search(text): return "IP"
    if REGEX["PHONE"].search(text): return "PHONE"
    if REGEX["GENERIC_CODE"].search(text) and any(c.isdigit() for c in text): return "GENERIC_CODE"
    return "INCONCLUSIVO"

def profile_column_type(col_name: str, values_sample: list) -> str:
    try:
        c = str(col_name).lower().strip()
        blacklist = {"created_at", "updated_at", "deleted_at", "timestamp", "data_criacao"}
        if c in blacklist: return "IGNORAR"

        valid_strings = [str(v) for v in values_sample if v is not None and str(v).strip() != ""]
        if not valid_strings: return "IGNORAR"
            
        sample_to_test = random.sample(valid_strings, min(5, len(valid_strings)))
        resultados = [classify_cell(text) for text in sample_to_test]
        votos = Counter(resultados)
        hits_validos = {k: v for k, v in votos.items() if k not in ["IGNORAR", "INCONCLUSIVO"]}
        
        if hits_validos:
            if "TEXTO_LIVRE" in hits_validos: return "TEXTO_LIVRE"
            return max(hits_validos, key=hits_validos.get)
            
        amostra_str = " | ".join(sample_to_test)
        prompt_salvacao = (
            f"Analise esta amostra de banco de dados: '{amostra_str}'. "
            f"Escolha APENAS UMA opção predominante:\n- ENDERECO\n- CODIGO_SENSIVEL\n- DADO_PESSOAL\n- SEGURO"
        )
        try:
            resp = requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": prompt_salvacao, "stream": False}, timeout=40, proxies={"http": "", "https": ""})
            if resp.status_code == 200:
                answer = resp.json().get("response", "").strip().upper()
                if "SEGURO" in answer or "ENDERECO" in answer: return "IGNORAR"
                elif "CODIGO_SENSIVEL" in answer: return "GENERIC_CODE"
                elif "DADO_PESSOAL" in answer: return "TEXTO_LIVRE"
        except Exception: pass
        return "DESCONHECIDO"
    except Exception: return "IGNORAR"

# =========================================================
# AGENTE EXECUTOR
# =========================================================
@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper().strip()

def _is_hallucination(ent_text: str, is_context: bool = False) -> bool:
    ent_text = ent_text.strip(".,;:-\n ")
    if not ent_text or len(ent_text) <= 2: return True
    if any(c.isdigit() for c in ent_text) and any(c in ['.', ',', '-'] for c in ent_text): return True
    if not is_context and not any(c.isupper() for c in ent_text): return True
    if re.match(r"^([A-Z]\.){1,3}[A-Z]?$", ent_text.upper().strip()): return True
    
    termos_proibidos = {"CPF", "RG", "CNPJ", "CEP", "DOC", "DOCUMENTO", "TEL", "CEL", "TELEFONE", "CELULAR", "PIX", "CHAVE", "PLACA", "DR", "DRA", "SR", "SRA", "RUA", "AVENIDA", "AV", "SUPORTE", "VENDAS", "FINANCEIRO", "RISCO", "MAINFRAME", "FATURAMENTO", "ARQUITETURA", "SOC", "TEAM", "DEV", "GERENTE", "PLANTAO", "CONTATO", "FALE", "CONOSCO", "OUVIDORIA", "APP", "MOBILE", "UX", "UI", "DESIGN", "TESTES", "APRESENTOU", "COMPORTAMENTO", "SISTEMA", "RELATORIO", "PROJETO", "MÓDULO", "API", "GATEWAY", "NUVEM", "PORTAL", "CIDADÃO", "DASHBOARD", "CORE", "BANCÁRIO"}
    texto_norm = _normalize(ent_text).upper()
    if texto_norm in termos_proibidos or any(texto_norm.startswith(t + " ") for t in termos_proibidos): return True
    return False

def _detect_all(text: str, anon_loc: bool):
    found = []
    
    # ⚡ NOVA REGRA DE PRIORIDADE: Força o IP e CPF a ganharem do GENERIC_CODE
    PRIORITY = {
        "EMAIL": 1, "IP": 1, "CPF": 1, "RG": 1, "PHONE": 1, 
        "PLATE": 1, "COORD": 1, "COORD_SINGLE": 1, 
        "PER": 2, "GENERIC_CODE": 99
    }
    
    for typ, pat in REGEX.items():
        if not anon_loc and typ in ["COORD", "COORD_SINGLE"]: continue
        for match in pat.finditer(text):
            val = match.group()
            if typ == "GENERIC_CODE" and not any(char.isdigit() for char in val): continue
            found.append((match.start(), match.end(), val, typ))

    for match in CONTEXT_NAME_REGEX.finditer(text):
        val = match.group(2).strip()
        if not _is_hallucination(val, is_context=True):
            found.append((match.start(2), match.end(2), text[match.start(2):match.end(2)], "PER"))

    if nlp:
        is_upper = sum(1 for c in text if c.isupper()) > len(text) * 0.5
        doc = nlp(text.title() if is_upper else text)
        for ent in doc.ents:
            if ent.label_ == "PER":
                prefix_match = PREFIX_TRIMMER.search(ent.text)
                offset = prefix_match.end() if prefix_match else 0
                
                # ⚡ CORREÇÃO MATEMÁTICA DO SPACY
                val_raw = ent.text[offset:]
                val_clean = val_raw.strip()
                start_adj = val_raw.find(val_clean)
                start = ent.start_char + offset + (start_adj if start_adj != -1 else 0)
                
                if not _is_hallucination(val_clean, is_context=False): 
                    found.append((start, start + len(val_clean), text[start : start + len(val_clean)], "PER"))
    else:
        for match in NAME_REGEX.finditer(text):
            val = match.group().strip()
            val_clean = re.sub(r'\s+', ' ', val)
            if not _is_hallucination(val_clean, is_context=False): 
                found.append((match.start(), match.end(), val_clean, "PER"))

    # Ordenação Blindada: Posição > Tamanho > Prioridade Definida
    found.sort(key=lambda x: (x[0], -(x[1] - x[0]), PRIORITY.get(x[3], 50)))
    
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, text[s:e], t))
            last = e
    return clean

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
    elif typ == "GENERIC_CODE": 
        val = "".join([random.choice(string.digits) if c.isdigit() else (random.choice(string.ascii_uppercase) if c.isalpha() else c) for c in clean_value])
    else: val = fake.word().upper()

    _MAPPING_CACHE[cache_key] = val
    return val

# =========================================================
# 🤖 O NOVO AGENTE REVISOR
# =========================================================
def _agente_revisor(texto_original: str, texto_anonimizado: str) -> str:
    """O Agente de IA valida e corrige o trabalho do Pente Fino se necessário."""
    prompt = f"""Você é um Agente de Segurança de Dados rigoroso.
Texto Original: "{texto_original}"
Texto Modificado pela máquina: "{texto_anonimizado}"

SUA MISSÃO:
1. Verifique se o Texto Modificado ainda contém nomes reais, CPFs, ou dados sensíveis.
2. Verifique se a frase perdeu o sentido ou foi "esmagada".

REGRA DE RESPOSTA:
- Se o Texto Modificado estiver seguro e fizer sentido, responda APENAS com a palavra: APROVADO
- Se estiver com falhas (vazando dados ou sem sentido), CORRIJA o texto usando dados fictícios e responda APENAS com o texto corrigido. Sem explicações adicionais."""

    try:
        resp = requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}, timeout=15, proxies={"http": "", "https": ""})
        if resp.status_code == 200:
            resposta = resp.json().get("response", "").strip()
            if "APROVADO" not in resposta.upper():
                return resposta 
    except Exception as e:
        logger.error(f"Erro no Agente Revisor: {e}")
    
    return texto_anonimizado

# =========================================================
# ORQUESTRAÇÃO FINAL
# =========================================================
def anonymize_value(col_name: str, val, anon_location: bool = True, usar_agente_revisor: bool = False):
    try:
        if val is None or not str(val).strip(): return val, None
        text = str(val)
        if len(text) < 2: return text, None
        
        # 1. Ação do Executor (Máquina)
        entities = _detect_all(text, anon_location)
        if not entities: return text, None
        
        result, last = [], 0
        for s, e, v, t in entities:
            if s < last: continue
            result.append(text[last:s])
            result.append(_get_fake(v, t))
            last = e
        result.append(text[last:])
        texto_maquina = "".join(result)
        
        # 2. Ação do Agente Revisor
        if usar_agente_revisor and len(text.split()) >= 3:
            texto_final = _agente_revisor(text, texto_maquina)
        else:
            texto_final = texto_maquina

        return texto_final, ("TEXT" if texto_final != text else None)
    except Exception as e:
        logger.error(f"⚠️ Erro ao aplicar mascara na coluna '{col_name}': {e}")
        return str(val), None 

def reset_memory():
    _MAPPING_CACHE.clear()