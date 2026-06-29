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
from collections import Counter
from functools import lru_cache
from faker import Faker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s]: %(message)s')
logger = logging.getLogger(__name__)

SECRET_SALT = os.getenv("ANONYMIZER_SECRET_SALT", "SaltSeguroSESP2026_Producao!")

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
    print(f"📊 Células inspecionadas no banco: {_TELEMETRIA['celulas_avaliadas']}")
    print(f"🔴 Células que sofreram mutação:   {_TELEMETRIA['celulas_alteradas']}")
    print(f"🔀 Total de substituições:         {_TELEMETRIA['substituicoes_totais']}")
    print(f"👤 Pessoas/Nomes mascarados:       {len(_TELEMETRIA['identidades_protegidas'])}")
    print(f"📄 Documentos/Dados mascarados:    {len(_TELEMETRIA['documentos_protegidos'])}")
    print("="*70 + "\n")

try:
    import spacy
    nlp = spacy.load("pt_core_news_lg", disable=["lemmatizer"])
except OSError:
    logger.error("🚨 spaCy não encontrado. O farejador de verbos não funcionará.")
    nlp = None

http_session = requests.Session()
_MAPPING_CACHE = {}
_COLUMN_POLICIES = {} 
_OLLAMA_CACHE = {} 


fake = Faker("pt_BR")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3:latest")

REGEX = {
    "CPF": re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b\d{11}\b"),
    "RG": re.compile(r"\b(?:RG\s+[Nn]°?\s*)?(?:\d{1,2}\.)?\d{3}\.\d{3}[-\s]*[0-9Xx]?(?:[A-Za-z]{2})?\b"),
    "EMAIL": re.compile(r"\b[\w\.-]+@[\w\.-]+\.\w+\b", re.IGNORECASE),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}[-\s]?\d[A-Z0-9]\d{2}\b|\b[A-Z]{3}[-\s]?\d{4}\b"),
    "PHONE": re.compile(r"\b(?:\+55\s?)?(?:\(?\d{2}\)?\s?)?\d{4,5}[-\s]?\d{4}\b"),
    "CHASSI": re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b"), 
    "DATE_TIME": re.compile(r"\b\d{2,4}[/\-]\d{2}[/\-]\d{2,4}(?:\s+\d{2}:\d{2}(?:\:\d{2})?)?\b"),    
    "COORD": re.compile(r"\b-?\d{1,3}[.,]\d{4,}\s*[,;]\s*-?\d{1,3}[.,]\d{4,}\b"), 
}

VIP_CONTEXT_REGEX = re.compile(r"\b(corpo de|cadáver de|cadaver de)\s*[:\-]?\s+([A-ZÀ-Ÿa-zà-ÿ]{2,20}(?:\s+[A-ZÀ-Ÿa-zà-ÿ]{2,20}){1,5})\b", re.IGNORECASE)
SUSPECT_CONTEXT_REGEX = re.compile(r"\b(nome|cliente|paciente|sr|sra|dr|dra|vítima|vitima|autor|perito|legista|motorista|condutor)\s*[:\-]?\s+([A-ZÀ-Ÿa-zà-ÿ]{2,20}(?:\s+[A-ZÀ-Ÿa-zà-ÿ]{2,20}){1,5})\b", re.IGNORECASE)
NAME_FALLBACK_REGEX = re.compile(r"\b[A-ZÀ-Ÿ]{2,}(?:\s+(?:DE|DA|DO|DOS|DAS|E|DI)?\s*[A-ZÀ-Ÿ]{2,}){1,5}\b")

INVALID_WORDS = re.compile(
    r"\b(rua|avenida|av|travessa|trav|praca|alameda|rodovia|bairro|lote|quadra|condominio|edificio|bloco|apartamento|km|br|centro|jardim|parque|vila|"
    r"sistema|relatorio|projeto|arquivo|anexo|dados|usuario|senha|protocolo|veiculo|carro|moto|motocicleta|caminhao|bicicleta|placa|chassi|renavam|"
    r"fatal|como|sendo|passageira|passageiro|capotou|capoti|data|joaquim|tavora|carlopolis|irati|exames|historico|raca|cor|estatura|talhe|conclusao|"
    r"fratura|fechada|aberta|sangue|escoria|escoriacao|parede|espessada|massa|encefalica|dente|arcada|muscular|subjacente|lateral|terco|pleura|aparente|rosto|face|externa|extensa|"
    r"acidente|transito|hospital|lesao|ferimento|obito|entrada|boletim|ocorrencia|equipe|saude|via|publica|evento|automotor|automovel|passeio|energia|impacto|colisao|autoxauto|atropelamento|"
    r"homem|mulher|adulto|adulta|crianca|idoso|indigente|desconhecido|individuo|sexo|feminino|masculino|menino|menina|ignorado|ignorada|natimorto|feto|"
    r"condutor|NO|EM|motorista|vitima|autor|paciente|perito|NA P.R|legista|medico|policial|socorrista|agente|delegado|investigador|cadaver|corpo|falecido|falecida|suspeito|testemunha)\b",
    re.IGNORECASE
)
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
        resp = http_session.post(OLLAMA_URL, json=payload, timeout=30)
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

class AegisClassifier:
    def __init__(self):
        self.FAST_TRACK_MAP = {
            "CPF": "CPF", "RG": "RG", "PLATE": "PLACA", "EMAIL": "EMAIL", "PHONE": "PHONE", 
            "CHASSI": "CHASSI", "IP": "IP", "COORD": "COORD", "COORD_SINGLE": "COORD_SINGLE", 
            "DATE_TIME": "IGNORAR", "TEXTO_LIVRE": "TEXTO_LIVRE", "NOME_SOLTO": "NOME_SOLTO"
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
        
        for s in amostras_unicas:
            tamanho_string = max(len(s), 1)
            for tag, padrao in REGEX.items():
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
            if confianca >= 0.7: return self.FAST_TRACK_MAP.get(vencedor, vencedor)
            elif confianca >= 0.4:
                prompt = f"A amostra '{' | '.join(amostras_unicas[:5])}' da coluna '{col_name}' é do tipo '{vencedor}' ou contém nomes de pessoas? Responda APENAS 'SIM' ou 'NAO'."
                if _ask_llm_yes_no(prompt, f"COL_CONFIRM_{col_name}_{vencedor}"):
                    return self.FAST_TRACK_MAP.get(vencedor, vencedor)

        if any(termo in col_lower for termo in {'cidade', 'estado', 'pais', 'bairro', 'cep', 'status', 'tipo', 'marca', 'cor', 'data', 'hora'}):
            return "IGNORAR"
            
        prompt_fallback = f"A amostra '{' | '.join(amostras_unicas[:5])}' da coluna '{col_name}' possui dados pessoais identificáveis? Responda APENAS 'SIM' ou 'NAO'."
        if _ask_llm_yes_no(prompt_fallback, f"COL_FALLBACK_{col_name}"): return "TEXTO_LIVRE"
        return "IGNORAR"

_aegis_engine = AegisClassifier()

def setup_column_policies(rows: list, target_columns: list):
    if not target_columns: return
    for col in target_columns:
        if col in _COLUMN_POLICIES: continue
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

def _get_fake(value: str, typ: str) -> str:
    clean_value = html.unescape(re.sub(r'<[^>]+>', '', value)).strip()
    norm_val = _normalize(clean_value)
    cache_key = f"{typ}:{norm_val}"
    if cache_key in _MAPPING_CACHE: return _MAPPING_CACHE[cache_key]

    seed_int = int(hmac.new(SECRET_SALT.encode('utf-8'), norm_val.encode('utf-8'), hashlib.sha256).hexdigest()[:16], 16)
    fake.seed_instance(seed_int)
    local_rand = random.Random(seed_int)
    
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
    elif typ == "RG": 
        val = fake.numerify('##.###.###-#') 
        _TELEMETRIA["documentos_protegidos"].add(norm_val)
    elif typ in ["PLATE", "PLACA"]: 
        val = fake.license_plate().upper()
        _TELEMETRIA["documentos_protegidos"].add(norm_val)
    elif typ == "EMAIL": val = fake.email().lower()
    elif typ == "PHONE": val = fake.phone_number()
    elif typ == "IP": val = fake.ipv4()
    elif typ == "CHASSI": val = "".join(local_rand.choices("ABCDEFGHJKLMNPRSTUVWXYZ0123456789", k=17))
    else: val = fake.word().upper()

    _MAPPING_CACHE[cache_key] = val
    return val

def _detect_all(text: str, anon_loc: bool):
    found = []
    TRUSTED_TAGS = {"CPF", "RG", "EMAIL", "IP", "PLATE", "CHASSI", "PHONE"}
    suspect_names = []
    trusted_names = [] 
    
    is_text_upper = text.isupper()

    for typ, pat in REGEX.items():
        if typ == "DATE_TIME" or typ in ["COORD", "COORD_SINGLE"]: continue
        for match in pat.finditer(text):
            val = match.group()
            if typ in TRUSTED_TAGS:
                found.append((match.start(), match.end(), val, typ))

    for match in VIP_CONTEXT_REGEX.finditer(text):
        val_sujo = match.group(2).strip()
        val_limpo = _farejador_sintatico(val_sujo) 
        val_norm = _normalize(val_limpo) 
        
        if val_limpo.islower() and not is_text_upper: continue
            
        if len(val_limpo) >= 3 and not INVALID_WORDS.search(val_norm):
            start = match.start(2)
            end = start + len(val_limpo)
            found.append((start, end, val_limpo, "PER"))
            trusted_names.append(val_limpo)

    for match in SUSPECT_CONTEXT_REGEX.finditer(text):
        val_sujo = match.group(2).strip()
        val_limpo = _farejador_sintatico(val_sujo) 
        val_norm = _normalize(val_limpo) 
        
        if val_limpo.islower() and not is_text_upper: continue
            
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

def anonymize_value(col_name: str, val, anon_location: bool = True):
    try:
        if val is None or not str(val).strip(): return val, None
        text = str(val).strip()
        
        global _TELEMETRIA
        _TELEMETRIA["celulas_avaliadas"] += 1
        politica_execucao = _COLUMN_POLICIES.get(col_name, "TEXTO_LIVRE")

        if politica_execucao == "IGNORAR": return text, None
            
        if politica_execucao in ["NOME_SOLTO", "COORD", "COORD_SINGLE", "PLACA", "CPF", "RG", "EMAIL", "PLATE", "PHONE", "IP", "CHASSI", "GENERIC_CODE", "DOC_GENERICO"]:
            fake_val = _get_fake(text, politica_execucao)
            if fake_val != text:
                _TELEMETRIA["celulas_alteradas"] += 1
                _TELEMETRIA["substituicoes_totais"] += 1
                logger.info(f"🕵️ [TROCA DIRETA | {col_name}] '{text}' ➡️ '{fake_val}'")
            return fake_val, ("TEXT" if fake_val != text else None)
            
        if politica_execucao == "TEXTO_LIVRE":
            entities = _detect_all(text, anon_location)
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

def process_chunk_parallel(rows, modo, anon_geo, target_columns):
    if modo != "🛡️ Anonimização Total" or not rows: return rows
    colunas_da_tabela = list(rows[0].keys())
    colunas_alvo_reais = [c for c in colunas_da_tabela if c in target_columns]

    if colunas_alvo_reais: setup_column_policies(rows, colunas_alvo_reais)
    html_regex = re.compile(r"<[^>]+>")

    processed = []
    for r in rows:
        row_dict = dict(r)
        for col, old in row_dict.items():
            if not target_columns or col not in target_columns: continue
            if old is None or type(old).__name__ in ['date', 'datetime', 'Timestamp', 'bool', 'int', 'float']: continue

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
                
                final_text, _ = anonymize_value(col, safe_text, anon_location=anon_geo)
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