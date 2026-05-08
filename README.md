# 🛡️ ANONIMIZADOR SESP (Local AI Edition)

Este projeto é uma ferramenta de anonimização de dados sensíveis desenvolvida para operar em ambiente de rede restrita (SESP). Ele utiliza uma abordagem híbrida de **Expressões Regulares (RegEx)** para dados estruturados e **Inteligência Artificial Local (Ollama/Llama 3)** para textos não estruturados.

## 🚀 Funcionalidades

* **NER (Named Entity Recognition) Local:** Extração de nomes próprios via Llama 3 sem saída de dados para a internet.
* **Detecção via RegEx:** Identificação instantânea de CPFs, RGs, Placas e Coordenadas Geográficas.
* **Substituição Consistente:** Garante que o mesmo dado real sempre receba o mesmo dado falso (preserva o cruzamento de dados).
* **Interface Streamlit:** Painel amigável para carregar arquivos e configurar o processamento.

## 🛠️ Pré-requisitos

1.  **Ollama instalado** (v0.1.x ou superior).
2.  **Modelo Llama 3** baixado (`ollama pull llama3`).
3.  **Python 3.10+** e ambiente virtual configurado.

## ⚙️ Configuração do Ambiente (Modo SESP)

Para rodar o projeto atrás do proxy da SESP, utilize o protocolo de inicialização:

1.  Abra o PowerShell.
2.  Execute o script de proxy (configurando `$env:HTTP_PROXY`).
3.  Inicie o servidor: `ollama serve`.

## 📂 Estrutura do Projeto

* `app.py`: Interface gráfica e orquestração do banco de dados.
* `anonymizer.py`: O "cérebro" do sistema (RegEx + Conexão com Ollama).
* `requirements.txt`: Dependências do projeto (Pandas, Streamlit, Requests, Faker).

## 🔒 Segurança e Privacidade

O sistema opera via **Localhost (127.0.0.1:11434)**. Os dados trafegam apenas entre a memória RAM e o processador local, garantindo conformidade total com a LGPD e soberania dos dados da SESP.


# 1. Mata qualquer processo travado do Ollama para limpar a fila
$BSTR = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($proxyPass)
$plainPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($BSTR)

# 3. Define as variáveis de ambiente para o Ollama conseguir "enxergar" a rede
$env:HTTP_PROXY = "http://${proxyUser}:${plainPassword}@proxy00.sesp.parana:8080"
$env:HTTPS_PROXY = $env:HTTP_PROXY

Write-Host "✅ Proxy configurado. Iniciando servidor..." -ForegroundColor Green

# 4. Inicia o servidor (Mantenha esta janela aberta)
ollama serve
