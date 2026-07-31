import os
import re
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s')
logger = logging.getLogger(__name__)

SECRET_SALT = os.getenv("ANONYMIZER_SECRET_SALT", "SaltSeguroSESP2026_Producao!")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3:latest")

_TELEMETRIA = {
    "celulas_avaliadas": 0,
    "celulas_alteradas": 0,
    "substituicoes_totais": 0,
    "identidades_protegidas": set(),
    "documentos_protegidos": set()
}

def emitir_relatorio_auditoria():
    print("\n" + "="*70)
    print("🛡️  RELATÓRIO DE TELEMETRIA E AUDITORIA (DLP / LGPD) 🛡️")
    print("="*70)
    print(f"📊 Células/Textos avaliados:       {_TELEMETRIA['celulas_avaliadas']}")
    print(f"🔴 Células/Textos alterados:       {_TELEMETRIA['celulas_alteradas']}")
    print(f"🔀 Total de substituições:         {_TELEMETRIA['substituicoes_totais']}")
    print(f"👤 Pessoas/Nomes mascarados:       {len(_TELEMETRIA['identidades_protegidas'])}")
    print(f"📄 Documentos/Dados mascarados:    {len(_TELEMETRIA['documentos_protegidos'])}")
    print("="*70 + "\n")

try:
    import spacy
    nlp = spacy.load("pt_core_news_lg", disable=["lemmatizer"])
except ImportError:
    logger.error("🚨 'spacy' ausente. (pip install spacy)")
    nlp = None
except OSError:
    logger.error("🚨 Modelo ausente. (python -m spacy download pt_core_news_lg)")
    nlp = None

http_session = requests.Session()
_retry_strategy = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
_adapter = HTTPAdapter(max_retries=_retry_strategy)
http_session.mount("http://", _adapter)
http_session.mount("https://", _adapter)

_MAPPING_CACHE: dict = {}
_COLUMN_POLICIES: dict = {} 
_OLLAMA_CACHE: dict = {} 
fake = Faker("pt_BR")

REGEX = {
    "CPF": re.compile(r"(?<!\d)(?:\d[-.\s_/*]{0,4}){10}\d(?!\d)"),
    "IP": re.compile(r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?!\d)"),
    "CEP": re.compile(r"(?<!\d)\d{5}[-\s]?\d{3}(?!\d)"), 
    "DATE_TIME": re.compile(r"(?<!\d)(?:(?:3[01]|[12]\d|0?[1-9])[/.-](?:1[0-2]|0?[1-9])[/.-](?:19|20)?\d\d|(?:19|20)\d\d[/.-](?:1[0-2]|0?[1-9])[/.-](?:3[01]|[12]\d|0?[1-9]))(?:[\s_T]+\d{1,2}:\d{2}(?::\d{2})?)?(?!\d)", re.IGNORECASE),    
    "RG": re.compile(r"(?<!\d)(?:\d[-.\s_/*]{0,4}){4,13}[0-9Xx](?!\d)"), 
    "EMAIL": re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),
    "PLATE": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{3}[-.\s]*[0-9][A-Za-z0-9][0-9]{2}(?![A-Za-z0-9])", re.IGNORECASE),
    "PHONE": re.compile(r"(?<!\d)(?:\+?55[-.\s_]*)?(?:\(?[0]?\d{2}\)?[-.\s_]*)?(?:9[-.\s_]*)?\d{4,5}[-.\s_]*\d{4}(?!\d)"),
    "CHASSI": re.compile(r"(?<![A-Za-z0-9])(?:[A-HJ-NPR-Z0-9][\-\s]*){16}[A-HJ-NPR-Z0-9](?![A-Za-z0-9])", re.IGNORECASE), 
    "COORD": re.compile(r"(?<!\d)-?\d{1,3}[.,]\d{3,}[^A-Za-z0-9]+-?\d{1,3}[.,]\d{3,}(?!\d)"), 
    "COORD_SINGLE": re.compile(r"(?<!\d)-?\d{1,3}[.,]\d{3,}(?!\d)"),
    "GENERIC_CODE": re.compile(r"(?<!\w)(?:[A-Za-z0-9]{1,10}[-/_.]){1,5}[A-Za-z0-9]{1,10}(?!\w)|(?<!\w)[A-Za-z]+\d+[A-Za-z0-9]*(?!\w)")
}

TITLES_TO_STRIP = re.compile(
    r"\b(cabo|soldado|sargento|tenente|capitao|coronel|delegado|investigador|agente|escrivao|"
    r"dr|dra|sr|sra|senhor|senhora|vítima|vitima|suspeito|autor|indivíduo|paciente)\b\.?", 
    re.IGNORECASE
)

TITLE_NAME_REGEX = re.compile(
    r"\b(?:cabo|soldado|sargento|tenente|capitao|coronel|delegado|investigador|agente|escrivao|"
    r"dr|dra|sr|sra|senhor|senhora|vítima|vitima|suspeito|autor|indivíduo|paciente)\b\.?\s+"
    r"([A-ZÀ-Ÿa-zà-ÿ]{2,}(?:\s+(?:de|da|do|dos|das|e)\s+)?(?:[A-ZÀ-Ÿa-zà-ÿ]{2,}\s*){1,4})",
    re.IGNORECASE
)

STOP_WORDS_NAME = re.compile(
    r"\b(portadora|portador|portadores|portadoras|cpf|rg|chassi|placa|email|telefone|veiculo|celular|"
    r"residencia|guarnicao|denuncia|abordagem|local|propriedade|processo|relato|inquisitorial)\b",
    re.IGNORECASE
)

FEMALE_INDICATORS = re.compile(r"\b(dra|sra|senhora|dona|vítima|vitima)\b", re.IGNORECASE)

def _clean_name(name_str: str) -> str:
    cleaned = TITLES_TO_STRIP.sub("", name_str).strip()
    return re.sub(r"^[,.:\-]+|[,.:\-]+$", "", cleaned).strip()

def _ask_llm_yes_no(prompt: str, cache_key: str, system_prompt: str = "") -> bool:
    if cache_key in _OLLAMA_CACHE: return _OLLAMA_CACHE[cache_key]
    try:
        payload = {
            "model": OLLAMA_MODEL, "system": system_prompt, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.0, "top_p": 0.1, "top_k": 1, "num_predict": 5}
        }
        resp = http_session.post(OLLAMA_URL, json=payload, timeout=300)
        if resp.status_code == 200:
            is_yes = "SIM" in resp.json().get("response", "").strip().upper()
            _OLLAMA_CACHE[cache_key] = is_yes
            return is_yes
    except Exception as e:
        logger.warning(f"Falha LLM [{cache_key}]: {e}")
    return False

def _ask_llm_batch(candidates: list) -> list:
    if not candidates: return []
    approved = []
    instrucao_mestra = (
        "Sua ÚNICA função é dizer se um termo é ESTRITAMENTE O NOME PRÓPRIO COMPLETO de uma PESSOA HUMANA REAL. "
        "REGRAS DE REJEIÇÃO ABSOLUTA ('NAO'): "
        "1. Locais, Fazendas, Ruas, Rodovias, Expressões como 'portadora do CPF', 'guarnição de'. "
        "2. Cargos, Posições e Papéis. "
        "3. Dados Demográficos ou nomes incompletos. "
        "Responda ESTRITAMENTE 'SIM' ou 'NAO'."
    )
    for c in set(candidates):
        if len(c.split()) < 2: continue
        if STOP_WORDS_NAME.search(c): continue
        
        key = f"PER:{c.upper()}"
        if key in _OLLAMA_CACHE:
            if _OLLAMA_CACHE[key]: approved.append(c)
            continue
        prompt = f"O termo '{c}' representa UMA PESSOA HUMANA REAL (SIM) ou um TERMO/CARGO/OBJETO (NAO)?\nResposta:"
        if _ask_llm_yes_no(prompt, key, system_prompt=instrucao_mestra): 
            approved.append(c)
    return approved

def _ask_llm_column_classification(amostras: list, tipo_suspeito: str) -> bool:
    amostras_limpas = [str(s).strip() for s in amostras if str(s).strip()][:5]
    amostras_str = " | ".join(amostras_limpas)
    instrucao = "Responda EXCLUSIVAMENTE com 'SIM' ou 'NAO'. Não justifique."
    prompt = f"Analise estas amostras: [{amostras_str}]\nParecem pertencer à categoria '{tipo_suspeito}'?\nResposta:"
    return _ask_llm_yes_no(prompt, f"COL_VOTE_{tipo_suspeito}_{hash(amostras_str)}", system_prompt=instrucao)

class AegisClassifier:
    def __init__(self):
        self.FAST_TRACK_MAP = {
            "CPF": "CPF", "RG": "RG", "CEP": "CEP", "PLATE": "PLACA", "EMAIL": "EMAIL", "PHONE": "PHONE", 
            "CHASSI": "CHASSI", "IP": "IP", "COORD": "COORD", "COORD_SINGLE": "COORD_SINGLE", 
            "TEXTO_LIVRE": "TEXTO_LIVRE", "NOME_SOLTO": "NOME_SOLTO",
            "DATE_TIME": "IGNORAR",
            "GENERIC_CODE": "GENERIC_CODE"
        }
        self.IGNORE_KEYWORDS = {'cidade', 'estado', 'pais', 'bairro', 'status', 'tipo', 'marca', 'cor', 'latitude', 'longitude'}
        self.LLM_TAG_NAMES = {
            "COORD": "Coordenadas Geográficas (GPS)", "CPF": "CPF Brasileiro", "RG": "RG ou Documento Numérico",
            "PLATE": "Placa de Veículo", "PHONE": "Número de Telefone", "DATE_TIME": "Data, Horário ou Ano",
            "GENERIC_CODE": "Código Numérico Genérico ou ID"
        }

    def get_column_tag(self, col_name: str, samples: list) -> str:
        amostras_unicas = list(set(samples))[:50]
        total = len(amostras_unicas)
        if total == 0: return "TEXTO_LIVRE"
        
        placar = Counter()
        col_lower = col_name.lower().strip()

        media_palavras = sum(len(str(s).split()) for s in amostras_unicas) / total
        media_tamanho = sum(len(str(s)) for s in amostras_unicas) / total
        if media_palavras >= 8 or media_tamanho >= 80:
            return "TEXTO_LIVRE"

        total_gps = 0
        for s in amostras_unicas:
            s_str = str(s).strip()
            tamanho_string = max(len(s_str), 1)
            
            match_c = REGEX["COORD"].search(s_str) or REGEX["COORD_SINGLE"].search(s_str)
            if match_c and (len(match_c.group()) / tamanho_string) >= 0.8:
                total_gps += 1
                continue 

            for tag, padrao in REGEX.items():
                if tag in ["COORD", "COORD_SINGLE"]: continue
                match = padrao.search(s_str)
                if match and (len(match.group()) / tamanho_string) >= 0.75:
                    if REGEX["DATE_TIME"].search(s_str):
                        placar["DATE_TIME"] += 1
                    elif tag == "GENERIC_CODE" and REGEX["RG"].search(s_str):
                        placar["RG"] += 1
                    else:
                        placar[tag] += 1

        if (total_gps / total) >= 0.5:
            if not any(REGEX["COORD"].search(str(s)) for s in amostras_unicas): return "COORD_SINGLE"
            return "COORD"

        bonus = total * 0.20 
        if 'cpf' in col_lower: placar["CPF"] += bonus
        if 'rg' in col_lower or 'identidade' in col_lower: placar["RG"] += bonus
        if 'placa' in col_lower: placar["PLATE"] += bonus
        if any(k in col_lower for k in ['data', 'date', 'nascimento', 'hora']): placar["DATE_TIME"] += bonus

        if placar:
            top_candidatos = placar.most_common(2)
            vencedor_principal, pontuacao = top_candidatos[0]
            confianca = pontuacao / total
            
            if confianca >= 0.6: 
                return self.FAST_TRACK_MAP.get(vencedor_principal, vencedor_principal)
            
            logger.warning(f"Dúvida na coluna '{col_name}' ({confianca*100:.1f}%). Acionando IA...")
            for candidato, _ in top_candidatos:
                tipo_humano = self.LLM_TAG_NAMES.get(candidato, candidato)
                if _ask_llm_column_classification(amostras_unicas, tipo_humano):
                    decisao_final = self.FAST_TRACK_MAP.get(candidato, candidato)
                    logger.info(f"✅ IA classificou '{col_name}' como: {decisao_final}")
                    return decisao_final

        palavras_ignorar = self.IGNORE_KEYWORDS.union({'data', 'ano', 'numero', 'hora', 'date', 'time'})
        if any(termo in col_lower for termo in palavras_ignorar): return "IGNORAR"

        tem_pontuacao = any(re.search(r'[,.!?]\s+[A-Z]', str(s)) for s in amostras_unicas)
        if media_palavras >= 3 or tem_pontuacao: return "TEXTO_LIVRE"

        return "IGNORAR"

_aegis_engine = AegisClassifier()

def setup_column_policies(rows: list, target_columns: list):
    if not target_columns or not rows: return
    for col in target_columns:
        if col in _COLUMN_POLICIES: continue
        
        valores_validos = []
        for r in rows:
            val = r.get(col)
            if val is not None:
                val_str = str(val).strip()
                if val_str and val_str.upper() not in ["NÃO CONSTA", "NULL", "NONE", "", "PREJUDICADO"]:
                    valores_validos.append(val_str)
        
        valores_unicos = list(dict.fromkeys(valores_validos))
        if not valores_unicos: continue
            
        amostra_topo = valores_unicos[:50]
        decisao = _aegis_engine.get_column_tag(col, amostra_topo)
        _COLUMN_POLICIES[col] = decisao
        logger.info(f"📊 [PRO] Radar Top-Down classificou '{col}' como: {decisao}")

@lru_cache(maxsize=100000)
def _normalize(text: str) -> str:
    if not text: return ""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).upper().strip()

def _imitar_estrutura_codigo(codigo_real: str, local_rand: random.Random) -> str:
    falso = ""
    for char in codigo_real:
        if char.isdigit(): falso += str(local_rand.randint(0, 9))
        elif char.isalpha(): 
            if char.isupper(): falso += local_rand.choice(string.ascii_uppercase)
            else: falso += local_rand.choice(string.ascii_lowercase)
        else: falso += char 
    return falso

def _get_fake(value: str, typ: str, context_prefix: str = "") -> str:
    clean_value = html.unescape(re.sub(r'<[^>]+>', '', value)).strip()
    norm_val = _normalize(clean_value)
    cache_key = f"{typ}:{norm_val}"
    if cache_key in _MAPPING_CACHE: return _MAPPING_CACHE[cache_key]

    seed_int = int(hmac.new(SECRET_SALT.encode('utf-8'), norm_val.encode('utf-8'), hashlib.sha256).hexdigest()[:16], 16)
    fake.seed_instance(seed_int)
    local_rand = random.Random(seed_int)
    
    if typ in ["COORD", "COORD_SINGLE"]:
        def jitter_match(m):
            try:
                coord_str = m.group().replace(',', '.')
                return f"{float(coord_str) + local_rand.uniform(-0.003, 0.003):.6f}"
            except:
                return m.group()
        val = re.sub(r"-?\d{1,3}[.,]\d{4,}", jitter_match, clean_value)
        _MAPPING_CACHE[cache_key] = val
        return val

    if typ in ["PER", "NOME_SOLTO"]: 
        is_female = bool(FEMALE_INDICATORS.search(context_prefix))
        first = fake.first_name_female() if is_female else fake.first_name()
        val = f"{first} {fake.last_name()}".upper()
        
        ultimo_real = norm_val.split()[-1] if norm_val.split() else ""
        tentativas = 0
        while val.split()[-1] == ultimo_real and tentativas < 10:
            seed_int += 1 
            fake.seed_instance(seed_int)
            first_name = fake.first_name_female() if is_female else fake.first_name()
            val = f"{first_name} {fake.last_name()}".upper()
            tentativas += 1
        _TELEMETRIA["identidades_protegidas"].add(norm_val)
    elif typ == "CPF": val = fake.cpf()
    elif typ in ["RG", "CEP", "GENERIC_CODE"]: val = _imitar_estrutura_codigo(clean_value, local_rand)
    elif typ in ["PLATE", "PLACA"]: val = fake.license_plate().upper()
    elif typ == "EMAIL": val = fake.email().lower()
    elif typ == "PHONE": val = _imitar_estrutura_codigo(clean_value, local_rand)
    elif typ == "IP": val = fake.ipv4()
    elif typ == "CHASSI": val = "".join(local_rand.choices("ABCDEFGHJKLMNPRSTUVWXYZ0123456789", k=17))
    else: val = fake.word().upper()

    _MAPPING_CACHE[cache_key] = val
    return val

def _detect_all(text: str, regras_mascara: dict):
    found = []
    TRUSTED_TAGS = set()
    
    if regras_mascara.get("CPF", True): TRUSTED_TAGS.add("CPF")
    if regras_mascara.get("RG", True): TRUSTED_TAGS.update(["RG", "CEP", "GENERIC_CODE"]) 
    if regras_mascara.get("EMAIL", True): TRUSTED_TAGS.add("EMAIL")
    if regras_mascara.get("IP", True): TRUSTED_TAGS.add("IP")
    if regras_mascara.get("PLATE", True): TRUSTED_TAGS.add("PLATE")
    if regras_mascara.get("CHASSI", True): TRUSTED_TAGS.add("CHASSI")
    if regras_mascara.get("PHONE", True): TRUSTED_TAGS.add("PHONE")
    if regras_mascara.get("COORD", True): TRUSTED_TAGS.update(["COORD", "COORD_SINGLE"])
        
    suspect_names = []
    date_spans = [m.span() for m in REGEX["DATE_TIME"].finditer(text)]

    for typ, pat in REGEX.items():
        if typ == "DATE_TIME": continue
        for match in pat.finditer(text):
            if typ in TRUSTED_TAGS:
                if not any(max(match.start(), ds[0]) < min(match.end(), ds[1]) for ds in date_spans):
                    found.append((match.start(), match.end(), match.group(), typ))

    occupied_spans = [(item[0], item[1]) for item in found]

    for match in TITLE_NAME_REGEX.finditer(text):
        val_extraido = match.group(1).strip()
        val_clean = _clean_name(val_extraido)
        if len(val_clean.split()) >= 2 and not STOP_WORDS_NAME.search(val_clean):
            s, e = match.start(1), match.end(1)
            if not any(max(s, osp[0]) < min(e, osp[1]) for osp in occupied_spans):
                suspect_names.append((s, e, val_clean))

    if regras_mascara.get("NOMES_IA", True) and nlp:
        doc = nlp(text.title()) 
        candidatos_nlp = set()
        
        for ent in doc.ents:
            if ent.label_ == "PER":
                candidatos_nlp.add(ent.text.strip(".,;:?!() \n'\""))

        current_propn = []
        for token in doc:
            if TITLES_TO_STRIP.match(token.text) or STOP_WORDS_NAME.search(token.text):
                if len(current_propn) >= 2:
                    if nlp(current_propn[-1])[0].pos_ == "ADP":
                        current_propn.pop()
                    if len(current_propn) >= 2:
                        candidatos_nlp.add(" ".join(current_propn))
                current_propn = []
                continue

            if token.pos_ == "PROPN":
                current_propn.append(token.text)
            elif token.pos_ == "ADP" and current_propn: 
                current_propn.append(token.text)
            else:
                if len(current_propn) >= 2:
                    if nlp(current_propn[-1])[0].pos_ == "ADP":
                        current_propn.pop()
                    if len(current_propn) >= 2:
                        candidatos_nlp.add(" ".join(current_propn))
                current_propn = []

        for candidato in candidatos_nlp:
            val_clean = _clean_name(candidato)
            if len(val_clean.split()) < 2 or any(c.isdigit() for c in val_clean) or STOP_WORDS_NAME.search(val_clean):
                continue

            for match_original in re.finditer(re.escape(val_clean), text, re.IGNORECASE):
                s, e = match_original.start(), match_original.end()
                
                while s > 0 and text[s-1].isalpha(): s -= 1
                while e < len(text) and text[e].isalpha(): e += 1
                
                if not any(max(s, osp[0]) < min(e, osp[1]) for osp in occupied_spans):
                    val_candidato_limpo = text[s:e].strip()
                    if not STOP_WORDS_NAME.search(val_candidato_limpo):
                        suspect_names.append((s, e, val_candidato_limpo))

        if suspect_names:
            unique_names = list(set([item[2] for item in suspect_names if not STOP_WORDS_NAME.search(item[2])]))
            approved_names = _ask_llm_batch(unique_names)
            
            for s, e, v in suspect_names:
                if any(v.lower() == app_name.lower() for app_name in approved_names):
                    found.append((s, e, v, "PER"))

    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    
    clean, last = [], -1
    for s, e, v, t in found:
        if s >= last:
            clean.append((s, e, text[s:e], t))
            last = e
            
    return clean

def anonymize_value(col_name: str, val, regras_mascara=None):
    try:
        if val is None or not str(val).strip(): return val, None
        text = str(val).strip()
        if isinstance(regras_mascara, bool): regras_mascara = {"COORD": regras_mascara, "COORD_SINGLE": regras_mascara}
        elif regras_mascara is None: regras_mascara = {}
            
        global _TELEMETRIA
        _TELEMETRIA["celulas_avaliadas"] += 1
        politica_execucao = _COLUMN_POLICIES.get(col_name, "TEXTO_LIVRE")
        
        if politica_execucao == "IGNORAR": return text, None
        if politica_execucao in ["COORD", "COORD_SINGLE"] and not regras_mascara.get("COORD", True): return text, None
            
        if politica_execucao in ["NOME_SOLTO", "COORD", "COORD_SINGLE", "PLACA", "CPF", "RG", "CEP", "EMAIL", "PLATE", "PHONE", "IP", "CHASSI", "GENERIC_CODE"]:
            chave_regra = "PLATE" if politica_execucao in ["PLACA", "PLATE"] else ("RG" if politica_execucao in ["CEP", "GENERIC_CODE"] else politica_execucao)
            if chave_regra not in ["COORD", "COORD_SINGLE"] and not regras_mascara.get(chave_regra, True): return text, None
                
            fake_val = _get_fake(text, politica_execucao)
            if fake_val != text:
                _TELEMETRIA["celulas_alteradas"] += 1
                _TELEMETRIA["substituicoes_totais"] += 1
            return fake_val, ("TEXT" if fake_val != text else None)
            
        if politica_execucao == "TEXTO_LIVRE":
            entities = _detect_all(text, regras_mascara)
            if not entities: return text, None
            
            result, last = [], 0
            
            for s, e, v, t in entities:
                if s < last: continue
                contexto_anterior = text[max(0, s-20):s]
                fake_val = _get_fake(v, t, context_prefix=contexto_anterior)
                
                _TELEMETRIA["substituicoes_totais"] += 1
                result.extend([text[last:s], fake_val])
                last = e
                
            result.append(text[last:])
            texto_final = "".join(result)
            
            if texto_final != text: _TELEMETRIA["celulas_alteradas"] += 1
            return texto_final, ("TEXT" if texto_final != text else None)

        return text, None
    except Exception as e:
        logger.error(f"Erro máscara '{col_name}': {e}")
        return str(val), None

def reset_memory():
    _MAPPING_CACHE.clear()
    _COLUMN_POLICIES.clear()
    _OLLAMA_CACHE.clear()

def process_chunk_parallel(rows, modo, regras_mascara, target_columns):
    if modo != "🛡️ Anonimização Total" or not rows: return rows
    if regras_mascara is None: regras_mascara = {}
    
    col_alvo = [c for c in rows[0].keys() if c in target_columns]
    if col_alvo: setup_column_policies(rows, col_alvo)
    
    html_regex = re.compile(r"<[^>]+>")
    processed = []
    
    for r in rows:
        row_dict = dict(r)
        for col, old in row_dict.items():
            if not target_columns or col not in target_columns: continue
            if old is None or type(old).__name__ in ['date', 'datetime', 'Timestamp', 'bool']: continue

            try:
                vault = {}
                safe_text = html.unescape(str(old).strip())
                if "<" in safe_text:
                    def hide(m):
                        tk = f" __SHLD{len(vault)}__ "
                        vault[tk.strip()] = m.group(0)
                        return tk
                    safe_text = html_regex.sub(hide, safe_text)
                
                final_text, _ = anonymize_value(col, safe_text, regras_mascara=regras_mascara)
                final_text = str(final_text)

                for tk, orig in vault.items():
                    final_text = final_text.replace(f" {tk} ", orig).replace(tk, orig)
                row_dict[col] = final_text
            except Exception as e:
                logger.error(f"⚠️ Erro coluna '{col}'.")
                row_dict[col] = str(old)
        processed.append(row_dict)
    return processed 

def process_raw_text(text: str, regras_mascara=None) -> str:
    if not text or not str(text).strip(): return text
    if isinstance(regras_mascara, bool): regras_mascara = {"COORD": regras_mascara, "COORD_SINGLE": regras_mascara}
    elif regras_mascara is None: regras_mascara = {}
        
    safe_text = str(text)
    vault = {}
    if "<" in safe_text:
        def hide(m):
            tk = f" __SHLD{len(vault)}__ "
            vault[tk.strip()] = m.group(0)
            return tk
        safe_text = re.compile(r"<[^>]+>").sub(hide, safe_text)
        
    _COLUMN_POLICIES["RAW_TEXT_INJECTION"] = "TEXTO_LIVRE"
    final_text, _ = anonymize_value("RAW_TEXT_INJECTION", safe_text, regras_mascara=regras_mascara)
    final_text = str(final_text)
    
    for tk, orig in vault.items():
        final_text = final_text.replace(f" {tk} ", orig).replace(tk, orig)
    return final_text