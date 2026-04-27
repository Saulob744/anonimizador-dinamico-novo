FROM python:3.11-slim

WORKDIR /app

# =========================
# 1. PROXY (opcional)
# =========================
ARG http_proxy
ARG https_proxy
ARG no_proxy

ENV http_proxy=$http_proxy
ENV https_proxy=$https_proxy
ENV no_proxy=$no_proxy

# =========================
# 2. DEPENDÊNCIAS SISTEMA + ODBC SQL SERVER
# =========================
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    curl \
    gnupg \
    unixodbc-dev \
    ca-certificates \
    && mkdir -p /usr/share/keyrings \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/11/prod bullseye main" > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql17 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# =========================
# 3. DEPENDÊNCIAS PYTHON
# =========================
COPY requirements.txt .

RUN python -m pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# =========================
# 4. MODELO SPACY
# =========================
RUN python -m spacy download pt_core_news_lg

# =========================
# 5. APP
# =========================
COPY . .

# =========================
# 6. OTIMIZAÇÕES
# =========================
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# =========================
# 7. EXECUÇÃO
# =========================
EXPOSE 8501

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
