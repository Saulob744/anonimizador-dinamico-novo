# 🛡️ Aegis Anonymizer Pro

O **Aegis Anonymizer Pro** é um pipeline de Prevenção de Perda de Dados (DLP) projetado para ambientes de rede restrita. O sistema combina Processamento de Linguagem Natural (NLP) e Inteligência Artificial Local para mascarar identidades e localizações em bancos de dados relacionais, garantindo conformidade com a LGPD e soberania total dos dados (zero tráfego externo).

---

## 🚀 Funcionalidades Principais

* **Classificador Estatístico:** Toma decisões automáticas de roteamento de dados com base em amostragem rápida.
* **Motor de IA Local:** Utiliza LLM (Ollama/Llama 3) com parâmetros restritos para atuar estritamente como validador de dados, sem gerar textos livres.
* **K-Anonimato Determinístico:** Garante que o mesmo dado real sempre gere a mesma máscara, permitindo cruzamentos estatísticos seguros.
* **Desvio Espacial (GPS Jitter):** Aplica uma máscara matemática sobre coordenadas geográficas para proteger a localização exata, mantendo a mancha criminal geral.
* **Cofre de Formatação:** Preserva marcações de sistema e HTML intactas durante o processo de anonimização.
* **Processamento Paralelo:** Motor assíncrono projetado para a manipulação eficiente de grandes tabelas sem sobrecarga de memória RAM.

---

## 🛠️ Pré-requisitos e Instalação da IA

O motor de IA do Aegis depende de um servidor cognitivo rodando em background na sua máquina hospedeira.

1. **Instalação do Ollama:** Acesse o site oficial do Ollama, faça o download do executável padrão para o seu sistema operacional e conclua a instalação básica.
2. **Download do Modelo:** Com o Ollama instalado e rodando, abra o seu terminal e execute o comando abaixo para baixar o modelo base (Llama 3):

```bash
ollama pull llama3:latest

```

3. **Ambiente de Execução:** Python 3.10+ instalado na máquina.
4. **Conteinerização:** Docker instalado (Opcional, mas recomendado).

---

## ⚙️ Execução do Sistema

Você pode iniciar a aplicação utilizando Docker ou rodando diretamente na sua máquina local de forma nativa.

### Método 1: Via Docker (Recomendado)

Abra o terminal na pasta raiz do projeto e construa a imagem do sistema:

```powershell
docker build -t aegis-motor .

```

Em seguida, inicie o contêiner mapeando a comunicação para o Ollama local:

```powershell
docker run -p 8501:8501 -e OLLAMA_URL="http://host.docker.internal:11434/api/generate" aegis-motor

```

### Método 2: Execução Local Nativa

Garanta que o servidor do Ollama já está rodando em background. Abra o terminal na pasta do projeto e prepare o ambiente Python:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m spacy download pt_core_news_lg
streamlit run app.py

```

> **Nota de Acesso:** Em ambos os métodos, após iniciar o sistema, a interface gráfica estará disponível no seu navegador de preferência através do endereço `http://localhost:8501`.

---

## 📂 Estrutura do Projeto

| Arquivo | Função no Sistema |
| --- | --- |
| `app.py` | Interface interativa (Streamlit) e orquestrador de estado do usuário. |
| `anonymizer.py` | Núcleo DLP contendo as expressões regulares, trava de LLM e processamento do spaCy. |
| `db_utils.py` | Gerenciador de conexão com o banco e orquestrador de processamento em blocos. |
| `requirements.txt` | Lista de bibliotecas de terceiros homologadas para o projeto. |

---

## 🔒 Diretrizes de Segurança (LGPD)

Toda a arquitetura foi desenhada para processamento **offline e isolado**. O tráfego de laudos e identidades ocorre estritamente no ambiente local (`127.0.0.1`), sendo expurgado da memória RAM imediatamente após a geração do dado anonimizado. Nenhuma API externa ou serviço em nuvem tem acesso aos dados processados por esta ferramenta.
