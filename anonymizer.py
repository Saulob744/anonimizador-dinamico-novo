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
from collections import Counter
from functools import lru_cache
from faker import Faker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s')
logger = logging.getLogger(__name__)

SECRET_SALT = os.getenv("ANONYMIZER_SECRET_SALT", "SaltDeEmergenciaApenasParaDesenvolvimento123!")

try:
    import spacy
    nlp = spacy.load("pt_core_news_lg", disable=["parser", "attribute_ruler", "lemmatizer", "tok2vec"])
except OSError:
    logger.warning("spaCy não encontrado.")
    nlp = None

# =========================================================
# OTIMIZAÇÃO 1: TÚNEL HTTP PERSISTENTE
# =========================================================
http_session = requests.Session()

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
        payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0, "num_predict": 5}}
        resp = http_session.post(OLLAMA_URL, json=payload, timeout=15)
        if resp.status_code == 200:
            is_yes = "SIM" in resp.json().get("response", "").strip().upper()
            _OLLAMA_CACHE[cache_key] = is_yes
            return is_yes
    except Exception:
        pass
    return False

def _ask_llm_batch(candidates: list) -> list:
    if not candidates: return []
    unknowns, approved = [], []
    
    for c in set(candidates):
        key = f"PER:{c.upper()}"
        if key in _OLLAMA_CACHE:
            if _OLLAMA_CACHE[key]: approved.append(c)
        else:
            unknowns.append(c)
            
    if not unknowns: return approved
    
    prompt = (
        f"Retorne APENAS os nomes próprios de PESSOAS HUMANAS presentes na lista.\n"
        f"Descarte empresas, ruas, bairros, cidades ou jargões operacionais.\n"
        f"Lista: {unknowns}\n"
        f"Responda no formato JSON estrito: {{\"nomes_reais\": [\"Nome\"]}}"
    )
    
    try:
        payload = {"model": OLLAMA_MODEL, "prompt": prompt, "format": "json", "stream": False, "options": {"temperature": 0.0}}
        resp = http_session.post(OLLAMA_URL, json=payload, timeout=20)
        if resp.status_code == 200:
            data = json.loads(resp.json().get("response", ""))
            reais = [str(n).upper() for n in data.get("nomes_reais", [])]
            for c in unknowns:
                valido = any(c.upper() in n or n in c.upper() for n in reais)
                _OLLAMA_CACHE[f"PER:{c.upper()}"] = valido
                if valido: approved.append(c)
    except Exception:
        for c in unknowns: _OLLAMA_CACHE[f"PER:{c.upper()}"] = False
        
    return approved

class AegisClassifier:
    def __init__(self):
        self.FAST_TRACK_MAP = {
            "CPF": "CPF", "RG": "RG", "DOC_GENERICO": "DOC_GENERICO",
            "CARTAO_CREDITO": "CARTAO_CREDITO", "PLATE": "PLACA", 
            "EMAIL": "EMAIL", "PHONE": "PHONE", "CHASSI": "CHASSI",
            "IP": "IP", "GENERIC_CODE": "GENERIC_CODE",
            "COORD": "COORD", "COORD_SINGLE": "COORD_SINGLE",
            "DATE_TIME": "IGNORAR",
            "TEXTO_LIVRE": "TEXTO_LIVRE",
            "NOME_SOLTO": "NOME_SOLTO"
        }

    def get_column_tag(self, col_name: str, samples: list) -> str:
        amostras_limpas = [str(s).strip() for s in samples if s is not None and str(s).strip()]
        if not amostras_limpas: return "IGNORAR"
        amostras_unicas = list(set(amostras_limpas))[:20]
        total = len(amostras_unicas)

        placar = Counter()
        col_lower = col_name.lower().strip()

        peso_nome = total * 0.4 
        if 'cpf' in col_lower: placar["CPF"] += peso_nome
        if 'rg' in col_lower or 'identidade' in col_lower: placar["RG"] += peso_nome
        if 'email' in col_lower: placar["EMAIL"] += peso_nome
        if 'placa' in col_lower: placar["PLATE"] += peso_nome
        if any(x in col_lower for x in ['lat', 'lon', 'coord']): placar["COORD_SINGLE"] += peso_nome
        
        for s in amostras_unicas:
            tamanho_string = max(len(s), 1)
            for tag, padrao in REGEX.items():
                match = padrao.search(s)
               
                if match and (len(match.group()) / tamanho_string) >= 0.8:
                    placar[tag] += 1

        media_palavras = sum(len(s.split()) for s in amostras_unicas) / total
        tem_pontuacao_frase = any(re.search(r'[,.!?]\s+[A-Z]', s) for s in amostras_unicas)
        
        if media_palavras >= 3 or tem_pontuacao_frase:
            placar["TEXTO_LIVRE"] += total * 0.8 

        if nlp:
            per_count = sum(1 for s in amostras_unicas if any(ent.label_ == "PER" for ent in nlp(s.title()).ents))
            placar["NOME_SOLTO"] += per_count 

        # ========================================================
        # O JULGAMENTO DA BALANÇA
        # ========================================================
        if placar:
            vencedor, pontuacao = placar.most_common(1)[0]
            confianca = pontuacao / total

            if confianca >= 0.7:
                tag_final = self.FAST_TRACK_MAP.get(vencedor, vencedor)
                logger.debug(f"⚖️ Balança aprovou '{col_name}' como {tag_final} (Confiança: {confianca*100:.1f}%)")
                return tag_final
            
            elif confianca >= 0.4:
                amostra_str = " | ".join(amostras_unicas[:5])
                prompt = f"A amostra '{amostra_str}' da coluna '{col_name}' é do tipo '{vencedor}' ou contém nomes de pessoas? Responda APENAS 'SIM' ou 'NAO'."
                logger.debug(f"⚖️ Balança indecisa para '{col_name}'. Chamando IA...")
                if _ask_llm_yes_no(prompt, f"COL_CONFIRM_{col_name}_{vencedor}"):
                    return self.FAST_TRACK_MAP.get(vencedor, vencedor)

        if any(termo in col_lower for termo in {'cidade', 'estado', 'pais', 'bairro', 'cep', 'status', 'tipo', 'marca', 'cor', 'data', 'hora'}):
            return "IGNORAR"
            
        prompt_fallback = f"A amostra '{' | '.join(amostras_unicas[:5])}' da coluna '{col_name}' possui dados pessoais identificáveis ou texto sensível? Responda APENAS 'SIM' ou 'NAO'."
        if _ask_llm_yes_no(prompt_fallback, f"COL_FALLBACK_{col_name}"): 
            return "TEXTO_LIVRE"

        return "IGNORAR"


_aegis_engine = AegisClassifier()


def setup_column_policies(rows: list, target_columns: list):
   
    if not target_columns: return

    for col in target_columns:
        if col in _COLUMN_POLICIES:
            continue
        
        samples = []
        for r in rows:
            val = r.get(col)
            if val is not None and str(val).strip():
                samples.append(str(val).strip())
                if len(samples) >= 20: break
                
        if samples:
            decisao = _aegis_engine.get_column_tag(col, samples)
            _COLUMN_POLICIES[col] = decisao
            logger.info(f"📊 Coluna '{col}' classificada como: {decisao}")

@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper().strip()

def _detect_all(text: str, anon_loc: bool):
    found = []
    
    TRUSTED_TAGS = {"CPF", "RG", "EMAIL", "IP", "PLATE", "CHASSI", "PHONE", "COORD", "COORD_SINGLE", "DOC_GENERICO", "GENERIC_CODE"}
    
    INVALID_WORDS = re.compile(
        r"\b(rua|avenida|av|travessa|trav|praça|praca|alameda|rodovia|rod|bairro|lote|lt|quadra|qd|condominio|cond|edificio|ed|bloco|apartamento|apto|casa|km|br|centro|jardim|jd|parque|pq|vila|"
        r"pgto|pagamento|pago|efetuado|transferencia|transf|pix|banco|agencia|conta|boleto|valor|saldo|debito|credito|tarifa|taxa|estorno|cancelado|aprovado|rejeitado|compra|venda|op|"
        r"sistema|relatorio|projeto|arquivo|anexo|dados|info|usuario|senha|login|id|codigo|protocolo|veiculo|carro|moto|placa|chassi|renavam)\b", 
        re.IGNORECASE
    )
    
    suspect_names = []

    for typ, pat in REGEX.items():
        if typ == "DATE_TIME" or (not anon_loc and typ in ["COORD", "COORD_SINGLE"]): continue
        for match in pat.finditer(text):
            val = match.group()
            if typ == "GENERIC_CODE" and (not any(c.isdigit() for c in val) or len(val) < 6): continue
            
            if typ in TRUSTED_TAGS:
                found.append((match.start(), match.end(), val, typ))

    for match in CONTEXT_NAME_REGEX.finditer(text):
        val = match.group(2).strip()
        if len(val) >= 3 and not INVALID_WORDS.search(val):
            suspect_names.append((match.start(2), match.end(2), val))

    for match in NAME_FALLBACK_REGEX.finditer(text):
        val = match.group().strip()
        if len(val) >= 3 and not INVALID_WORDS.search(val):
            suspect_names.append((match.start(), match.end(), val))

    if nlp:
        doc = nlp(text.title() if text.isupper() or text.islower() else text)
        for ent in doc.ents:
            if ent.label_ == "PER":
                val_clean = ent.text.strip(".,;:?!() \n'\"")
                if len(val_clean) >= 3 and not any(char.isdigit() for char in val_clean) and not INVALID_WORDS.search(val_clean): 
                    start = text.find(val_clean, max(0, ent.start_char - 2))
                    if start != -1: suspect_names.append((start, start + len(val_clean), val_clean))

    if suspect_names:
        unique_names = list(set([item[2] for item in suspect_names]))
        approved_names = _ask_llm_batch(unique_names)
        for s, e, v in suspect_names:
            if v in approved_names: found.append((s, e, v, "PER"))

    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, text[s:e], t))
            last = e
    return clean

def apply_gps_jitter(coord_str):
    try: return f"{float(coord_str.replace(',', '.')) + random.uniform(-0.003, 0.003):.6f}"
    except ValueError: return coord_str

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
    elif typ == "DOC_GENERICO": val = "".join([str(local_rand.randint(0, 9)) if c.isdigit() else c for c in clean_value])
    elif typ == "GENERIC_CODE": val = "".join([str(local_rand.randint(0, 9)) if c.isdigit() else (local_rand.choice(string.ascii_uppercase) if c.isalpha() else c) for c in clean_value])
    else: val = fake.word().upper()

    _MAPPING_CACHE[cache_key] = val
    return val

def anonymize_value(col_name: str, val, anon_location: bool = True):
    try:
        if val is None or not str(val).strip(): return val, None
        text = str(val).strip()
        
        politica_execucao = _COLUMN_POLICIES.get(col_name, "TEXTO_LIVRE")

        if politica_execucao == "IGNORAR": return text, None
            
        if politica_execucao in ["NOME_SOLTO", "COORD", "COORD_SINGLE", "PLACA", "CPF", "RG", "EMAIL", "PLATE", "PHONE", "IP", "CHASSI", "GENERIC_CODE", "DOC_GENERICO"]:
            fake_val = _get_fake(text, politica_execucao)
            if fake_val != text:
                logger.info(f"🕵️ [TROCA DIRETA | {col_name}] '{text}' ➡️ '{fake_val}'")
            return fake_val, ("TEXT" if fake_val != text else None)
            
        if politica_execucao == "TEXTO_LIVRE":
            entities = _detect_all(text, anon_location)
            if not entities: return text, None
            
            result, last = [], 0
            for s, e, v, t in entities:
                if s < last: continue
                
                fake_val = _get_fake(v, t)
                logger.info(f"🕵️ [TROCA IA | {col_name}] '{v}' ➡️ '{fake_val}'")
                
                result.append(text[last:s])
                result.append(fake_val)
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
