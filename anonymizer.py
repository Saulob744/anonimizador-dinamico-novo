import re
import random
import string
import unicodedata
import hashlib
import logging
import requests
import json
from functools import lru_cache
from faker import Faker

# =========================================================
# CONFIGURAÇÕES DE LOG E AMBIENTE
# =========================================================
logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
logger = logging.getLogger(__name__)

_MAPPING_CACHE = {}
_USED_FAKES = set()
fake = Faker("pt_BR")
CACHE_LIMIT = 500000

# =========================================================
# BLACKLIST EXPANDIDA (O ESCUDO PRINCIPAL)
# =========================================================
MEDICAL_LEGAL_BLACKLIST = {
    "exame", "exames", "cavidade", "oral", "torax", "abdominal", "cervical", "ombro", 
    "cotovelo", "esquerdo", "direito", "membro", "membros", "inferior", "inferiores", 
    "superior", "superiores", "afundamento", "hemorragia", "interna", "externo", 
    "aguda", "sistema", "circulatorio", "respiratorio", "vasos", "base", 
    "hospital", "clinica", "boletim", "ocorrencia", "internamento", "distrito", 
    "ficha", "encaminhamento", "acidente", "transito", "medindo", "aproximadamente", 
    "regional", "processo", "penal", "delegacia", "atropelamento", "historico",
    "animal", "silvestre", "colidir", "tancredo", "neves", "coronel", "vivida",
    "hist", "rico", "sinistro", "guaxinim", "battle", "politrauma", "forame", "magno", 
    "encef", "cranio", "craniano", "sinal", "infarto", "miocardio", "agudo", "gradil", 
    "costal", "cavidades", "pleurais", "interface", "toracoabdominal", "toracoabdominais",
    "achado", "achados", "corporal", "geral", "santa", "tereza", "formosa", "oeste", 
    "ceo", "periorbit", "revestimento", "cut", "nio", "local", "patol", "cef", "vitima", "autor",
    "complementar", "complementares", "quesito", "quesitos", "pergunta", "resposta",
    "viatura", "veiculo", "centro", "bairro", "desconhecido", "policial", "equipe",
    "rio", "janeiro", "sao", "paulo", "curitiba", "parana", "brasil", "rua", "avenida", "praça",
    "cor", "olho", "olhos", "cabelo", "cabelos", "pele", "cutis", "idade", "sexo", 
    "profissao", "naturalidade", "nacionalidade", "estado", "civil", "endereco", 
    "telefone", "celular", "email", "data", "nascimento", "trabalho", "pai", "mae",
    "marca", "modelo", "placa", "chassi", "renavam", "ano", "fabricacao",
    "altura", "peso", "tatuagem", "cicatriz", "vestes", "trajes", "fisico", "compleicao",
    "tamponamento", "card", "edema", "otorragia", "lesao", "les", "decorrente", "aco",
    "ferimento", "morte", "obito", "fratura", "estomago", "pulmao", "trauma", "descrição",
    "observação", "pergunta", "resposta", "opção", "selecionada", "coleta", "dna"
}

# =========================================================
# REGEX E APOIO
# =========================================================
REGEX = {
    "CPF": re.compile(r"\b(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(\d{2}\)|\d{2})\s?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "RG": re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}-?[0-9X]\b"),
}

# Regex de nomes focada em evitar capturar frases inteiras em maiúsculo
NAME_REGEX = re.compile(
    r"\b([A-ZÀ-Ü][a-zA-ZÀ-Üà-ü\'\.]+"
    r"(?:\s+(?:de|da|do|dos|das|e|DE|DA|DO|DOS|DAS|E)\s+|\s+)"
    r"[A-ZÀ-Ü][a-zA-ZÀ-Üà-ü\'\.]+(?:\s+[A-ZÀ-Ü][a-zA-ZÀ-Üà-ü\'\.]+){0,2})\b"
)
PREFIX_TRIMMER = re.compile(r"^(ao|a|para|dr\.?|dra\.?|sr\.?|sra\.?|em|na|no|de|do|da|vitima|autor|paciente)\s+", re.IGNORECASE)

# =========================================================
# LÓGICA DE IA (OLLAMA)
# =========================================================
def _text_needs_llm(text: str) -> bool:
    if not text: return False
    text_clean = str(text).strip()
    if len(text_clean) < 12: return False 
    if "{" in text_clean: return False
    # Se for tudo maiúsculo e tiver palavras da Blacklist, nem manda pra IA
    words = text_clean.lower().split()
    if any(w in MEDICAL_LEGAL_BLACKLIST for w in words) and text_clean.isupper():
        return False
    return True

def _ask_local_llm(text: str) -> list:
    # Prompt mais agressivo contra alucinações de campos de formulário
    prompt = f"""Você é um censor de dados privados. Extraia APENAS nomes de seres humanos.
IGNORE categoricamente campos de formulário, perguntas médicas e diagnósticos.

REGRAS:
1. "COR DOS OLHOS", "TIPO DE PELE", "COLETA DNA" NÃO SÃO PESSOAS.
2. Se o texto for um título de seção ou instrução, ignore.
3. Retorne JSON: {{"nomes": []}} se não houver certeza absoluta.

Texto: {text}
"""
    try:
        response = requests.post("http://localhost:11434/api/generate", json={
            "model": "llama3", 
            "prompt": prompt,
            "format": "json", 
            "stream": False,
            "options": {"temperature": 0.0}
        }, timeout=130)
        if response.status_code == 200:
            return json.loads(response.json().get("response", "{}")).get("nomes", [])
        return []
    except:
        return []

# =========================================================
# UTILITÁRIOS E GERAÇÃO DE FAKES
# =========================================================
@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", "", text.upper().strip())

def _is_hallucination(ent_text: str) -> bool:
    if not ent_text or len(str(ent_text)) <= 3: return True
    # Se o nome detectado tiver pontuação ou números, é alucinação
    if any(char.isdigit() for char in ent_text): return True
    
    words = [w.strip(string.punctuation).lower() for w in str(ent_text).split()]
    # Se QUALQUER palavra do nome estiver na Blacklist, descarta (Dinamismo!)
    if any(w in MEDICAL_LEGAL_BLACKLIST for w in words):
        return True
    return False

def _get_fake(value: str, typ: str) -> str:
    global _MAPPING_CACHE
    norm_val = _normalize(value)
    cache_key = f"{typ}:{norm_val}"
    if cache_key in _MAPPING_CACHE: return _MAPPING_CACHE[cache_key]

    seed = int(hashlib.sha256(norm_val.encode()).hexdigest()[:8], 16)
    fake.seed_instance(seed)
    random.seed(seed)

    if typ == "PER":
        first_name = value.split()[0].upper()
        # Gênero simplificado
        if (first_name.endswith('A') or any(first_name.endswith(x) for x in ["EINE", "IANE"])) \
           and first_name not in ["NATAN", "LUCA"]:
            val = f"{fake.first_name_female()} {fake.last_name()}".upper()
        else:
            val = f"{fake.first_name_male()} {fake.last_name()}".upper()
    elif typ == "CPF": val = fake.cpf()
    elif typ == "EMAIL": val = fake.email()
    elif typ == "PLATE": val = fake.license_plate().upper()
    else: val = "".join(random.choices(string.ascii_uppercase + string.digits, k=len(value)))

    _MAPPING_CACHE[cache_key] = val
    return val

# =========================================================
# MOTOR HÍBRIDO
# =========================================================
def _detect_all(text: str, anon_loc: bool):
    found = []
    # 1. Dados Estruturados
    for typ, pat in REGEX.items():
        for match in pat.finditer(text):
            found.append((match.start(), match.end(), match.group(), typ))

    # 2. Regex de Nomes com Limpeza de Prefixo e Blacklist
    for match in NAME_REGEX.finditer(text):
        raw_name = match.group()
        clean_name = PREFIX_TRIMMER.sub("", raw_name).strip()
        if not _is_hallucination(clean_name):
            start_offset = raw_name.find(clean_name)
            found.append((match.start() + start_offset, match.start() + start_offset + len(clean_name), clean_name, "PER"))

    # 3. IA (Ollama)
    if _text_needs_llm(text):
        nomes_ia = _ask_local_llm(text)
        for nome in nomes_ia:
            nome_limpo = PREFIX_TRIMMER.sub("", str(nome)).strip().strip(string.punctuation)
            if not _is_hallucination(nome_limpo):
                padrao = re.compile(rf"\b{re.escape(nome_limpo)}\b", re.IGNORECASE)
                for m in padrao.finditer(text):
                    found.append((m.start(), m.end(), m.group(), "PER"))

    # 4. Consolidação
    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, v, t))
            last = e
    return clean

def anonymize_text(text: str, anon_loc: bool = True) -> str:
    if not isinstance(text, str) or len(text) < 3: return text
    entities = _detect_all(text, anon_loc)
    result, last = [], 0
    for s, e, v, t in entities:
        result.extend([text[last:s], _get_fake(v, t)])
        last = e
    result.append(text[last:])
    return "".join(result)

def anonymize_value(col_name: str, val, anon_location: bool = True):
    if val is None or not isinstance(val, str): return val, None
    new_v = anonymize_text(val, anon_location)
    return new_v, ("TEXT" if new_v != val else None)

def should_anonymize_column(col_name: str, sample_values) -> bool:
    c = col_name.lower()
    return any(k in c for k in ["nome", "vitima", "autor", "cpf", "rg", "placa", "motorista"])

def reset_memory():
    _MAPPING_CACHE.clear()
    _USED_FAKES.clear()

def get_gliner():
    return False