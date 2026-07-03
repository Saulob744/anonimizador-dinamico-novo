FROM python:3.11-slim

WORKDIR /app

# =========================
# 1. DEPENDÊNCIAS SISTEMA
# =========================
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    curl \
    gnupg \
    unixodbc-dev \
    ca-certificates \
    && mkdir -p /usr/share/keyrings \
    && curl -k -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/11/prod bullseye main" > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql17 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# =========================
# 2. DEPENDÊNCIAS PYTHON
# =========================
COPY requirements.txt .

RUN python -m pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt \
    --trusted-host pypi.org \
    --trusted-host files.pythonhosted.org \
    --trusted-host pypi.python.org

# =========================
# 3. MODELO SPACY
# =========================
RUN python -m spacy download pt_core_news_lg

# =========================
# 4. APP
# =========================
COPY . .

# =========================
# 5. EXECUÇÃO
# =========================
EXPOSE 8501
ENV PYTHONUNBUFFERED=1

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]