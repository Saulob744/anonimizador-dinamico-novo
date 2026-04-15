FROM python:3.11-slim
WORKDIR /app
# 1. Configurações de Proxy (Recebidas via --build-arg)
ARG http_proxy
ARG https_proxy
ARG no_proxy

# Transformando os argumentos em variáveis de ambiente para o sistema Linux
ENV http_proxy=$http_proxy
ENV https_proxy=$https_proxy
ENV no_proxy=$no_proxy



# 2. Instala dependências de sistema (Configura o proxy para o APT)
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 3. Cache de dependências Python
COPY requirements.txt .

# 4. Instalação com "Trusted Hosts" (Mesma lógica que usamos no terminal)
# Isso impede que o Docker pare por erro de certificado SSL da SESP
RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r requirements.txt \
    --trusted-host pypi.org \
    --trusted-host files.pythonhosted.org \
    --trusted-host pypi.python.org

# 5. Baixa o modelo do spacy (Também usando o proxy configurado)
RUN python -m spacy download pt_core_news_lg

# 6. Copia o restante do código
COPY . .

# 7. Configurações de execução
EXPOSE 8501
ENV PYTHONUNBUFFERED=1

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]