Como Engenheiro de Segurança, vejo que o seu projeto evoluiu de um simples "script de substituição" para um verdadeiro **Motor de Prevenção de Perda de Dados (DLP)** de grau militar. O seu README antigo já não faz justiça ao que construímos.

Vou reescrever a documentação completa em Markdown, estruturando o panorama geral da arquitetura atual, as dependências do Ollama e os guias de instalação (tanto o moderno via Docker quanto o clássico via máquina local com proxy).

Copie o texto abaixo e cole no seu arquivo `README.md`:

---

# 🛡️ Aegis Anonymizer Pro (SESP Local AI Edition)

O **Aegis Anonymizer Pro** é um pipeline híbrido de Prevenção de Perda de Dados (DLP) projetado para operar em ambientes de rede restrita e dados sensíveis (Secretaria de Segurança Pública - SESP).

Ele substituiu a abordagem simples de *Find & Replace* por um motor cognitivo avançado. O sistema atua como um "tribunal de dados", combinando Matemática, Processamento de Linguagem Natural (NLP) e Inteligência Artificial Local para mascarar identidades e localizações em bancos de dados relacionais gigantescos, garantindo 100% de conformidade com a LGPD e soberania total (Zero tráfego externo).

## 🚀 Arquitetura e Funcionalidades

* **AegisClassifier (Zero Trust Radar):** Um classificador estatístico que analisa as primeiras 50 amostras válidas de cada coluna e toma decisões automáticas de roteamento (ex: Fast-Track para CPFs puros ou Fallback Paranoico para textos ambíguos).
* **LLM Lockdown (Ollama/Llama 3):** IA operando com "camisa de força" cognitiva (`temperature: 0.0`, `top_k: 1`). Ele não conversa, atua estritamente como um juiz binário (SIM/NAO) para validação de nomes próprios em laudos de texto livre, sem alucinar.
* **Determinismo Criptográfico (K-Anonimato):** Usando sementes `HMAC` e um `SECRET_SALT`, o mesmo dado real sempre gera o mesmo dado falso (ex: "João" sempre vira "Pedro"), permitindo cruzamento seguro de dados estatísticos.
* **Desvio Espacial (GPS Jitter):** Máscara matemática aplicada sobre coordenadas geográficas (+/- 0.003 graus) que desloca a ocorrência do local exato, mas preserva a mancha criminal no mapa da cidade.
* **Cofre de Formatação (HTML Vault):** Protege e devolve marcações de sistema (como `<br>`, `<div>`) intactas após a anonimização, garantindo que o front-end dos laudos não quebre.
* **Ghost Worker (Processamento Paralelo):** Motor assíncrono projetado para "sugar", processar em múltiplos núcleos de CPU e replicar tabelas inteiras de banco de dados sem sobrecarregar a memória RAM.

---

## 🛠️ Pré-requisitos do Sistema

Para o motor de IA funcionar, você precisa do servidor cognitivo rodando em background:

1. **Ollama:** Instalado e rodando (v0.1.x ou superior).
2. **Modelo Llama 3:** O cérebro do sistema. Baixe executando o comando no terminal:
```bash
ollama pull llama3:latest

```


3. **Python 3.10+** (Para execução Bare Metal).
4. **Docker** (Opcional, mas recomendado para implantação).

---

## 🐳 Método 1: Desempacotamento via Docker (Recomendado)

O método mais seguro para rodar a aplicação sem conflitos de bibliotecas na máquina do usuário.

**1. Construir a Imagem (Build):**
No terminal, dentro da pasta do projeto, execute:

```powershell
docker build -t aegis-motor .

```

**2. Executar o Contêiner (Run):**
Como o Ollama roda na máquina host do Windows e o código dentro do Docker, usamos `host.docker.internal` para criar a ponte segura:

```powershell
docker run -p 8501:8501 -e OLLAMA_URL="http://host.docker.internal:11434/api/generate" aegis-motor

```

**3. Acessar o Painel:**
Abra o navegador e acesse: `http://localhost:8501`

---

## 💻 Método 2: Execução "Bare Metal" (Máquina Local + Proxy SESP)

Para rodar diretamente na máquina, especialmente atrás de firewalls e proxys corporativos rigorosos.

### Passo A: Configurando a Rede e o Ollama (PowerShell)

Abra um terminal PowerShell e rode este script para furar o proxy e subir a IA:

```powershell
# 1. Solicita credenciais do usuário para o Proxy
$proxyUser = Read-Host "Digite seu usuário da SESP"
$proxyPass = Read-Host "Digite sua senha" -AsSecureString

# 2. Descriptografa a senha para injeção na variável
$BSTR = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($proxyPass)
$plainPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($BSTR)

# 3. Define as variáveis de ambiente para o Ollama conseguir "enxergar" a rede e baixar modelos
$env:HTTP_PROXY = "http://${proxyUser}:${plainPassword}@proxy00.sesp.parana:8080"
$env:HTTPS_PROXY = $env:HTTP_PROXY

Write-Host "✅ Proxy configurado. Iniciando servidor..." -ForegroundColor Green

# 4. Inicia o servidor Ollama (Mantenha esta janela aberta)
ollama serve

```

### Passo B: Subindo a Interface Streamlit

Abra um **segundo terminal**, ative seu ambiente virtual e inicie o motor Python:

```powershell
# 1. Ative o ambiente virtual
.\.venv\Scripts\Activate.ps1

# 2. Instale as dependências (Se for a primeira vez)
pip install -r requirements.txt
python -m spacy download pt_core_news_lg

# 3. Inicie o sistema de controle Aegis
streamlit run app.py

```

---

## 📂 Estrutura Interna do Projeto

* `app.py`: Interface gráfica interativa (Streamlit) e orquestrador de telemetria/estado.
* `anonymizer.py`: O "Núcleo de DLP". Contém as Regex, a trava do LLM (`Ollama`), o classificador de colunas, os farejadores do `spaCy` e o cofre de HTML.
* `db_utils.py` *(se aplicável)*: Orquestrador de chunks e comunicação com o banco de dados via SQLAlchemy/PyODBC.
* `requirements.txt`: Relação de blindagem de bibliotecas (Pandas, Streamlit, Requests, Faker, spaCy).

---

## 🔒 Segurança e Soberania (LGPD)

Este software foi auditado para **não realizar saídas de rede externa** durante o processamento de dados.
O tráfego de laudos e identidades ocorre estritamente pela porta `127.0.0.1:11434` (ou rede interna do Docker), morrendo na memória RAM imediatamente após a geração do dado K-Anonimizado. Nenhuma API de terceiros (como OpenAI ou Google) tem acesso à base criminal processada.
