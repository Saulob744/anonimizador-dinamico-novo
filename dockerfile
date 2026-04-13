FROM python:3.11-slim

WORKDIR /app

# Instala dependências de sistema para o Postgres e IA
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Configurações de Proxy passadas no Build
ARG http_proxy
ARG https_proxy

RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r requirements.txt

# BAIXA O MODELO DE IA (Isso demora um pouco, mas é essencial)
RUN python -m spacy download pt_core_news_lg

COPY . .

# O Streamlit usa a porta 8501 por padrão, vamos expor ela
EXPOSE 8501

# Comando para rodar o servidor Streamlit
# Substitua a linha do CMD por esta:
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]