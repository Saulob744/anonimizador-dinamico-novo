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
_NEGATIVE_PATTERN_CACHE = set()

def _get_skeleton(text: str) -> str:
    return re.sub(r'\d+', '[NUM]', text.upper())
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
    r"\b[A-ZÀ-Ÿ][a-zà-ÿ]+" 
    r"(?:\s+(?:de|da|do|dos|das|e))?"       
    r"(?:\s+[A-ZÀ-Ÿ][a-zà-ÿ]+){1,5}\b"      
)

# =========================================================
# 1. MOTOR DE COMUNICAÇÃO COM A IA
# =========================================================
def _ask_ollama_sim_nao(pergunta: str, cache_key: str) -> bool:
    
    if cache_key in _OLLAMA_CACHE: return _OLLAMA_CACHE[cache_key]
    
    termo_avaliado = cache_key.split(":")[-1] 
    esqueleto = _get_skeleton(termo_avaliado)
    
    if esqueleto in _NEGATIVE_PATTERN_CACHE:
        logger.debug(f"🛑 Bloqueado por Padrão: {termo_avaliado} ({esqueleto})")
        return False
    
    prompt = f"{pergunta} Responda APENAS 'SIM' ou 'NAO', sem explicações ou pontuação."
    logger.debug(f"🧠 IA Julgando: {pergunta}")
    
    try:
        payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0}}
        resp = requests.post(OLLAMA_URL, json=payload, timeout=40, proxies={"http": "", "https": ""})
        if resp.status_code == 200:
            is_valid = "SIM" in resp.json().get("response", "").strip().upper()
            _OLLAMA_CACHE[cache_key] = is_valid 
            
            if not is_valid:
                _NEGATIVE_PATTERN_CACHE.add(esqueleto)
                
            return is_valid
    except Exception as e:
        logger.warning(f"Timeout/Erro no Ollama. Falha segura ativada. Erro: {e}")
        
    return False

def _is_valid_entity_ollama(text: str, tag: str) -> bool:
  
    if len(text.strip()) < 3: return False
    
    if tag == "PER":
        pergunta = f"A expressão '{text}' é o NOME PRÓPRIO de uma pessoa humana real?"
    elif tag == "GENERIC_CODE":
        pergunta = f"A expressão '{text}' parece ser um código de identificação, chassi ou senha sensível?"
    else:
        return True 

    return _ask_ollama_sim_nao(pergunta, f"{tag}:{text.upper()}")

# =========================================================
# 2. INTELIGÊNCIA DE PERFILAMENTO DA COLUNA 
# =========================================================
def _smart_title(text: str) -> str:
    if not text: return text
    letras = sum(1 for c in text if c.isalpha())
    if letras == 0: return text
    
    excecoes = {"de", "da", "do", "dos", "das", "e"}
    
    partes = re.split(r'(\s+)', text) 
    
    for i in range(len(partes)):
        if not partes[i].strip():
            continue
            
        word_lower = partes[i].lower()
        if word_lower in excecoes:
            partes[i] = word_lower
        else:
           
            partes[i] = partes[i].capitalize()
            
    return "".join(partes)


def define_column_policy(col_name: str, samples: list) -> str:
    """
    Define a política de anonimização de uma coluna inteira.
    Usa um sistema de pontuação baseado em Regex/NLP e usa o Ollama como balança final.
    """
    # 0. Verifica o Cache: Se já julgamos essa coluna, não gasta processamento
    if col_name in _COLUMN_POLICIES:
        return _COLUMN_POLICIES[col_name]

    valid_samples = [str(s).strip() for s in samples if str(s).strip()]
    if not valid_samples:
        return "IGNORAR"

    amostra_base = valid_samples[0]
    # Junta as amostras (até 3) para dar um bom contexto ao Ollama depois
    amostra_conjunta = " | ".join(valid_samples[:3])

    # 1. TEXTO LIVRE DIRETO (Se for muito longo, não gastamos Regex à toa)
    if len(amostra_base) > 50 or len(amostra_base.split()) > 4:
        _COLUMN_POLICIES[col_name] = "TEXTO_LIVRE"
        return "TEXTO_LIVRE"

    # 2. SISTEMA DE PONTUAÇÃO
    pontuacao = {key: 0 for key in REGEX.keys()}
    pontuacao["NOME_SOLTO"] = 0

    for amostra in valid_samples:
        # A. Pontua Regex Estrutural
        for typ, pat in REGEX.items():
            if pat.search(amostra):
                if typ in ["GENERIC_CODE", "CHASSI"] and not any(c.isdigit() for c in amostra):
                    continue
                pontuacao[typ] += 1
        
        # B. Pontua Nomes (Regex + spaCy)
        texto_formatado = _smart_title(amostra)
        parece_nome = False
        if NAME_FALLBACK_REGEX.search(texto_formatado):
            parece_nome = True
        elif nlp and any(ent.label_ == "PER" for ent in nlp(texto_formatado).ents):
            parece_nome = True
            
        if parece_nome:
            pontuacao["NOME_SOLTO"] += 1

    # Descobre qual tipo teve a maior pontuação
    melhor_tipo = max(pontuacao, key=pontuacao.get)
    maior_pontuacao = pontuacao[melhor_tipo]

    # 3. OLLAMA COMO BALANÇA / JUIZ FINAL (Executado apenas 1x por coluna)
    if maior_pontuacao > 0:
        if melhor_tipo == "NOME_SOLTO":
            pergunta = f"A coluna '{col_name}' com os dados '{amostra_conjunta}' contém NOMES PRÓPRIOS de pessoas reais?"
            if _ask_ollama_sim_nao(pergunta, f"COL_PER:{col_name}:{amostra_conjunta}"):
                logger.debug(f"⚖️ Balança Ollama: Confirmou NOME_SOLTO para a coluna '{col_name}'")
                _COLUMN_POLICIES[col_name] = "NOME_SOLTO"
                return "NOME_SOLTO"
        else:
            pergunta = f"A coluna '{col_name}' com os dados '{amostra_conjunta}' parece ser do tipo estruturado {melhor_tipo}?"
            if _ask_ollama_sim_nao(pergunta, f"COL_TYPE:{melhor_tipo}:{col_name}"):
                logger.debug(f"⚖️ Balança Ollama: Confirmou {melhor_tipo} para a coluna '{col_name}'")
                _COLUMN_POLICIES[col_name] = melhor_tipo
                return melhor_tipo

    # 4. FALLBACK: PERGUNTA GENÉRICA DE DADO SENSÍVEL
    # Se o regex achou algo mas o Ollama negou, ou se o regex não achou nada, fazemos uma última checagem
    pergunta_sensivel = f"A coluna '{col_name}' com os dados '{amostra_conjunta}' contém informações pessoais sensíveis que precisam ser mascaradas?"
    if _ask_ollama_sim_nao(pergunta_sensivel, f"COL_SENSITIVE:{col_name}:{amostra_conjunta}"):
        logger.debug(f"⚖️ Balança Ollama: Classificou '{col_name}' como TEXTO_LIVRE sensível genérico")
        _COLUMN_POLICIES[col_name] = "TEXTO_LIVRE"
        return "TEXTO_LIVRE"

    # 5. SEGURO: Ignorar coluna para poupar processamento
    _COLUMN_POLICIES[col_name] = "IGNORAR"
    return "IGNORAR"

# =========================================================
# 3. MOTOR DE TEXTO LIVRE 
# =========================================================
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
        "ESTORNO", "DEVOLUCAO", "VALOR", "TARIFA", "TAXA", "VIATURA", "VEICULO"
    }
    texto_norm = _normalize(ent_text)
    partes = texto_norm.split()
    
    if all(p in termos_proibidos for p in partes): return True
    if texto_norm in termos_proibidos or any(texto_norm.startswith(t + " ") for t in termos_proibidos): return True
    return False

def _clean_extracted_name(name_text: str) -> str:
    words = name_text.split()
    valid_words = []
    preposicoes = {"de", "da", "do", "dos", "das", "e"}
    for w in words:
        w_clean = w.strip(".,;:!?()\"'")
        if not w_clean: continue
        valid_words.append(w)
    return " ".join(valid_words).strip(".,;:!?()\"' ")

def _detect_all(text: str, anon_loc: bool):
    found = []
    PRIORITY = { "EMAIL": 1, "IP": 1, "CPF": 1, "CHASSI": 1, "RG": 2, "PHONE": 2, "PLATE": 2, "COORD": 3, "COORD_SINGLE": 3, "PER": 4, "GENERIC_CODE": 99 }
    
    text_analise = _smart_title(text)
    
    for typ, pat in REGEX.items():
        if not anon_loc and typ in ["COORD", "COORD_SINGLE"]: continue
        for match in pat.finditer(text):
            val = match.group()
            if typ == "GENERIC_CODE":
                if not any(c.isdigit() for c in val) or len(val) < 6: continue
                if not _is_valid_entity_ollama(val, "GENERIC_CODE"): continue
            found.append((match.start(), match.end(), val, typ))

    # DENTRO DA FUNÇÃO _detect_all
    for match in CONTEXT_NAME_REGEX.finditer(text_analise):
        val = match.group(2).strip()
        val_clean = _clean_extracted_name(val)
        
        if val_clean and not _is_hallucination_basic(val_clean):
            
           
            words = val_clean.split()
            while len(words) >= 1:
                candidate = " ".join(words)
                
                if len(words) == 1 and candidate.lower() in ["de", "da", "do", "dos", "das", "e"]:
                    break
                
                if _is_valid_entity_ollama(candidate, "PER"):
                    start = match.start(2) + val.find(candidate)
                    texto_original = text[start:start + len(candidate)]
                    found.append((start, start + len(candidate), texto_original, "PER"))
                    break
                words.pop()

    if nlp:
        doc = nlp(text_analise)
        for ent in doc.ents:
            if ent.label_ == "PER":
                if any(token.pos_ in ["VERB", "PRON", "PUNCT", "SYM"] for token in ent): continue
                prefix_match = PREFIX_TRIMMER.search(ent.text)
                offset = prefix_match.end() if prefix_match else 0
                val_raw = ent.text[offset:]
                val_clean = _clean_extracted_name(val_raw)
                if not val_clean: continue
                
                start_adj = ent.text[offset:].find(val_clean)
                start = ent.start_char + offset + (start_adj if start_adj != -1 else 0)
                texto_original = text[start : start + len(val_clean)] 
                
                if not _is_hallucination_basic(texto_original): 
                    if _is_valid_entity_ollama(texto_original, "PER"):
                        found.append((start, start + len(val_clean), texto_original, "PER"))

    for match in NAME_FALLBACK_REGEX.finditer(text_analise):
        val = match.group().strip()
        val_clean = _clean_extracted_name(val)
        if val_clean and not _is_hallucination_basic(val_clean):
            if _is_valid_entity_ollama(val_clean, "PER"):
                start = match.start() + val.find(val_clean)
                texto_original = text[start:start + len(val_clean)]
                found.append((start, start + len(val_clean), texto_original, "PER"))

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

    try:
        seed_hex = hashlib.sha256(norm_val.encode()).hexdigest()[:8]
        seed_int = int(seed_hex, 16) if seed_hex.strip() else random.randint(1, 99999)
        fake.seed_instance(seed_int)
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
            
        # --- PERFILAMENTO INTELIGENTE ---
        if col_name not in _COLUMN_POLICIES:
          
            politica = define_column_policy(col_name, [text])
        else:
            politica = _COLUMN_POLICIES[col_name]

        # --- ROTEAMENTO EXECUTOR ---
        politica_execucao = politica

        if politica_execucao not in ["TEXTO_LIVRE", "IGNORAR"]:
            e_texto_longo = len(text) > 50 or len(text.split()) > 4
            
            tem_estrutura_frase = False
            if not e_texto_longo and nlp:
                doc = nlp(_smart_title(text))
                tem_per = any(ent.label_ == "PER" for ent in doc.ents)
                tem_action = any(token.pos_ in ["VERB", "ADJ", "AUX"] for token in doc)
                if tem_per and tem_action:
                    tem_estrutura_frase = True

            if e_texto_longo or tem_estrutura_frase:
                if not NAME_FALLBACK_REGEX.fullmatch(_smart_title(text)):
                    politica_execucao = "TEXTO_LIVRE"

        if politica_execucao == "IGNORAR":
            return text, None
            
        if politica_execucao in ["CPF", "RG", "EMAIL", "PLATE", "PHONE", "IP", "CHASSI", "GENERIC_CODE", "NOME_SOLTO", "COORD", "COORD_SINGLE"]:
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

    except Exception as e:
        logger.error(f"⚠️ Erro ao aplicar mascara na coluna '{col_name}': {e}", exc_info=True)
        return str(val), None 

def reset_memory():
    _MAPPING_CACHE.clear()
    _COLUMN_POLICIES.clear()
    _OLLAMA_CACHE.clear()
    _DYNAMIC_SAMPLES.clear()
