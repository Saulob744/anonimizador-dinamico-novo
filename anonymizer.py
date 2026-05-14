import re
import random
import string
import unicodedata
import hashlib
import logging
import spacy
import requests
from functools import lru_cache
from faker import Faker
from bs4 import BeautifulSoup
import html
import os

# =========================================================
# CONFIGURAÇÕES GLOBAIS E INICIALIZAÇÃO
# =========================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_MAPPING_CACHE = {}
_USED_FAKES = set()
fake = Faker("pt_BR")

# Configuração Ollama
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "llama3"

try:
    nlp = spacy.load("pt_core_news_lg")
except OSError:
    logger.error("Modelo SpaCy não encontrado! Rode: python -m spacy download pt_core_news_lg")
    nlp = None

MEDICAL_LEGAL_BLACKLIST = {
    # Termos médicos, legais e anatômicos
    "exame", "cavidade", "oral", "torax", "abdominal", "cervical", "ombro", "cotovelo", 
    "esquerdo", "direito", "membro", "inferior", "superior", "afundamento", "hemorragia", 
    "interna", "externo", "aguda", "sistema", "circulatorio", "respiratorio", "vasos", 
    "base", "hospital", "clinica", "boletim", "ocorrencia", "internamento", "distrito", 
    "ficha", "encaminhamento", "acidente", "transito", "medindo", "aproximadamente", 
    "regional", "processo", "penal", "delegacia", "atropelamento", "historico",
    "animal", "silvestre", "colidir", "tancredo", "neves", "coronel", "vivida",
    "hist", "rico", "sinistro", "guaxinim", "battle", "politrauma", "forame", "magno", 
    "encef", "cranio", "craniano", "sinal", "infarto", "miocardio", "agudo", "gradil", 
    "costal", "cavidades", "pleurais", "interface", "toracoabdominal", "achado", "achados", 
    "corporal", "geral", "santa", "tereza", "formosa", "oeste", "ceo", "periorbit", 
    "revestimento", "cut", "nio", "local", "patol", "cef", "vitima", "autor", "estavam", 
    "atendendo", "conforme", "apurado", "irregurales", "energia", "mec", "petequias", 
    "esclerais", "frontal", "axila", "coxa", "hematoma", "subcapsular", "couro", "cabeludo", 
    "outras", "les", "coluna", "escoria", "rupturas", "multiplas", "mesenterio", "fraturas", 
    "cominutivas", "via", "publica", "feridas", "cortocontusas", "sinais", "atitude", 
    "pugilista", "policia", "civil", "agente", "oficial", "criminal", "escoriacoes", 
    "subcutanea", "regiao", "lesao", "pr", "sc", "sp", "rj", "iml", "pm", "prv", "prf", 
    "pcpr", "pmpr", "detran", "samu", "siate", "upa", "sus", "crm", "oab", "rg", "cpf",
    "trata", "se", "cad", "pe", "ba", "al", "cr", "hemorr", "morfol", "test", "envolveu",
    "auto", "vinte", "trinta", "quarenta", "cinquenta", "sessenta", "setenta", "oitenta", 
    "noventa", "cem", "mil", "estendendo", "conclui", "zigom", "traqueia", "abd", "clav", 
    "supra", "infra", "terco", "distal", "proximal", "medial", "lateral", "anterior", 
    "posterior", "tibia", "fibula", "clavicula", "cadaver", "sexo", "masculino", "feminino",
    "esquerda", "extensa", "fossa", "iliaca", "hipogastrio", "coxas", "viatura", "pessoa",
    "estrada", "pulm", "dorso", "instrumento", "contundente", "eviscera", "coracao",
    "cicatriz", "asa", "menor", "maior", "esfen", "tombamento", "laparotomia", "utero",
    "regi", "neo", "falencia", "card", "pol", "lio", "ac", "sentido", "carlopolis", 
    "tavora", "rodoviario", "federal", "passageira", "condutor", "identificado", "como", "policial","fratura",
    "munic", "pio", "decorr", "tico", "ncia", "clico", "ortod", "hemit", "presen", "avuls",
    "foz", "igua", "moreira", "sales", "ouro", "verde", "cafezal", "sul", "portes", "vila","munic", "pio", "decorr", "tico", "ncia", "clico", "ortod", "hemit", "presen", "avuls",
    "foz", "igua", "moreira", "sales", "ouro", "verde", "cafezal", "sul", "portes", "vila",
    "fcc", "cranioencef", "es", "tor", "mandirituba", "roque", "pinhal", 
    "estava", "sendo", "conduzido", "conduzindo", "qual", "senhor", "senhora"
    "rua","ES CRANIOENCEF", "avenida", "trevo", "cia", "batalhao", "veiculo", "placa", "placas", "gmvectra", "vectra", "bairro", "rodovia", "br", "km", "logradouro", "moto", "carro", "gm","costelas", "bilaterais", "toraco", "abdominais", "desloquei", "ate", "ponta", "grossa", 
    "mandirituba", "guaratuba", "maringa", "stico", "fcc", "cranioencef", "cerebelar", 
    "luxa", "abras", "rua", "avenida", "veiculo", "placa", "placas", "gmvectra", "qual", 
    "estava", "sendo", "conduzido", "senhor", "senhora","acromio", "clavicular", "bra", "impress", "vel", "medio", "terco"
}


# =========================================================
# INTEGRAÇÃO OLLAMA 
# =========================================================
@lru_cache(maxsize=10000)
def _ask_ollama_is_name(text: str) -> bool:
    prompt = (
        f"Você é um juiz de dados ultrarigoroso. "
        f"Responda APENAS 'SIM' se o texto a seguir for CLARAMENTE um nome próprio completo de pessoa humana. "
        f"Responda 'NAO' se for veículo, placa de carro, endereço, nome de rua, rodovia, empresa, instituição (como CIA), parte do corpo, termo médico, cargo, cidade, ou fragmento de texto.\n"
        f"Texto: '{text}'"
    )
    
    # Trava do proxy local 
    proxies_vazios = {"http": "", "https": ""}
    
    try:
        response = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False
        }, timeout=40, proxies=proxies_vazios)
        
        if response.status_code == 200:
            answer = response.json().get("response", "").strip().upper()
            return "SIM" in answer or "YES" in answer
            
    except Exception as e:
        logger.warning(f"Ollama falhou ao analisar '{text}': {e}. Assumindo FALSE para evitar mascarar fragmentos.")
        return False 
    
    return False

# =========================================================
# REGEX DE DADOS ESTRUTURADOS
# =========================================================
REGEX = {
    "CPF": re.compile(r"\b(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(\d{2}\)|\d{2})\s?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "RG": re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}-?[0-9X]\b"),
    "COORD": re.compile(r"-?\d{1,2}\.\d+,\s*-?\d{1,3}\.\d+"),
}

NAME_REGEX = re.compile(
    r"(?<![a-zA-ZÀ-ÿ])"
    r"(?:"
        r"(?:[A-ZÀ-Ÿ]{2,}\s+(?:DE\s+|DA\s+|DO\s+|DOS\s+|DAS\s+|E\s+)?)+[A-ZÀ-Ÿ]{2,}"
        r"|"
        r"(?:[A-ZÀ-Ÿ][a-zà-ÿ]{1,}\s+(?:de\s+|da\s+|do\s+|dos\s+|das\s+|e\s+)?)+[A-ZÀ-Ÿ][a-zà-ÿ]{1,}"
    r")"
    r"(?![a-zA-ZÀ-ÿ])"
)

PREFIX_TRIMMER = re.compile(r"^(ao|a|para|dr\.?|dra\.?|sr\.?|sra\.?|senhor|senhora|em|na|no|de|do|da|vitima|autor|paciente|soldado|policial|rua|avenida|trevo|cia)\s+", re.IGNORECASE)

# Corta sujeira no FINAL do nome (ex: "RUY SIM" -> "RUY", "GUSTAVO-PR" -> "GUSTAVO")
SUFFIX_TRIMMER = re.compile(r"[-.,\s]+(sim|nao|e|com|pr|sc|sp|rs|mg|rj)$", re.IGNORECASE)# =========================================================
# UTILITÁRIOS E FILTROS
# =========================================================
def _preprocess_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r'<[^>]+>', lambda m: ' ' * len(m.group()), text)
    def replace_entity(m):
        ent = m.group()
        decoded = html.unescape(ent)
        return decoded + (' ' * (len(ent) - len(decoded)))
    text = re.sub(r'&[a-z0-9#]+;', replace_entity, text, flags=re.IGNORECASE)
    return text

@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", "", text.upper().strip())

def _is_hallucination(ent_text: str) -> bool:
    # LIMPEZA: Remove pontuações grudadas
    ent_text = ent_text.strip(".,;:-\n ")

    if not ent_text or len(ent_text) <= 3: return True
    if not any(c.isupper() for c in ent_text): return True
    if re.match(r"^([A-Z]\.){1,3}[A-Z]?$", ent_text.upper().strip()): return True
    
    labels_proibidas = {"COR DOS OLHOS", "FOTOS EM ANEXO", "DATA DE", "SEXO MASCULINO", "LAUDO"}
    if ent_text.upper() in labels_proibidas: return True

    clean_text = _normalize(ent_text).lower()
    words = clean_text.split()
    
    # TRAVA DE TAMANHO: Se tiver mais de 5 palavras, é uma frase, não um nome.
    if len(words) > 5:
        return True
        
    conectores_invalidos = {"com", "sem", "ao", "aos", "em", "no", "na", "pelo", "pela", "por", "para"}
    if any(w in conectores_invalidos for w in words):
        return True
    
    # TRAVA ESTÁTICA
    if any(w in MEDICAL_LEGAL_BLACKLIST for w in words):
        return True
    termos_institucionais = {
        "universidade", "faculdade", "escola", "colegio", "instituto", "fundacao", 
        "prefeitura", "secretaria", "ministerio", "tribunal", "vara", "forum", 
        "igreja", "paroquia", "banco", "unidade", "centro", "estado", "estadual", 
        "federal", "municipal", "nacional", "departamento", "conselho", "clube",
        "condominio", "edificio", "residencial", "comercial", "sindicato", "associacao", "CICO CONTUSO","CM DE EXENS","DO FEMUR DIR","dir", "esq", "inf", "sup", "ant", "post", "lat", "med", "prox", "dist",
        "direito", "esquerdo", "inferior", "superior", "anterior", "posterior", "lateral", "medial"
    }
    if any(w in termos_institucionais for w in words):
        return True

    # ANÁLISE DO SPACY
    if nlp:
        test_doc = nlp(ent_text.lower())
        palavras_comuns = 0
        palavras_validas = [w for w in test_doc if w.text not in ["de", "da", "do", "dos", "das", "e"]]
        
        for token in palavras_validas:
            if token.pos_ in ["NOUN", "ADJ", "VERB", "ADV", "NUM", "PRON", "SYM"]:
                palavras_comuns += 1
                
        if palavras_validas and palavras_comuns == len(palavras_validas):
            return True

    # JUIZ OLLAMA
    if not _ask_ollama_is_name(ent_text):
        return True 

    return False

# =========================================================
# MOTOR DE DETECÇÃO HÍBRIDA
# =========================================================
def _detect_all(text: str, anon_loc: bool):
    found = []
    det_text = _preprocess_text(text)

    # 1. Regex de Estruturas Fixas (CPF, Placas, etc)
    for typ, pat in REGEX.items():
        for match in pat.finditer(text):
            found.append((match.start(), match.end(), match.group(), typ))

    # 2. SpaCy (Deixamos ele achar o que quiser)
    if nlp:
        doc = nlp(det_text)
        for ent in doc.ents:
            if ent.label_ == "PER":
                nome_limpo = PREFIX_TRIMMER.sub("", ent.text).strip()
                if not _is_hallucination(nome_limpo):
                    found.append((ent.start_char, ent.end_char, ent.text, "PER"))

    # 3. Regex Customizada de Nomes (Deixamos ela achar o que quiser também)
    for match in NAME_REGEX.finditer(det_text):
        val = match.group().strip()
        val_clean = re.sub(r'\s+', ' ', val)
        if not _is_hallucination(val_clean):
            # Removemos a "trava" que impedia ele de adicionar nomes sobrepostos
            found.append((match.start(), match.end(), val, "PER"))

    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, text[s:e], t))
            last = e
            
    return clean

# =========================================================
# GERAÇÃO E INTERFACE
# =========================================================
def _get_fake(value: str, typ: str) -> str:
    clean_value = html.unescape(re.sub(r'<[^>]+>', '', value)).strip()
    norm_val = _normalize(clean_value)
    cache_key = f"{typ}:{norm_val}"
    
    if cache_key in _MAPPING_CACHE: 
        return _MAPPING_CACHE[cache_key]

    seed = int(hashlib.sha256(norm_val.encode()).hexdigest()[:8], 16)
    fake.seed_instance(seed)
    
    if typ == "PER":
        prefix = ""
        u_val = clean_value.upper()
        if "DR." in u_val: prefix = "DR. "
        elif "DRA." in u_val: prefix = "DRA. "
        val = prefix + fake.name().upper()
    elif typ == "CPF": val = fake.cpf()
    elif typ == "PLATE": val = fake.license_plate().upper()
    else: val = fake.word().upper()

    _MAPPING_CACHE[cache_key] = val
    return val

def anonymize_text(text: str, anon_loc: bool = True) -> str:
    if not isinstance(text, str) or len(text) < 3: return text
    entities = _detect_all(text, anon_loc)
    
    result, last = [], 0
    for s, e, v, t in entities:
        result.append(text[last:s])
        result.append(_get_fake(v, t))
        last = e
    result.append(text[last:])
    return "".join(result)

def anonymize_value(col_name: str, val, anon_location: bool = True):
    if val is None or not isinstance(val, str):
        return val, None
    new_v = anonymize_text(val, anon_location)
    return new_v, ("TEXT" if new_v != val else None)

def should_anonymize_column(col_name: str, sample_values) -> bool:
    c = col_name.lower()
    targets = ["nome", "vitima", "autor", "cpf", "rg", "placa", "condutor", "proprietario"]
    return any(k in c for k in targets)

def reset_memory():
    _MAPPING_CACHE.clear()
    _USED_FAKES.clear()

def filtrar_nomes_proprios(texto_html):
    entities = _detect_all(texto_html, True)
    return [v for s, e, v, t in entities if t == "PER"]