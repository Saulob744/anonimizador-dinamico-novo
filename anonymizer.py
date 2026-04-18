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
# Adicionamos classes de caracteres que cobrem á, é, í, ó, ú, ã, õ, ç, etc.
REGEX = {
    "CPF": re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    "RG": re.compile(r"\b\d{1,2}\.?\d{3,5}\.?\d{3}-?[0-9Xx]\b"),
    "PHONE": re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})-?\d{4}\b"),
    "EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    "PLATE": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b", re.IGNORECASE),
    
    # NOVO: CHASSI (VIN) - 17 caracteres alfanuméricos
    "CHASSI": re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE),
    
    # MELHORADO: Códigos com hífens ou misturas densas de letras e números
    "CODE": re.compile(r"\b(?=[A-Z]*\d)(?=[\d]*[A-Z])[A-Z0-9-]{5,}\b", re.IGNORECASE),
    
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

def anonymize_text(text):
    if not isinstance(text, str) or not text: 
        return text

    doc = nlp(text)
    entities = []

    # 1. NLP com Validação Dinâmica
    for ent in doc.ents:
        if ent.label_ == "PER":
            # --- TRAVA DINÂMICA 1: Substantivo Comum ---
            # Se a palavra principal for um substantivo comum (suspeito, vítima), ignore.
            if any(t.pos_ == "NOUN" for t in ent):
                continue
            
            # --- TRAVA DINÂMICA 2: Presença de Números ---
            # Se o "nome" tiver números (ex: Gol 1.0, BMW X6), ignore. 
            # Nomes de pessoas não têm números.
            if any(char.isdigit() for char in ent.text):
                continue
                
            entities.append({"start": ent.start_char, "end": ent.end_char, "text": ent.text, "type": "PER"})

    # 2. Regex (Apanha CHASSI, Documentos e Placas)
    for label, pattern in REGEX.items():
        for match in pattern.finditer(text):
            if not any(e["start"] <= match.start() < e["end"] for e in entities):
                entities.append({
                    "start": match.start(), 
                    "end": match.end(), 
                    "text": match.group(), 
                    "type": label
                })

    # 3. Ordenação reversa para substituição segura
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
            # Para códigos, preserva hífens e números mas troca os valores
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
    
    # --- CAMADA DE SEGURANÇA 0: IDs e INTEIROS ---
    # Se o valor original for um número (int) ou a coluna terminar em _id,
    # nós NÃO rodamos o anonimizador de texto.
    if isinstance(val, int) or col.lower().endswith('_id') or col.lower() == 'id':
        return val, None

    val_str = str(val)
    col_lower = col.lower()

    # --- CAMADA DE SEGURANÇA 1: COLUNAS DIRETAS ---
    # Só anonimizamos como NOME se não for um ID
    if any(x in col_lower for x in ["nome", "usuario", "proprietario", "cliente"]):
        # Verificação extra: se o conteúdo parece um número, não é um nome real
        if val_str.isdigit():
            return val, None
        return _get(val_str, "PER", lambda: fake.name().upper()), "PER"

    if "cpf" in col_lower:
        return _get(val_str, "CPF", fake.cpf), "CPF"
    
    if "email" in col_lower:
        return _get(val_str, "EMAIL", fake.email), "EMAIL"

    # --- CAMADA DE SEGURANÇA 2: TEXTO LIVRE ---
    new_val = anonymize_text(val_str)
    cat = "TEXT" if new_val != val_str else None
    return new_val, cat