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
# Mantenha INFO para um terminal limpo. Mude para DEBUG se quiser ver a IA pensando.
logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
logger = logging.getLogger(__name__)

# =========================================================
# CONFIGURAÇÕES GLOBAIS E BLACKLIST
# =========================================================
_MAPPING_CACHE = {}
_USED_FAKES = set()
fake = Faker("pt_BR")

CACHE_LIMIT = 500000

# Blacklist expandida para blindar a IA contra jargões policiais e médicos
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
    # 🔥 NOVAS ADIÇÕES (Bloqueia Características e Formulários):
    "cor", "olho", "olhos", "cabelo", "cabelos", "pele", "cutis", "idade", "sexo", 
    "profissao", "naturalidade", "nacionalidade", "estado", "civil", "endereco", 
    "telefone", "celular", "email", "data", "nascimento", "trabalho", "pai", "mae",
    "marca", "modelo", "placa", "chassi", "renavam", "ano", "fabricacao",
    "altura", "peso", "tatuagem", "cicatriz", "vestes", "trajes", "fisico", "compleicao"
}
# =========================================================
# REGEX DE DADOS ESTRUTURADOS E NOMES
# =========================================================
REGEX = {
    "CPF": re.compile(r"\b(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(\d{2}\)|\d{2})\s?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "RG": re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}-?[0-9X]\b"),
    "COORD": re.compile(r"^\s*-?(?:90(?:\.0+)?|[0-8]?\d(?:\.\d+)?)\s*,\s*-?(?:180(?:\.0+)?|1[0-7]\d(?:\.\d+)?|\d{1,2}(?:\.\d+)?)\s*$"),
}

# Regex turbinada para pegar nomes formatados (Title Case e ALL CAPS) rapidamente
NAME_REGEX = re.compile(
    r"\b([A-ZÀ-Ü][a-zA-ZÀ-Üà-ü\'\.]+"
    r"(?:\s+(?:de|da|do|dos|das|e|DE|DA|DO|DOS|DAS|E)\s+|\s+)"
    r"[A-ZÀ-Ü][a-zA-ZÀ-Üà-ü\'\.]+(?:\s+[A-ZÀ-Ü][a-zA-ZÀ-Üà-ü\'\.]+){0,3})\b"
)
PREFIX_TRIMMER = re.compile(r"^(ao|a|para|dr\.?|dra\.?|sr\.?|sra\.?|em|na|no|de|do|da|vitima|autor|paciente)\s+", re.IGNORECASE)

# =========================================================
# IA: OLLAMA E FILTROS DE SEGURANÇA
# =========================================================
def _text_needs_llm(text: str) -> bool:
    """O Porteiro de Ferro: Bloqueia lixo e textos curtos antes de chamar a IA."""
    if not text: return False
    text_clean = str(text).strip()
    
    # 1. BARREIRA DE TAMANHO: Impede que a IA alucine com "Praticada" ou "Maria"
    if len(text_clean) < 20:
        return False
        
    # 2. BARREIRA DE VARIÁVEIS: Impede que a IA leia "{ENVOLVIDO.NOME}"
    if "{" in text_clean or "}" in text_clean:
        return False
        
    # 3. BARREIRA DE CABEÇALHOS: Impede que a IA leia "EXAMES COMPLEMENTARES"
    if text_clean.isupper() and len(text_clean.split()) <= 4:
        return False

    return True

def _ask_local_llm(text: str) -> list:
    """Consulta o Ollama com foco em extração pura, aceitando nomes minúsculos."""
    prompt = f"""Você é um perito em LGPD. Extraia APENAS nomes próprios completos de PESSOAS do texto.

REGRAS:
1. IGNORE hospitais, ruas, cidades, exames, viaturas, objetos ou jargões policiais.
2. Pode extrair nomes mesmo se estiverem em letras minúsculas.
3. Extraia de forma literal (não corrija acentos e não mude maiúsculas/minúsculas).
4. Retorne APENAS um JSON: {{"nomes": ["NOME 1", "NOME 2"]}}
5. Se não houver nome de ser humano, retorne {{"nomes": []}}

Texto: {text}
"""
    try:
        response = requests.post("http://localhost:11434/api/generate", json={
            "model": "llama3", 
            "prompt": prompt,
            "format": "json", 
            "stream": False,
            "options": {"temperature": 0.0, "top_p": 0.9}
        }, timeout=120) # Timeout em 120s para textos longos não travarem
        
        if response.status_code == 200:
            return json.loads(response.json().get("response", "{}")).get("nomes", [])
        return []
    except Exception as e:
        logger.error(f"Erro na IA: {e}")
        return []

# =========================================================
# UTILITÁRIOS E GERAÇÃO DE FAKES DETERMINÍSTICOS
# =========================================================
@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", "", text.upper().strip())

def _is_hallucination(ent_text: str) -> bool:
    """Verifica se é lixo. Aceita nomes minúsculos, mas bloqueia palavras muito curtas."""
    if not ent_text or len(str(ent_text)) <= 2:
        return True
    words = [w.strip(string.punctuation).lower() for w in str(ent_text).split()]
    if any(w in MEDICAL_LEGAL_BLACKLIST for w in words):
        return True
    return False

def _get_fake(value: str, typ: str) -> str:
    global _MAPPING_CACHE
    
    # Previne estouro de memória deletando o item mais antigo (FIFO)
    if len(_MAPPING_CACHE) >= CACHE_LIMIT:
        del _MAPPING_CACHE[next(iter(_MAPPING_CACHE))]
        
    norm_val = _normalize(value)
    cache_key = f"{typ}:{norm_val}"
    
    if cache_key in _MAPPING_CACHE: 
        return _MAPPING_CACHE[cache_key]

    # 🔥 Mágica da Consistência: O hash do nome real gera a semente do nome falso
    seed = int(hashlib.sha256(norm_val.encode()).hexdigest()[:8], 16)
    fake.seed_instance(seed)
    random.seed(seed)

    # Força a geração apenas de Nome + Sobrenome em maiúsculo
    if typ == "PER": val = f"{fake.first_name()} {fake.last_name()}".upper()
    elif typ == "CPF": val = fake.cpf()
    elif typ == "EMAIL": val = fake.email()
    elif typ == "PLATE": val = fake.license_plate().upper()
    elif typ == "COORD":
        try:
            lat, lon = map(float, value.split(","))
            val = f"{lat + random.uniform(-0.003, 0.003):.4f}, {lon + random.uniform(-0.003, 0.003):.4f}"
        except:
            val = value
    else:
        val = "".join(random.choices(string.ascii_uppercase + string.digits, k=len(value)))

    _MAPPING_CACHE[cache_key] = val
    return val

# =========================================================
# MOTOR DE DETECÇÃO HÍBRIDA (O FUNIL PERFEITO)
# =========================================================
def _detect_all(text: str, anon_loc: bool):
    found = []

    # 1. Regex Estruturada (Rodagem rápida para CPF, RG, etc)
    for match in NAME_REGEX.finditer(text):
        raw_name = match.group()
        # 🔥 Remove o "Ao " ou "Para " que a Regex capturou por engano
        clean_name = PREFIX_TRIMMER.sub("", raw_name).strip()
        
        if not _is_hallucination(clean_name):
            # Recalcula os índices para substituir APENAS o nome e manter o "Ao " intacto no texto
            start_offset = raw_name.find(clean_name)
            if start_offset != -1:
                real_start = match.start() + start_offset
                real_end = real_start + len(clean_name)
                found.append((real_start, real_end, clean_name, "PER"))

    # 3. Inteligência Artificial (Apenas para narrativas complexas e nomes minúsculos)
    if _text_needs_llm(text):
        nomes_ia = _ask_local_llm(text)
        for nome in nomes_ia:
            # Limpeza extrema de pontuações que a IA pode ter inventado
            nome_limpo = PREFIX_TRIMMER.sub("", str(nome)).strip()
            nome_limpo = nome_limpo.strip(string.punctuation)
            
            if _is_hallucination(nome_limpo): 
                continue
            
            nome_norm = _normalize(nome_limpo)
            if not nome_norm: continue
            
            # Procura o nome sugerido pela IA dentro do texto original (Match exato)
            padrao = re.compile(rf"\b{re.escape(nome_limpo)}\b", re.IGNORECASE)
            for match in padrao.finditer(text):
                found.append((match.start(), match.end(), match.group(), "PER"))

    # 4. Remove Sobreposições (Prioriza a detecção maior)
    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, v, t))
            last = e
    return clean

# =========================================================
# FUNÇÕES DE INTERFACE COM O APP.PY
# =========================================================
def anonymize_text(text: str, anon_loc: bool = True) -> str:
    if not isinstance(text, str) or len(text) < 3: return text
    entities = _detect_all(text, anon_loc)
    if not entities: return text

    result, last = [], 0
    for s, e, v, t in entities:
        result.extend([text[last:s], _get_fake(v, t)])
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
    if any(k in c for k in ["nome", "vitima", "autor", "cpf", "rg", "placa"]):
        return True
    return False

def reset_memory():
    _MAPPING_CACHE.clear()
    _USED_FAKES.clear()

# =========================================================
# COMPATIBILIDADE GLINER (STUB)
# =========================================================
def get_gliner():
    """Mantido apenas para evitar erro de importação no app principal."""
    return False