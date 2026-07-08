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

# ==============================================================================
# CONFIGURAÇÕES E VARIÁVEIS DE AMBIENTE
# ==============================================================================
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

# ==============================================================================
# INICIALIZAÇÃO DO MOTOR DE NLP 
# ==============================================================================
try:
    import spacy
    nlp = spacy.load("pt_core_news_lg", disable=["lemmatizer"])
except ImportError:
    logger.error("🚨 Biblioteca 'spacy' ausente. (Execute: pip install spacy)")
    nlp = None
except OSError:
    logger.error("🚨 Modelo ausente. (Execute: python -m spacy download pt_core_news_lg)")
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

# ==============================================================================
# EXPRESSÕES REGULARES DO MOTOR
# ==============================================================================
REGEX = {
    "CPF": re.compile(r"(?<!\d)(?:\d[-.\s_/*]{0,4}){10}\d(?!\d)"),
    "IP": re.compile(r"(?<!\d)(?:\d{1,3}[_.\-\s]+){3}\d{1,3}(?!\d)"),
    "CEP": re.compile(r"(?<!\d)\d{5}[-\s]?\d{3}(?!\d)"), 
    "RG": re.compile(r"(?<!\d)(?:\d[-.\s_/*]{0,4}){4,13}[0-9Xx](?!\d)"), 
    "EMAIL": re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),
    "PLATE": re.compile(r"(?<![A-Za-z])(?:[A-Za-z][-.\s_]*){3}\d[-.\s_]*[A-Za-z0-9][-.\s_]*\d[-.\s_]*\d(?![A-Za-z0-9])"),
    "PHONE": re.compile(r"(?<!\d)(?:\+?55[-.\s_]*)?(?:\(?[0]?\d{2}\)?[-.\s_]*)?(?:9[-.\s_]*)?\d{4,5}[-.\s_]*\d{4}(?!\d)"),
    "CHASSI": re.compile(r"(?<![A-Za-z0-9])(?:[A-HJ-NPR-Z0-9][\-\s]*){16}[A-HJ-NPR-Z0-9](?![A-Za-z0-9])", re.IGNORECASE), 
    "DATE_TIME": re.compile(r"(?<!\d)\d{1,4}[/.\-\\]\d{1,2}[/.\-\\]\d{2,4}(?:[-.\s_]+\d{1,2}[:h]\d{1,2}(?::\d{1,2})?)?(?!\d)", re.IGNORECASE),    
    "COORD": re.compile(r"(?<!\d)-?\d{1,3}[.,]\d{3,}[^A-Za-z0-9]+-?\d{1,3}[.,]\d{3,}(?!\d)"), 
    "COORD_SINGLE": re.compile(r"(?<!\d)-?\d{1,3}[.,]\d{3,}(?!\d)"),
    "GENERIC_CODE": re.compile(r"(?<!\w)(?:[A-Za-z]{1,4}[-.\s_]+)?\d{5,20}(?:[-.\s_/]+[A-Za-z0-9]+)*(?!\w)")
}

VIP_CONTEXT_REGEX = re.compile(r"\b(corpo de|cadáver de|cadaver de)\s*[:\-]?\s+([A-ZÀ-Ÿa-zà-ÿ]{2,20}(?:\s+[A-ZÀ-Ÿa-zà-ÿ]{2,20}){1,5})\b", re.IGNORECASE)

SUSPECT_CONTEXT_REGEX = re.compile(r"\b(nome|cliente|paciente|sr|sra|dr|dra|vítima|vitima|autor|perito|legista|motorista|condutor|testemunha|delegado|investigador|agente|suspeito)\b(?:.*?[:\-.)])?\s*([A-ZÀ-Ÿa-zà-ÿ]{2,20}(?:\s+[A-ZÀ-Ÿa-zà-ÿ]{2,20}){1,5})\b", re.IGNORECASE)

NAME_FALLBACK_REGEX = re.compile(r"\b[A-ZÀ-Ÿ]{2,}(?:\s+(?:DE|DA|DO|DOS|DAS|E|DI)?\s*[A-ZÀ-Ÿ]{2,}){1,5}\b")

INVALID_WORDS = re.compile(
    r"\b(rua|avenida|av|travessa|trav|praca|alameda|rodovia|bairro|lote|quadra|condominio|edificio|bloco|apartamento|km|br|centro|jardim|parque|vila|"
    r"sistema|relatorio|projeto|arquivo|anexo|dados|usuario|senha|protocolo|veiculo|carro|moto|motocicleta|caminhao|bicicleta|placa|chassi|renavam|"
    r"fatal|como|sendo|passageira|passageiro|capotou|capoti|data|joaquim|tavora|carlopolis|irati|exames|historico|raca|cor|estatura|talhe|conclusao|"
    r"fratura|fechada|aberta|sangue|escoria|escoriacao|parede|espessada|massa|encefalica|dente|arcada|muscular|subjacente|lateral|terco|pleura|aparente|rosto|face|externa|extensa|"
    r"acidente|transito|hospital|lesao|ferimento|obito|entrada|boletim|ocorrencia|equipe|saude|via|publica|evento|automotor|automovel|passeio|energia|impacto|colisao|autoxauto|atropelamento|"
    r"homem|mulher|adulto|adulta|crianca|idoso|indigente|desconhecido|individuo|sexo|feminino|masculino|menino|menina|ignorado|ignorada|natimorto|feto|"
    r"condutor|motorista|vitima|autor|paciente|perito|legista|medico|policial|socorrista|agente|delegado|investigador|cadaver|corpo|falecido|falecida|suspeito|testemunha)\b",
    re.IGNORECASE
)

# ==============================================================================
# UTILITÁRIOS E CONEXÃO COM IA
# ==============================================================================
def _farejador_sintatico(nome_sujo: str) -> str:
    if not nlp: return nome_sujo.strip()
    doc = nlp(nome_sujo)
    tokens_limpos = []
    
    for token in doc:
        if not tokens_limpos and token.pos_ in ["ADP", "DET"]:
            continue
        if token.pos_ in ["VERB", "AUX", "NUM", "PUNCT", "SYM"]:
            break
        tokens_limpos.append(token.text_with_ws)
        
    return "".join(tokens_limpos).strip()

def _ask_llm_yes_no(prompt: str, cache_key: str, system_prompt: str = "") -> bool:
    if cache_key in _OLLAMA_CACHE: return _OLLAMA_CACHE[cache_key]
    try:
        payload = {
            "model": OLLAMA_MODEL,
            "system": system_prompt, 
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.0,  
                "top_p": 0.1,        
                "top_k": 1,          
                "num_predict": 5     
            }
        }
        resp = http_session.post(OLLAMA_URL, json=payload, timeout=300)
        if resp.status_code == 200:
            is_yes = "SIM" in resp.json().get("response", "").strip().upper()
            _OLLAMA_CACHE[cache_key] = is_yes
            return is_yes
    except Exception as e:
        logger.warning(f"Falha de conexão com a IA no termo '{cache_key}': {e}")
    return False

def _ask_llm_batch(candidates: list) -> list:
    if not candidates: return []
    approved = []
    
    instrucao_mestra = (
        "Você é um algoritmo de Prevenção de Perda de Dados (DLP) de grau militar operando em laudos periciais da Polícia Científica. "
        "Sua ÚNICA função é dizer se um termo suspeito é ESTRITAMENTE O NOME PRÓPRIO COMPLETO de uma PESSOA HUMANA REAL. "
        "REGRAS DE REJEIÇÃO ABSOLUTA (Você DEVE responder 'NAO' para): "
        "1. Cargos, Posições e Papéis (ex: condutor, motorista, vítima, autor, paciente, perito). "
        "2. Dados Demográficos (ex: indivíduo, masculino, feminino, cadáver, corpo). "
        "3. Locais, Cidades e Municípios, ESPECIALMENTE os que levam nomes de santos (ex: São José dos Pinhais, Santa Catarina, Santo Antônio, São Paulo). "
        "4. Ações ou Jargões Médicos (ex: colisão frontal, fratura, atropelamento). "
        "Responda ESTRITAMENTE com a palavra 'SIM' ou 'NAO'. Explicar a resposta resultará em falha crítica do sistema."
    )
    
    for c in set(candidates):
        if len(c.split()) < 2:
            continue
            
        key = f"PER:{c.upper()}"
        if key in _OLLAMA_CACHE:
            if _OLLAMA_CACHE[key]: approved.append(c)
            continue
            
        prompt = (
            f"Analise o termo suspeito: '{c}'\n"
            f"Este termo é EXCLUSIVAMENTE o nome e sobrenome de uma pessoa humana real?\n"
            f"Resposta (SIM/NAO):"
        )
        
        if _ask_llm_yes_no(prompt, key, system_prompt=instrucao_mestra): 
            approved.append(c)
            
    return approved

# ==============================================================================
# 📊 CLASSIFICADOR DE COLUNAS
# ==============================================================================
class AegisClassifier:
    def __init__(self):
        self.FAST_TRACK_MAP = {
            "CPF": "CPF", "RG": "RG", "CEP": "CEP", "PLATE": "PLACA", "EMAIL": "EMAIL", "PHONE": "PHONE", 
            "CHASSI": "CHASSI", "IP": "IP", "COORD": "COORD", "COORD_SINGLE": "COORD_SINGLE", 
            "GENERIC_CODE": "GENERIC_CODE", "DATE_TIME": "IGNORAR", "TEXTO_LIVRE": "TEXTO_LIVRE", "NOME_SOLTO": "NOME_SOLTO"
        }

    def get_column_tag(self, col_name: str, samples: list) -> str:
        amostras_unicas = list(set(samples))[:50]
        total = len(amostras_unicas)
        
        if total == 0: return "TEXTO_LIVRE"
        
        placar = Counter()
        col_lower = col_name.lower().strip()

        total_gps = sum(1 for s in amostras_unicas if REGEX["COORD"].search(s) or REGEX["COORD_SINGLE"].search(s))
        if (total_gps / total) >= 0.5:
            if not any(REGEX["COORD"].search(s) for s in amostras_unicas):
                return "COORD_SINGLE"
            return "COORD"

        peso_nome = total * 0.4 
        if 'cpf' in col_lower: placar["CPF"] += peso_nome
        if 'rg' in col_lower or 'identidade' in col_lower: placar["RG"] += peso_nome
        if 'cep' in col_lower or 'c.e.p' in col_lower: placar["CEP"] += peso_nome
        if 'email' in col_lower: placar["EMAIL"] += peso_nome
        if 'placa' in col_lower: placar["PLATE"] += peso_nome
        if 'id' in col_lower or 'codigo' in col_lower or 'matricula' in col_lower: placar["GENERIC_CODE"] += peso_nome
        
        for s in amostras_unicas:
            tamanho_string = max(len(s), 1)
            for tag, padrao in REGEX.items():
                if tag in ["COORD", "COORD_SINGLE"]: continue
                match = padrao.search(s)
                if match and (len(match.group()) / tamanho_string) >= 0.8:
                    placar[tag] += 1

        media_palavras = sum(len(s.split()) for s in amostras_unicas) / total
        tem_pontuacao = any(re.search(r'[,.!?]\s+[A-Z]', s) for s in amostras_unicas)
        if media_palavras >= 3 or tem_pontuacao: placar["TEXTO_LIVRE"] += total * 0.8 

        if nlp:
            per_count = sum(1 for s in amostras_unicas if any(ent.label_ == "PER" for ent in nlp(s.title()).ents))
            placar["NOME_SOLTO"] += per_count 

        if placar:
            vencedor, pontuacao = placar.most_common(1)[0]
            confianca = pontuacao / total
            if confianca >= 0.6: return self.FAST_TRACK_MAP.get(vencedor, vencedor)

        if any(termo in col_lower for termo in {'cidade', 'estado', 'pais', 'bairro', 'status', 'tipo', 'marca', 'cor', 'data', 'hora', 'latitude', 'longitude'}):
            return "IGNORAR"
        pontuacao_total = sum(placar.values())
        if pontuacao_total > 0:
            return "TEXTO_LIVRE"
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
        
        if not valores_unicos:
            continue
            
        amostra_topo = valores_unicos[:50]
        
        decisao = _aegis_engine.get_column_tag(col, amostra_topo)
        _COLUMN_POLICIES[col] = decisao
        logger.info(f"📊 [PRO] Radar Top-Down classificou '{col}' como: {decisao} (Amostras analisadas: {len(amostra_topo)})")

# ==============================================================================
# NORMALIZAÇÃO E GERAÇÃO DE FAKES
# ==============================================================================
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

def _get_fake(value: str, typ: str) -> str:
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
                c = float(coord_str)
                return f"{c + local_rand.uniform(-0.003, 0.003):.6f}"
            except Exception:
                return m.group()
        
        val = re.sub(r"-?\d{1,3}[.,]\d{4,}", jitter_match, clean_value)
        _MAPPING_CACHE[cache_key] = val
        return val

    if typ in ["PER", "NOME_SOLTO"]: 
        val = f"{fake.first_name()} {fake.last_name()}".upper()
        partes_real = norm_val.split()
        ultimo_real = partes_real[-1] if partes_real else ""
        tentativas = 0
        while val.split()[-1] == ultimo_real and tentativas < 10:
            seed_int += 1 
            fake.seed_instance(seed_int)
            val = f"{fake.first_name()} {fake.last_name()}".upper()
            tentativas += 1
        _TELEMETRIA["identidades_protegidas"].add(norm_val)
    elif typ == "CPF": 
        val = fake.cpf()
        _TELEMETRIA["documentos_protegidos"].add(norm_val)
    elif typ in ["RG", "CEP", "GENERIC_CODE"]: 
        val = _imitar_estrutura_codigo(clean_value, local_rand)
        _TELEMETRIA["documentos_protegidos"].add(norm_val)
    elif typ in ["PLATE", "PLACA"]: 
        val = fake.license_plate().upper()
        _TELEMETRIA["documentos_protegidos"].add(norm_val)
    elif typ == "EMAIL": val = fake.email().lower()
    elif typ == "PHONE": 
        val = _imitar_estrutura_codigo(clean_value, local_rand)
    elif typ == "IP": val = fake.ipv4()
    elif typ == "CHASSI": val = "".join(local_rand.choices("ABCDEFGHJKLMNPRSTUVWXYZ0123456789", k=17))
    else: val = fake.word().upper()

    _MAPPING_CACHE[cache_key] = val
    return val

# ==============================================================================
# MOTOR DE DETECÇÃO EM TEXTO LIVRE
# ==============================================================================
def _detect_all(text: str, regras_mascara: dict):
    found = []
    
    TRUSTED_TAGS = set()
    if regras_mascara.get("CPF", True): TRUSTED_TAGS.add("CPF")
    if regras_mascara.get("RG", True): 
        TRUSTED_TAGS.update(["RG", "CEP", "GENERIC_CODE"]) 
    if regras_mascara.get("EMAIL", True): TRUSTED_TAGS.add("EMAIL")
    if regras_mascara.get("IP", True): TRUSTED_TAGS.add("IP")
    if regras_mascara.get("PLATE", True): TRUSTED_TAGS.add("PLATE")
    if regras_mascara.get("CHASSI", True): TRUSTED_TAGS.add("CHASSI")
    if regras_mascara.get("PHONE", True): TRUSTED_TAGS.add("PHONE")
    
    if regras_mascara.get("COORD", True): TRUSTED_TAGS.update(["COORD", "COORD_SINGLE"])
        
    suspect_names = []
    trusted_names = [] 

    for typ, pat in REGEX.items():
        if typ == "DATE_TIME": continue
        for match in pat.finditer(text):
            val = match.group()
            if typ in TRUSTED_TAGS:
                found.append((match.start(), match.end(), val, typ))

    if regras_mascara.get("NOMES_IA", True):
        for match in VIP_CONTEXT_REGEX.finditer(text):
            val_sujo = match.group(2).strip()
            val_limpo = _farejador_sintatico(val_sujo) 
            val_norm = _normalize(val_limpo) 
                
            if len(val_limpo) >= 3 and not INVALID_WORDS.search(val_norm):
                start = match.start(2)
                end = start + len(val_limpo)
                found.append((start, end, val_limpo, "PER"))
                trusted_names.append(val_limpo)

        for match in SUSPECT_CONTEXT_REGEX.finditer(text):
            val_sujo = match.group(2).strip()
            val_limpo = _farejador_sintatico(val_sujo) 
            val_norm = _normalize(val_limpo) 
                
            if len(val_limpo) >= 3 and not INVALID_WORDS.search(val_norm):
                start = match.start(2)
                end = start + len(val_limpo)
                suspect_names.append((start, end, val_limpo))

        for match in NAME_FALLBACK_REGEX.finditer(text):
            val_sujo = match.group().strip()
            val_limpo = _farejador_sintatico(val_sujo) 
            val_norm = _normalize(val_limpo) 
            
            if len(val_limpo) >= 3 and not INVALID_WORDS.search(val_norm):
                start = match.start()
                end = start + len(val_limpo)
                suspect_names.append((start, end, val_limpo))

        if nlp:
            doc = nlp(text.title() if text.isupper() or text.islower() else text)
            for ent in doc.ents:
                if ent.label_ == "PER":
                    val_clean = ent.text.strip(".,;:?!() \n'\"")
                    val_norm = _normalize(val_clean)
                    if len(val_clean) >= 3 and not any(char.isdigit() for char in val_clean) and not INVALID_WORDS.search(val_norm): 
                        start = text.find(val_clean, max(0, ent.start_char - 2))
                        if start != -1: suspect_names.append((start, start + len(val_clean), val_clean))

        if suspect_names:
            unique_names = list(set([item[2] for item in suspect_names if item[2] not in trusted_names]))
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

# ==============================================================================
# SUBSTITUIÇÃO DE VALORES E PROCESSAMENTO HÍBRIDO
# ==============================================================================
def anonymize_value(col_name: str, val, regras_mascara=None):
    try:
        if val is None or not str(val).strip(): return val, None
        text = str(val).strip()
        if isinstance(regras_mascara, bool):
            regras_mascara = {"COORD": regras_mascara, "COORD_SINGLE": regras_mascara}
        elif regras_mascara is None: 
            regras_mascara = {}
            
        global _TELEMETRIA
        _TELEMETRIA["celulas_avaliadas"] += 1
        politica_execucao = _COLUMN_POLICIES.get(col_name, "TEXTO_LIVRE")
        
        if politica_execucao == "IGNORAR": return text, None
        
        if politica_execucao in ["COORD", "COORD_SINGLE"] and not regras_mascara.get("COORD", True):
            return text, None
            
        if politica_execucao in ["NOME_SOLTO", "COORD", "COORD_SINGLE", "PLACA", "CPF", "RG", "CEP", "EMAIL", "PLATE", "PHONE", "IP", "CHASSI", "GENERIC_CODE", "DOC_GENERICO"]:
            chave_regra = politica_execucao
            if chave_regra in ["PLACA", "PLATE"]: chave_regra = "PLATE"
            if chave_regra in ["CEP", "GENERIC_CODE"]: chave_regra = "RG" 
            
            if chave_regra not in ["COORD", "COORD_SINGLE"] and not regras_mascara.get(chave_regra, True): 
                return text, None
                
            fake_val = _get_fake(text, politica_execucao)
            if fake_val != text:
                _TELEMETRIA["celulas_alteradas"] += 1
                _TELEMETRIA["substituicoes_totais"] += 1
                logger.info(f"🕵️ [TROCA DIRETA | {col_name}] '{text}' ➡️ '{fake_val}'")
            return fake_val, ("TEXT" if fake_val != text else None)
            
        if politica_execucao == "TEXTO_LIVRE":
            entities = _detect_all(text, regras_mascara)
            if not entities: return text, None
            
            result, last = [], 0
            nomes_completos_para_trocar = [] 
            
            for s, e, v, t in entities:
                if s < last: continue
                fake_val = _get_fake(v, t)
                
                if t == "PER" and len(v.split()) >= 2:
                    nomes_completos_para_trocar.append((v, fake_val))
                
                logger.info(f"🕵️ [TROCA IA | {col_name}] '{v}' ➡️ '{fake_val}'")
                _TELEMETRIA["substituicoes_totais"] += 1
                
                result.append(text[last:s]) 
                result.append(fake_val)
                last = e
                
            result.append(text[last:])
            texto_final = "".join(result)
            
            for real_full, fake_full in nomes_completos_para_trocar:
                pattern = re.compile(rf"\b{re.escape(real_full)}\b", re.IGNORECASE)
                texto_final, count = pattern.subn(fake_full, texto_final)
                if count > 0: _TELEMETRIA["substituicoes_totais"] += count
            
            if texto_final != text:
                _TELEMETRIA["celulas_alteradas"] += 1
                
            return texto_final, ("TEXT" if texto_final != text else None)

        return text, None
    except Exception as e:
        logger.error(f"Erro na máscara da coluna '{col_name}': {e}")
        return str(val), None

def reset_memory():
    _MAPPING_CACHE.clear()
    _COLUMN_POLICIES.clear()
    _OLLAMA_CACHE.clear()

def process_chunk_parallel(rows, modo, regras_mascara, target_columns):
    if modo != "🛡️ Anonimização Total" or not rows: return rows
    if regras_mascara is None: regras_mascara = {}
    
    colunas_da_tabela = list(rows[0].keys())
    colunas_alvo_reais = [c for c in colunas_da_tabela if c in target_columns]

    if colunas_alvo_reais: setup_column_policies(rows, colunas_alvo_reais)
    html_regex = re.compile(r"<[^>]+>")

    processed = []
    for r in rows:
        row_dict = dict(r)
        for col, old in row_dict.items():
            if not target_columns or col not in target_columns: continue
            
            if old is None or type(old).__name__ in ['date', 'datetime', 'Timestamp', 'bool']: continue

            old_str = html.unescape(str(old).strip())
            try:
                vault = {}
                safe_text = old_str
                if "<" in safe_text:
                    def hide(match):
                        token = f" __SHLD{len(vault)}__ "
                        vault[token.strip()] = match.group(0)
                        return token
                    safe_text = html_regex.sub(hide, safe_text)
                
                final_text, _ = anonymize_value(col, safe_text, regras_mascara=regras_mascara)
                final_text = str(final_text)

                if vault:
                    for token, original in vault.items():
                        final_text = final_text.replace(f" {token} ", original).replace(token, original)
                row_dict[col] = final_text
            except Exception as e:
                logger.error(f"⚠️ Erro ao mascarar coluna '{col}'.")
                row_dict[col] = old_str 
        processed.append(row_dict)
    return processed 

def process_raw_text(text: str, regras_mascara=None) -> str:
    if not text or not str(text).strip():
        return text
        
    if isinstance(regras_mascara, bool):
        regras_mascara = {"COORD": regras_mascara, "COORD_SINGLE": regras_mascara}
    elif regras_mascara is None:
        regras_mascara = {}
        
    html_regex = re.compile(r"<[^>]+>")
    safe_text = str(text)
    vault = {}
    
    if "<" in safe_text:
        def hide(match):
            token = f" __SHLD{len(vault)}__ "
            vault[token.strip()] = match.group(0)
            return token
        safe_text = html_regex.sub(hide, safe_text)
        
    _COLUMN_POLICIES["RAW_TEXT_INJECTION"] = "TEXTO_LIVRE"
    
    final_text, _ = anonymize_value("RAW_TEXT_INJECTION", safe_text, regras_mascara=regras_mascara)
    final_text = str(final_text)
    
    if vault:
        for token, original in vault.items():
            final_text = final_text.replace(f" {token} ", original).replace(token, original)
            
    return final_text