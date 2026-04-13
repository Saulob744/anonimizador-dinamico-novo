FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# dependências do sistema (necessário para psycopg + spacy)
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# 🔥 spaCy modelo PT (OBRIGATÓRIO no seu projeto)
RUN python -m spacy download pt_core_news_sm

COPY . .

CMD ["python", "main.py"]