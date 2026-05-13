import re
import random
import string
import unicodedata
import hashlib
import logging
import spacy
from functools import lru_cache
from faker import Faker
from bs4 import BeautifulSoup

# =========================================================
# CONFIGURAÇÕES GLOBAIS E INICIALIZAÇÃO
# =========================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_MAPPING_CACHE = {}
_USED_FAKES = set()
fake = Faker("pt_BR")

try:
    nlp = spacy.load("pt_core_news_lg")
except OSError:
    logger.error("Modelo SpaCy não encontrado! Rode: python -m spacy download pt_core_news_lg")
    nlp = None

# Blacklist expandida com base nos falsos positivos detectados (verbos e termos técnicos)
MEDICAL_LEGAL_BLACKLIST = {
    "exame", "cavidade", "oral", "torax", "abdominal", "cervical", "ombro", 
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
    "estavam", "atendendo", "conforme", "apurado", "irregurales", "energia", "mec",
    "petequias", "esclerais", "frontal", "axila", "coxa", "hematoma", "subcapsular",
    "couro", "cabeludo", "outras", "les", "coluna", "escoria", "rupturas", "multiplas",
    "mesenterio", "fraturas", "cominutivas", "via", "publica", "feridas", "cortocontusas",
    "sinais", "atitude", "pugilista", "policia", "civil", "agente", "oficial", "criminal","pr", "sc", "sp", "rj", "iml", "pm", "prv", "prf", "pcpr", "pmpr",
    "detran", "samu", "siate", "upa", "sus", "crm", "oab", "rg", "cpf"
}

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

# Regex de Nomes: Captura Title Case e ALL CAPS (comum em HTML/Laudos)
# Adicionado suporte para nomes em maiúsculas que terminam com ":" ou espaços extras
NAME_REGEX = re.compile(r"\b(?!(?:TRAUMATISMO|EQUIMOSE|COR|DATA|FOTOS|LAUDO|SEXO)\b)([A-ZÀ-Ü]{3,}(?:\s(?:DE|DA|DO|DOS|DAS)\s|\s)[A-ZÀ-Ü]{2,}(?:\s[A-ZÀ-Ü]{2,})*)\b")
PREFIX_TRIMMER = re.compile(r"^(ao|a|para|dr\.?|dra\.?|sr\.?|sra\.?|em|na|no|de|do|da|vitima|autor|paciente)\s+", re.IGNORECASE)

# =========================================================
# UTILITÁRIOS E FILTROS
# =========================================================
@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", "", text.upper().strip())

def _is_hallucination(ent_text: str) -> bool:
    # 1. Filtro de comprimento mínimo e integridade
    if not ent_text or len(ent_text) < 3: 
        return True
    
    # 2. Bloqueio de Siglas e Abreviações (Ex: P.R., I.M.L., P.M.)
    # Captura letras isoladas com pontos ou siglas de 2-3 letras em maiúsculo
    if re.match(r"^([A-Z]\.){1,3}[A-Z]?$", ent_text.upper()) or \
       (ent_text.isupper() and len(ent_text) <= 3 and ent_text.upper() in ["PR", "PM", "IML", "SAMU", "DR", "DRA"]):
        return True

    # 3. Verificação de "Labels" de formulário e termos estáticos
    # Transformamos em set para busca O(1) e adicionamos novos culpados dos logs
    labels_proibidas = {
        "COR DOS OLHOS", "FOTOS EM ANEXO", "DATA DE", "SEXO MASCULINO", 
        "VINICIUS ALVES:", "OLIVIA LIMA:", "RESPOSTA", "PERGUNTA", "LAUDO"
    }
    if ent_text.upper().strip() in labels_proibidas:
        return True

    clean_text = _normalize(ent_text).lower()
    words = clean_text.split()
    
    # 4. Blacklist Expandida (Baseada nos seus logs recentes)
    # Verificamos se qualquer palavra do termo está na lista de termos proibidos
    if any(w in MEDICAL_LEGAL_BLACKLIST for w in words):
        return True

    # 5. Truque do POS-Tagging (Refinado)
    if nlp:
        # Analisamos o texto em minúsculo para "forçar" o SpaCy a ver o significado real da palavra
        test_doc = nlp(ent_text.lower())
        
        # Se o termo for composto (ex: "EQUIMOSE EXTENSA"), e as palavras forem substantivos/adjetivos
        # comuns em minúsculo, é uma alucinação de nome próprio.
        for token in test_doc:
            # NOUN (Substantivo), ADJ (Adjetivo), VERB (Verbo), ADV (Advérbio)
            if token.pos_ in ["NOUN", "ADJ", "VERB", "ADV"]:
                 # Partículas de nomes próprios brasileiros que devem ser ignoradas no filtro
                 if token.text not in ["de", "da", "do", "dos", "das"]:
                    # Se a palavra em minúsculo tem um significado comum, descartamos como nome
                    return True
                    
    return False

# =========================================================
# MOTOR DE DETECÇÃO HÍBRIDA
# =========================================================
def _detect_all(text: str, anon_loc: bool):
    found = []
    
    # 1. Limpeza de HTML para análise (mas manteremos as posições do texto original)
    soup = BeautifulSoup(text, "html.parser")
    texto_puro = soup.get_text()

    # 2. Regex de Dados Estruturados (CPF, Placas, etc)
    for typ, pat in REGEX.items():
        for match in pat.finditer(text):
            found.append((match.start(), match.end(), match.group(), typ))

    # 3. SpaCy para Nomes Próprios (PER)
    if nlp:
        doc = nlp(text)
        for ent in doc.ents:
            if ent.label_ == "PER":
                nome_limpo = PREFIX_TRIMMER.sub("", ent.text).strip()
                if not _is_hallucination(nome_limpo):
                    found.append((ent.start_char, ent.end_char, ent.text, "PER"))

    # 4. Fallback: Regex de Nomes para capturar o que o SpaCy perde (como nomes em HTML ou ALL CAPS)
    # Procuramos no texto original para não perder os offsets
    for match in NAME_REGEX.finditer(text):
        val = match.group().strip()
        if not _is_hallucination(val):
            # Evita duplicar se o SpaCy já pegou
            if not any(s <= match.start() < e for s, e, v, t in found):
                found.append((match.start(), match.end(), val, "PER"))

    # Ordenar por início e remover sobreposições
    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, v, t))
            last = e
            
    return clean

# =========================================================
# GERAÇÃO E INTERFACE
# =========================================================
def _get_fake(value: str, typ: str) -> str:
    norm_val = _normalize(value)
    cache_key = f"{typ}:{norm_val}"
    
    if cache_key in _MAPPING_CACHE: 
        return _MAPPING_CACHE[cache_key]

    seed = int(hashlib.sha256(norm_val.encode()).hexdigest()[:8], 16)
    fake.seed_instance(seed)
    
    if typ == "PER":
        # Se o original tinha prefixos como "Dr.", mantemos a estrutura mas trocamos o nome
        prefix = ""
        if "DR." in value.upper(): prefix = "DR. "
        elif "DRA." in value.upper(): prefix = "DRA. "
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
    targets = ["nome", "vitima", "autor", "cpf", "rg", "placa", "condutor", "proprietario"]
    return any(k in c for k in targets)

def reset_memory():
    _MAPPING_CACHE.clear()
    _USED_FAKES.clear()

# Função solicitada para compatibilidade de log
def filtrar_nomes_proprios(texto_html):
    entities = _detect_all(texto_html, True)
    return [v for s, e, v, t in entities if t == "PER"]