import re
import spacy
import random
import string
from faker import Faker

# Inicialização
fake = Faker("pt_BR")
# Carrega o modelo de NLP (o 'lg' é o mais preciso para nomes brasileiros)
nlp = spacy.load("pt_core_news_lg")

# Mantemos suas Regex (são ótimas para dados estruturados)
REGEX = {
    "CPF": re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    "RG": re.compile(r"\b\d{1,2}\.?\d{3,5}\.?\d{3}-?[0-9Xx]\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    "CODE": re.compile(r"\b[A-Z]{2,5}-\d+[A-Z0-9]*\b")
}

_memory = {}

def _get(val, cat, fn):
    """Sua lógica de memória para manter integridade relacional"""
    val_norm = str(val).strip().upper()
    if cat not in _memory: _memory[cat] = {}
    if val_norm not in _memory[cat]:
        _memory[cat][val_norm] = fn()
    return _memory[cat][val_norm]

# ==========================================
# CAMADA 1: MOTOR NLP (Para Nomes em Frases)
# ==========================================
def _get_nlp_entities(text):
    """Usa IA para mapear nomes no texto"""
    doc = nlp(text)
    entities = []
    for ent in doc.ents:
        if ent.label_ == "PER":  # Se a IA diz que é Pessoa
            entities.append({
                "start": ent.start_char,
                "end": ent.end_char,
                "text": ent.text,
                "type": "PER"
            })
    return entities

# ==========================================
# CAMADA 2: MOTOR ESTRUTURAL (Para Documentos)
# ==========================================
def _get_regex_entities(text):
    """Usa suas Regex para mapear documentos"""
    entities = []
    for key, pattern in REGEX.items():
        for match in pattern.finditer(text):
            entities.append({
                "start": match.start(),
                "end": match.end(),
                "text": match.group(),
                "type": key
            })
    return entities

# ==========================================
# CAMADA 3: O ORQUESTRADOR (A Unificação)
# ==========================================
def anonymize_text(text):
    if not isinstance(text, str): return text

    # 1. Mapeia tudo
    all_entities = _get_nlp_entities(text) + _get_regex_entities(text)
    
    # 2. Ordena do fim para o começo (para não quebrar os índices ao substituir)
    all_entities.sort(key=lambda x: x["start"], reverse=True)

    # 3. Substitui usando sua lógica de memória
    new_text = text
    for ent in all_entities:
        # Define qual função do Faker usar baseado no tipo
        if ent["type"] == "PER":
            subst = _get(ent["text"], "PER", lambda: fake.name().upper())
        elif ent["type"] == "CPF":
            subst = _get(ent["text"], "CPF", fake.cpf)
        elif ent["type"] == "EMAIL":
            subst = _get(ent["text"], "EMAIL", fake.email)
        else:
            # Para códigos genéricos, usa sua máscara de padrão
            subst = _get(ent["text"], ent["type"], lambda: "".join(
                random.choice(string.digits) if c.isdigit() else random.choice(string.ascii_uppercase) if c.isalpha() else c 
                for c in ent["text"]
            ))
        
        # Aplica a troca na "coordenada" exata
        new_text = new_text[:ent["start"]] + subst + new_text[ent["end"]:]

    return new_text

def anonymize_value(col, val):
    """Sua função principal de entrada"""
    if val is None: return val, None
    
    val_str = str(val)
    col_lower = col.lower()

    # Se a coluna já indica o tipo, não precisamos de NLP (Ganhamos performance)
    if "cpf" in col_lower:
        return _get(val_str, "CPF", fake.cpf), "CPF"
    if "nome" in col_lower or "usuario" in col_lower:
        return _get(val_str, "PER", lambda: fake.name().upper()), "PER"

    # Se for texto livre ou coluna desconhecida, passa pelo Pipeline Híbrido
    new_val = anonymize_text(val_str)
    return new_val, "MIXED"