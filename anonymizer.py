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
# REGEX REFINADAS
# =========================
REGEX = {
    "CPF": re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    "RG": re.compile(r"\b\d{1,2}\.?\d{3,5}\.?\d{3}-?[0-9Xx]\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    
    # CHASSI/CODE: Melhorados para aceitar mais variações de separadores
    "CHASSI": re.compile(r"\b(?=[a-zA-Z0-9]*\d)(?=[a-zA-Z0-9]*[a-zA-Z])[a-zA-Z0-9]{8,17}\b", re.IGNORECASE),    
    "CODE": re.compile(r"\b(?=[a-zA-Z0-9-]*\d)(?=[a-zA-Z0-9-]*[a-zA-Z])[a-zA-Z0-9-]{4,30}\b", re.IGNORECASE),
    
    # NAME_FALLBACK: Exige 2+ palavras capitalizadas
    "NAME_FALLBACK": re.compile(
        r"\b[A-ZÀ-Ÿ][a-zà-ÿ]{2,}(?:\s+(?:da|de|do|dos|das))?\s+[A-ZÀ-Ÿ][a-zà-ÿ]{2,}(?:\s+[A-ZÀ-Ÿ][a-zà-ÿ]{2,})*\b"
    )
}

_memory = {}

# =========================
# HELPERS (MANTIDOS)
# =========================

def _remove_accents(input_str):
    nfkd_form = unicodedata.normalize('NFKD', input_str)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])

def _normalize_key(v):
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
# VALIDAR SE É NOME REAL (DINÂMICO)
# ==========================================

def _is_likely_real_name(ent):
    """
    Recebe a entidade do SpaCy e decide se é um nome humano sensível.
    """
    text = ent.text
    words = text.split()

    # 1. ÂNCORAS E PAPEIS (Filtro Dinâmico)
    # Se a palavra for um substantivo comum (NOUN), não é um nome próprio.
    # Isso protege: Agente, Suspeito, Vítima, Condutor, Policial, etc.
    if any(token.pos_ == "NOUN" for token in ent):
        return False

    # 2. ESTRUTURA
    # Nomes sensíveis em logs geralmente têm sobrenome (2+ palavras)
    if len(words) < 2:
        return False
        
    # 3. LIMPEZA
    # Nomes não contêm números
    if any(char.isdigit() for char in text):
        return False
        
    # Evita siglas curtas (ex: "BO", "ID", "DETRAN")
    if text.isupper() and len(text) < 10:
        return False
        
    return True

# ==========================================
# PROCESSAMENTO HÍBRIDO AJUSTADO
# ==========================================

def anonymize_text(text, is_vehicle_col=False):
    if not isinstance(text, str) or not text: 
        return text

    doc = nlp(text)
    entities = []
    
    # Zonas Protegidas (Marcas/Locais que não têm números)
    protected_by_ai = [
        {"start": ent.start_char, "end": ent.end_char} 
        for ent in doc.ents 
        if ent.label_ in ["ORG", "MISC", "LOC"] and not any(c.isdigit() for c in ent.text)
    ]

    # 1. Identificação de Pessoas (IA)
    if not is_vehicle_col:
        text_lower = text.lower()
        for ent in doc.ents:
            if ent.label_ == "PER": 
                # Checa se é um papel (Agente/Suspeito) ou nome real
                if _is_likely_real_name(ent): 
                    prefix = text_lower[max(0, ent.start_char - 15):ent.start_char]
                    if any(kw in prefix for kw in ["modelo ", "veiculo ", "carro ", "marca "]):
                        continue
                        
                    if not any(p["start"] <= ent.start_char < p["end"] for p in protected_by_ai):
                        entities.append({"start": ent.start_char, "end": ent.end_char, "text": ent.text, "type": "PER"})

    # 2. Identificação por Regex (O resto continua igual...)
    for label, pattern in REGEX.items():
        if is_vehicle_col and label == "NAME_FALLBACK": continue
        for match in pattern.finditer(text):
            start, end = match.start(), match.end()
            if any(e["start"] <= start < e["end"] for e in entities): continue
            if any(p["start"] <= start < p["end"] for p in protected_by_ai): continue
            
            # Aplica a mesma lógica de validação na Regex
            # Como a regex não tem tokens do SpaCy, simulamos um doc rápido
            if label == "NAME_FALLBACK":
                temp_doc = nlp(match.group())
                if not _is_likely_real_name(temp_doc):
                    continue

            entities.append({"start": start, "end": end, "text": match.group(), "type": label})

  

    # 4. Ordenação e substituição (AJUSTADO PARA EVITAR O 'VALOR')
    entities.sort(key=lambda x: x["start"], reverse=True)

    new_text = text
    for ent in entities:
        cat = "PER" if ent["type"] in ["PER", "NAME_FALLBACK"] else ent["type"]
        
        # Lógica de substituição dinâmica e consistente
        if cat == "PER":
            subst = _get(ent["text"], "PER", lambda: fake.name().upper())
        elif cat == "CPF":
            subst = _get(ent["text"], "CPF", fake.cpf)
        elif cat == "RG":
            # Gera um RG fake padrão: 12.345.678-9
            subst = _get(ent["text"], "RG", lambda: fake.bothify(text='##.###.###-#'))
        elif cat == "EMAIL":
            subst = _get(ent["text"], "EMAIL", fake.email)
        elif cat == "PLATE":
            # Gera uma placa fake padrão Mercosul ou Antiga: ABC-1234 ou ABC1D23
            subst = _get(ent["text"], "PLATE", lambda: fake.bothify(text='???-####').upper())
        elif cat == "PHONE":
            subst = _get(ent["text"], "PHONE", fake.cellphone_number)
        else:
            # Para CODE, CHASSI e outros padrões
            subst = _get(ent["text"], cat, lambda: "".join(
                random.choice(string.digits) if c.isdigit() 
                else random.choice(string.ascii_uppercase) if c.isalpha() 
                else c for c in ent["text"]
            ))
        
        new_text = new_text[:ent["start"]] + subst + new_text[ent["end"]:]

    return new_text

# ==========================================
# INTERFACE COM O BANCO (MANTIDA)
# ==========================================

def anonymize_value(col, val):
    if val is None: return val, None
    if isinstance(val, (int, float)) or col.lower() in ['id', 'uuid'] or col.lower().endswith('_id'):
        return val, None

    val_str = str(val)
    col_lower = col.lower()

    # Colunas diretas de identidade
    if any(x in col_lower for x in ["nome", "usuario", "proprietario", "cliente"]):
        if val_str.isdigit(): return val, None
        return _get(val_str, "PER", lambda: fake.name().upper()), "PER"

    # Verificação de coluna de descrição/veículo
    # Reduzi a agressividade do is_vehicle_col para permitir nomes em descrições
    is_vehicle_col = any(x in col_lower for x in ["modelo", "marca", "tipo_veiculo"])

    new_val = anonymize_text(val_str, is_vehicle_col=is_vehicle_col)
    cat = "TEXT" if new_val != val_str else None
    return new_val, cat