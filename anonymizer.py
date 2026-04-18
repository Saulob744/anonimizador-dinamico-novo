import re
import spacy
import random
import string
import unicodedata
from faker import Faker

# =========================
# INICIALIZAÇÃO
# =========================
fake = Faker("pt_BR")
nlp = spacy.load("pt_core_news_lg")

# =========================
# REGEX COM SUPORTE A ACENTOS (LATIN-1)
# =========================
REGEX = {
    "CPF": re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    "RG": re.compile(r"\b\d{1,2}\.?\d{3,5}\.?\d{3}-?[0-9Xx]\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    
    # CORREÇÃO CRÍTICA: Trocamos o .* por [a-zA-Z0-9-]*
    # Isso obriga a Regex a procurar o número DENTRO da palavra, parando no espaço.
    "CHASSI": re.compile(r"\b(?=[a-zA-Z0-9]*\d)(?=[a-zA-Z0-9]*[a-zA-Z])[a-zA-Z0-9]{8,17}\b", re.IGNORECASE),    
    "CODE": re.compile(r"\b(?=[a-zA-Z0-9-]*\d)(?=[a-zA-Z0-9-]*[a-zA-Z])[a-zA-Z0-9-]{5,30}\b", re.IGNORECASE),
    
    "NAME_FALLBACK": re.compile(
        r"\b[A-ZÀ-Ÿ][a-zà-ÿ]{2,}(?:\s+(?:da|de|do|dos|das))?(?:\s+[A-ZÀ-Ÿ][a-zà-ÿ]{2,}){1,3}\b"
    )
}

_memory = {}

# =========================
# HELPERS DE BLINDAGEM
# =========================

def _remove_accents(input_str):
    """Transforma 'João' em 'Joao' para criar chaves de memória consistentes"""
    nfkd_form = unicodedata.normalize('NFKD', input_str)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])

def _normalize_key(v):
    """
    Cria uma chave única e limpa:
    ' João da Silva ' -> 'JOAO DA SILVA'
    """
    text = str(v).strip()
    text = _remove_accents(text)
    return " ".join(text.upper().split())

def _get(val, cat, fn):
    key = _normalize_key(val)
    if cat not in _memory: 
        _memory[cat] = {}
    
    if key not in _memory[cat]:
        _memory[cat][key] = fn()
        
    return _memory[cat][key]

# ==========================================
# PROCESSAMENTO HÍBRIDO (NLP + REGEX)
# ==========================================

def anonymize_text(text, is_vehicle_col=False):
    if not isinstance(text, str) or not text: 
        return text

    doc = nlp(text)
    entities = []
    
    # Proteção de marcas identificadas pela IA
    protected_by_ai = [
        {"start": ent.start_char, "end": ent.end_char} 
        for ent in doc.ents if ent.label_ in ["ORG", "MISC", "LOC"]
    ]

    # --- AJUSTE AQUI: Filtro de Pessoas com trava de contexto ---
    if not is_vehicle_col:
        text_lower = text.lower()
        for ent in doc.ents:
            if ent.label_ == "PER": 
                # Pega os 15 caracteres antes da entidade para checar contexto
                prefix = text_lower[max(0, ent.start_char - 15):ent.start_char]
                
                # Se vier depois de palavras que indicam objetos, ignoramos a anonimização
                if any(kw in prefix for kw in ["modelo ", "veiculo ", "carro ", "marca "]):
                    continue
                    
                entities.append({"start": ent.start_char, "end": ent.end_char, "text": ent.text, "type": "PER"})

    # O restante da função (Loop de Regex e Substituição) continua igual...

    # 2. Regex
    for label, pattern in REGEX.items():
        
        # Se for coluna de veículo, desliga a Regex "caçadora de nomes" para não destruir os modelos
        if is_vehicle_col and label == "NAME_FALLBACK":
            continue
            
        for match in pattern.finditer(text):
            start, end = match.start(), match.end()
            
            # TRAVA INTELIGENTE PARA TEXTO LIVRE: 
            # Se a Regex achar que é um "Nome", mas a IA disse que é uma "Marca" (ORG), ignoramos!
            if label == "NAME_FALLBACK":
                if any(p["start"] <= start < p["end"] or start <= p["start"] < end for p in protected_by_ai):
                    continue

            # Evita sobreposição
            if not any(e["start"] <= start < e["end"] for e in entities):
                entities.append({
                    "start": start, 
                    "end": end, 
                    "text": match.group(), 
                    "type": label
                })

    # 3. Ordenação e substituição (MANTENHA EXATAMENTE O QUE VOCÊ JÁ TEM)
    entities.sort(key=lambda x: x["start"], reverse=True)

    new_text = text
    for ent in entities:
        if ent["type"] in ["PER", "NAME_FALLBACK"]:
            subst = _get(ent["text"], "PER", lambda: fake.name().upper())
        elif ent["type"] == "CPF":
            subst = _get(ent["text"], "CPF", fake.cpf)
        elif ent["type"] == "EMAIL":
            subst = _get(ent["text"], "EMAIL", fake.email)
        else:
            subst = _get(ent["text"], ent["type"], lambda: "".join(
                random.choice(string.digits) if c.isdigit() 
                else random.choice(string.ascii_uppercase) if c.isalpha() 
                else c for c in ent["text"]
            ))
        
        new_text = new_text[:ent["start"]] + subst + new_text[ent["end"]:]

    return new_text

# ==========================================
# INTERFACE COM O BANCO DE DADOS
# ==========================================

def anonymize_value(col, val):
    if val is None: 
        return val, None
    
    # Camada 0: IDs
    if isinstance(val, int) or col.lower().endswith('_id') or col.lower() == 'id':
        return val, None

    val_str = str(val)
    col_lower = col.lower()

    # Camada 1: Colunas de Pessoas e Docs
    if any(x in col_lower for x in ["nome", "usuario", "proprietario", "cliente"]):
        if val_str.isdigit():
            return val, None
        return _get(val_str, "PER", lambda: fake.name().upper()), "PER"

    if "cpf" in col_lower:
        return _get(val_str, "CPF", fake.cpf), "CPF"
    
    if "email" in col_lower:
        return _get(val_str, "EMAIL", fake.email), "EMAIL"

    # --- A MÁGICA ACONTECE AQUI ---
    # Detecta se a coluna atual é focada no bem (veículo, modelo, marca)
    is_vehicle_col = any(x in col_lower for x in ["modelo", "veiculo", "carro", "marca", "descricao"])

    # Camada 2: Texto Livre
    # Passamos o status da coluna para a função de texto.
    new_val = anonymize_text(val_str, is_vehicle_col=is_vehicle_col)
    
    cat = "TEXT" if new_val != val_str else None
    return new_val, cat