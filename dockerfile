FROM python:3.11-slim

WORKDIR /app

# 1. Instala dependências de sistema
# Adicionei o 'curl' caso precise debugar conexão com o banco dentro do container
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 2. Cache de dependências Python
COPY requirements.txt .

# Configurações de Proxy (se necessário no seu ambiente corporativo)
ARG http_proxy
ARG https_proxy

RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r requirements.txt

# 3. Baixa o modelo de IA 
# DICA: Se você colocou o link do .whl no requirements.txt, esta linha abaixo é DESNECESSÁRIA.
# Se deixou o requirements limpo, mantenha esta linha:
RUN python -m spacy download pt_core_news_lg

# 4. Copia o restante do código
COPY . .

# 5. Configurações de execução
EXPOSE 8501

# Variável de ambiente para garantir que o log do Python apareça no terminal do Docker
ENV PYTHONUNBUFFERED=1

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]