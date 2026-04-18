FROM python:3.11-slim

WORKDIR /app

# 1. Configurações de Proxy
ARG http_proxy
ARG https_proxy
ARG no_proxy

ENV http_proxy=$http_proxy
ENV https_proxy=$https_proxy
ENV no_proxy=$no_proxy

# 2. Instala dependências de sistema e drivers para SQL Server
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    curl \
    gnupg2 \
    unixodbc-dev \
    && curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - \
    && curl https://packages.microsoft.com/config/debian/11/prod.list > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql17 \
    && rm -rf /var/lib/apt/lists/*

# 3. Cache de dependências Python
COPY requirements.txt .

# 4. Instalação do pip com Trusted Hosts para ignorar bloqueios de SSL do proxy
RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r requirements.txt \
    --trusted-host pypi.org \
    --trusted-host files.pythonhosted.org \
    --trusted-host pypi.python.org

# 5. Baixa o modelo do spacy (usando o proxy configurado no ENV)
RUN python -m spacy download pt_core_news_lg

# 6. Copia o restante do código
COPY . .

# 7. Configurações de execução
EXPOSE 8501
ENV PYTHONUNBUFFERED=1

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]