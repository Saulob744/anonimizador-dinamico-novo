# 🛡️ Aegis Anonymizer Pro

### _Dados reais. Zero exposição._

<p align="center">
  <b>Anonimização inteligente de dados com IA + Engenharia de Dados</b><br>
  Transforme bancos de produção em ambientes seguros para testes e homologação.
</p>

---

## 🚀 Sobre o projeto

O **Aegis Anonymizer** é uma ferramenta projetada para anonimizar dados sensíveis (**PII**) de forma **realista, consistente e segura**.

Diferente de soluções tradicionais, ele combina:

- 🤖 **NLP (SpaCy)** → entende contexto humano
- 🔍 **Regex avançadas** → detecta padrões estruturados
- 🧠 **Memória consistente** → mantém coerência dos dados

---

## ⚡ O que ele resolve

✔ Permite usar dados reais sem expor pessoas  
✔ Gera bases de homologação confiáveis  
✔ Remove dados sensíveis mesmo em textos livres

---

## 🧠 Exemplo prático

```text
Entrada:
"João da Silva realizou a operação"

Saída:
"CARLOS PEREIRA realizou a operação"

docker build -t aegis-pro .
docker run -p 8501:8501 aegis-pro

http://localhost:8501


pip install -r requirements.txt
python -m spacy download pt_core_news_lg
streamlit run app.py


| Sistema       | Host                   |
| ------------- | ---------------------- |
| Windows / Mac | `host.docker.internal` |
| Linux         | `172.17.0.1`           |
```
