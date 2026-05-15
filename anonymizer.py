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

try:
    import spacy
    nlp = spacy.load("pt_core_news_lg")
except OSError:
    logging.error("Modelo SpaCy não encontrado! Rode: python -m spacy download pt_core_news_lg")
    nlp = None

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

# =========================================================
# LISTAS DE BLOQUEIO E REGEX ESTRUTURAIS 
# =========================================================
MEDICAL_LEGAL_BLACKLIST = {
    "exame", "cavidade", "oral", "torax", "abdominal", "cervical", "ombro", "cotovelo", 
    "esquerdo", "direito", "membro", "inferior", "superior", "afundamento", "hemorragia", 
    "interna", "externo", "aguda", "sistema", "circulatorio", "respiratorio", "vasos", 
    "base", "hospital", "clinica", "boletim", "ocorrencia", "internamento", "distrito", 
    "ficha", "encaminhamento", "acidente", "transito", "medindo", "aproximadamente", 
    "regional", "processo", "penal", "delegacia", "atropelamento", "historico", "animal",
    "silvestre", "colidir", "tancredo", "neves", "coronel", "vivida", "hist", "rico", 
    "sinistro", "guaxinim", "battle", "politrauma", "forame", "magno", "encef", "cranio", 
    "craniano", "sinal", "infarto", "miocardio", "agudo", "gradil", "costal", "cavidades", 
    "pleurais", "interface", "toracoabdominal", "achado", "achados", "corporal", "geral", 
    "santa", "tereza", "formosa", "oeste", "ceo", "periorbit", "revestimento", "cut", "nio", 
    "local", "patol", "cef", "vitima", "autor", "estavam", "atendendo", "conforme", "apurado", 
    "irregurales", "energia", "mec", "petequias", "esclerais", "frontal", "axila", "coxa", 
    "hematoma", "subcapsular", "couro", "cabeludo", "outras", "les", "coluna", "escoria", 
    "rupturas", "multiplas", "mesenterio", "fraturas", "cominutivas", "via", "publica", 
    "feridas", "cortocontusas", "sinais", "atitude", "pugilista", "policia", "civil", "agente", 
    "oficial", "criminal", "escoriacoes", "subcutanea", "regiao", "lesao", "pr", "sc", "sp", 
    "rj", "iml", "pm", "prv", "prf", "pcpr", "pmpr", "detran", "samu", "siate", "upa", "sus", 
    "crm", "oab", "rg", "cpf", "trata", "se", "cad", "pe", "ba", "al", "cr", "hemorr", "morfol", 
    "test", "envolveu", "auto", "vinte", "trinta", "quarenta", "cinquenta", "sessenta", "setenta", 
    "oitenta", "noventa", "cem", "mil", "estendendo", "conclui", "zigom", "traqueia", "abd", 
    "clav", "supra", "infra", "terco", "distal", "proximal", "medial", "lateral", "anterior", 
    "posterior", "tibia", "fibula", "clavicula", "cadaver", "sexo", "masculino", "feminino",
    "esquerda", "extensa", "fossa", "iliaca", "hipogastrio", "coxas", "viatura", "pessoa",
    "estrada", "pulm", "dorso", "instrumento", "contundente", "eviscera", "coracao", "cicatriz", 
    "asa", "menor", "maior", "esfen", "tombamento", "laparotomia", "utero", "regi", "neo", 
    "falencia", "card", "pol", "lio", "ac", "sentido", "carlopolis", "tavora", "rodoviario", 
    "federal", "passageira", "condutor", "identificado", "como", "policial", "fratura", "munic", 
    "pio", "decorr", "tico", "ncia", "clico", "ortod", "hemit", "presen", "avuls", "foz", "igua", 
    "moreira", "sales", "ouro", "verde", "cafezal", "sul", "portes", "vila", "fcc", "cranioencef", 
    "es", "tor", "mandirituba", "roque", "pinhal", "estava", "sendo", "conduzido", "conduzindo", 
    "qual", "senhor", "senhora", "rua", "avenida", "trevo", "cia", "batalhao", "veiculo", "placa", 
    "placas", "gmvectra", "vectra", "bairro", "rodovia", "br", "km", "logradouro", "moto", "carro", 
    "gm", "costelas", "bilaterais", "toraco", "abdominais", "desloquei", "ate", "ponta", "grossa", 
    "guaratuba", "maringa", "stico", "cerebelar", "luxa", "abras", "acromio", "clavicular", "bra", 
    "impress", "vel", "medio"
}

REGEX = {
    "CPF": re.compile(r"(?<!\d)(?:\d{3}[.\-\s]?\d{3}[.\-\s]?\d{3}[.\-\s]?\d{2}|\d{11})(?!\d)"),
    "EMAIL": re.compile(r"[\w\.-]+@[\w\.-]+", re.IGNORECASE),
    "PHONE": re.compile(r"(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})[-\s]?\d{4}"),
    "PLATE": re.compile(r"\b[A-Z]{3}[-\s]?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "RG": re.compile(r"(?<!\d)(?:[A-Z]{2}[-\s]?)?\d{1,3}\.?\d{3}\.?\d{3}[-\s]?[0-9A-Z](?!\w)|(?<!\d)\d{5,11}(?!\d)", re.IGNORECASE),
    "COORD": re.compile(r"-?\d{1,2}\.\d+,\s*-?\d{1,3}\.\d+"),
    "COORD_SINGLE": re.compile(r"^-?\d{1,3}\.\d{4,}$|^-\d{5,10}$"), 
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
SUFFIX_TRIMMER = re.compile(r"[-.,\s]+(sim|nao|e|com|pr|sc|sp|rs|mg|rj)$", re.IGNORECASE)

# =========================================================
# JUIZ OLLAMA (PROMPTS "SE PARECE COM")
# =========================================================
@lru_cache(maxsize=10000)
def _ask_ollama_type(text: str, tipo_dado: str) -> bool:
    """Juiz LLM """
    prompts = {
        "CPF": f"Responda APENAS 'SIM' ou 'NAO': O texto '{text}' APARENTA SER um número de CPF válido (com ou sem pontuação)?",
        "RG": f"Responda APENAS 'SIM' ou 'NAO': O texto '{text}' SE PARECE COM um RG, CNH, Passaporte ou documento de identidade?",
        "PLACA": f"Responda APENAS 'SIM' ou 'NAO': O texto '{text}' SE PARECE COM uma placa de veículo?",
        "NOME": (
            f"Responda APENAS 'SIM' ou 'NAO': O texto '{text}' SE PARECE COM um nome próprio de pessoa humana? "
            f"Responda 'NAO' se for uma cidade, estado, país, endereço, empresa ou termo médico."
        )
    }
    
    if tipo_dado not in prompts:
        return True 

    prompt = f"Você é um classificador de dados focado em LGPD. {prompts[tipo_dado]}"
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
        logger.warning(f"Ollama falhou ao classificar '{tipo_dado}' em '{text}': {e}. Assumindo FALSE.")
        return False 
    
    return False

# =========================================================
# PROFILER DE COLUNAS 
# =========================================================
def classify_cell(text: str) -> str:
    text = str(text).strip()
    words = text.split()
    
    if not text or len(text) < 3: return "IGNORAR"
    if len(text) > 80 or len(words) > 8: return "TEXTO_LIVRE"
        
    # Email, GPS e Telefone são puramente matemáticos (Regex)
    if REGEX["EMAIL"].search(text) or "@" in text: return "EMAIL"
    if REGEX["PHONE"].search(text): return "PHONE"
    if REGEX["COORD"].search(text) or REGEX["COORD_SINGLE"].search(text): return "GPS"
    
    # CPF, RG e PLACA ganham aval da IA (com o prompt tolerante)
    if REGEX["CPF"].search(text) and _ask_ollama_type(text, "CPF"): return "CPF"
    if REGEX["RG"].search(text) and _ask_ollama_type(text, "RG"): return "RG"
    if REGEX["PLATE"].search(text) and _ask_ollama_type(text, "PLACA"): return "PLACA"
    
    if 3 <= len(text) <= 60 and 1 <= len(words) <= 6:
        if NAME_REGEX.search(text) and _ask_ollama_type(text, "NOME"): 
            return "NOME_SOLTO"
        
    if len(words) >= 3: return "TEXTO_LIVRE"
    
    return "DESCONHECIDO"

def profile_column_type(col_name: str, values_sample: list) -> str:
    c_lower = str(col_name).lower()
    
    blacklist_colunas = [
        "cidade", "municipio", "bairro", "estado", "uf", "pais", "cep", 
        "papel", "prefixo", "status", "situacao", "tipo", "cor", "marca", "modelo",
        "id", "uuid", "guid", "created_at", "updated_at"
    ]
    if c_lower in blacklist_colunas or c_lower.startswith("id_") or c_lower.endswith("_id"): return "IGNORAR"
        
    if "latitude" in c_lower or "longitude" in c_lower or c_lower in ["lat", "lon", "lng"]: return "GPS_SINGLE"
    if "renavam" in c_lower: return "RENAVAM"
    if "matricula" in c_lower: return "MATRICULA"

    valid_strings = [str(v) for v in values_sample if v and not isinstance(v, (bool, int, float))]
    if not valid_strings: return "IGNORAR"
        
    sample_to_test = random.sample(valid_strings, min(5, len(valid_strings)))
    resultados = [classify_cell(text) for text in sample_to_test]
    
    from collections import Counter
    votos = Counter(resultados)
    
    hits_validos = {k: v for k, v in votos.items() if k not in ["IGNORAR", "DESCONHECIDO"]}
    if hits_validos:
        if "TEXTO_LIVRE" in hits_validos: return "TEXTO_LIVRE"
        return max(hits_validos, key=hits_validos.get)
        
    return "DESCONHECIDO"

# =========================================================
# PROCESSAMENTO DE TEXTO LIVRE (COM IA)
# ========================================================
@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", "", text.upper().strip())

def _is_hallucination(ent_text: str) -> bool:
    ent_text = ent_text.strip(".,;:-\n ")
    if not ent_text or len(ent_text) <= 2: return True
    if not any(c.isupper() for c in ent_text): return True
    if re.match(r"^([A-Z]\.){1,3}[A-Z]?$", ent_text.upper().strip()): return True
    
    labels_proibidas = {"COR DOS OLHOS", "FOTOS EM ANEXO", "DATA DE", "SEXO MASCULINO", "LAUDO"}
    if ent_text.upper() in labels_proibidas: return True

    clean_text = _normalize(ent_text).lower()
    
    locais_exatos = {
        "sao paulo", "rio de janeiro", "curitiba", "parana", "brasil", "minas gerais", "bahia", 
        "santa catarina", "ceara", "pernambuco", "mato grosso", "goias", "amazonas", "espirito santo", 
        "porto alegre", "belo horizonte", "salvador", "fortaleza", "recife", "brasilia", "campinas"
    }
    if clean_text in locais_exatos: return True

    words = clean_text.split()
    if len(words) > 5: return True
        
    conectores_invalidos = {"com", "sem", "ao", "aos", "em", "no", "na", "pelo", "pela", "por", "para"}
    if any(w in conectores_invalidos for w in words): return True
    if any(w in MEDICAL_LEGAL_BLACKLIST for w in words): return True

    termos_institucionais = {
        "universidade", "faculdade", "escola", "colegio", "instituto", "fundacao", 
        "prefeitura", "secretaria", "ministerio", "tribunal", "vara", "forum", 
        "igreja", "paroquia", "banco", "unidade", "centro", "estado", "estadual", 
        "federal", "municipal", "nacional", "departamento", "conselho", "clube", "associacao"
    }
    if any(w in termos_institucionais for w in words): return True

    if not _ask_ollama_type(ent_text, "NOME"): return True 

    return False

def _detect_all(text: str, anon_loc: bool):
    found = []

    # 1. Regex Estruturais
    for typ, pat in REGEX.items():
        if not anon_loc and typ in ["COORD", "COORD_SINGLE"]: continue
        for match in pat.finditer(text):
            val = match.group()
            
            # Se for Email, Phone ou Coord, confia cega na Regex. Senão, pergunta pro Ollama.
            if typ in ["COORD", "COORD_SINGLE", "EMAIL", "PHONE"]:
                found.append((match.start(), match.end(), val, typ))
            else:
                if _ask_ollama_type(val, typ):
                    found.append((match.start(), match.end(), val, typ))

    # 2. SpaCy 
    if nlp:
        doc = nlp(text)
        for ent in doc.ents:
            if ent.label_ == "PER":
                nome_limpo = PREFIX_TRIMMER.sub("", ent.text).strip()
                if not _is_hallucination(nome_limpo):
                    found.append((ent.start_char, ent.end_char, nome_limpo, "PER"))

    # 3. Regex Nome
    for match in NAME_REGEX.finditer(text):
        val = match.group().strip()
        val_clean = re.sub(r'\s+', ' ', val)
        if not _is_hallucination(val_clean):
            found.append((match.start(), match.end(), val_clean, "PER"))

    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, text[s:e], t))
            last = e
            
    return clean

# =========================================================
# GERAÇÃO DE FAKES E INTERFACE PÚBLICA
# =========================================================
def _get_fake(value: str, typ: str) -> str:
    clean_value = html.unescape(re.sub(r'<[^>]+>', '', value)).strip()
    norm_val = _normalize(clean_value)
    cache_key = f"{typ}:{norm_val}"
    
    if cache_key in _MAPPING_CACHE: 
        return _MAPPING_CACHE[cache_key]

    seed = int(hashlib.sha256(norm_val.encode()).hexdigest()[:8], 16)
    fake.seed_instance(seed)
    
    if typ == "PER" or typ == "NOME_SOLTO":
        prefix = ""
        u_val = clean_value.upper()
        if "DR." in u_val: prefix = "DR. "
        elif "DRA." in u_val: prefix = "DRA. "
        val = prefix + fake.name().upper()
    elif typ == "CPF": 
        val = fake.cpf()
    elif typ == "RG": 
        val = fake.numerify('##.###.###-#') 
    elif typ == "PLATE" or typ == "PLACA": 
        val = fake.license_plate().upper()
    elif typ == "EMAIL":
        val = fake.email().lower()
    elif typ == "PHONE":
        val = fake.phone_number()
    elif typ == "RENAVAM":
        val = fake.numerify('###########')
    elif typ == "MATRICULA":
        val = fake.numerify('######')
    else: 
        val = fake.word().upper()

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
    targets = ["nome", "vitima", "autor", "cpf", "rg", "placa", "condutor", "proprietario", "email", "mail", "contato"]
    return any(k in c for k in targets)

def reset_memory():
    _MAPPING_CACHE.clear()
    _USED_FAKES.clear()

def filtrar_nomes_proprios(texto_html):
    entities = _detect_all(texto_html, True)
    return [v for s, e, v, t in entities if t == "PER"]