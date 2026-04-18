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

Para que o seu repositório no GitHub tenha um aspecto profissional e de "nível sênior", o segredo está no uso de **Badges**, **Callouts** (alertas visuais) e **Divisores de Seção**.

Aqui está o conteúdo formatado em **Markdown Puro**, com a estrutura exata que o GitHub utiliza para estilizar descrições de projetos de software:

-----

# 🛡️ Aegis Anonymizer Pro

> **AI-Powered Data Privacy & ETL Pipeline**

O **Aegis Anonymizer** é uma solução avançada para o tratamento de dados sensíveis em ambientes de homologação. Utilizando **Processamento de Linguagem Natural (NLP)** e heurísticas de engenharia de dados, o sistema garante que bases de teste sejam seguras e fiéis à realidade, mantendo a conformidade com a LGPD.

-----

## 💎 Diferenciais do Projeto

  * **🧠 Inteligência Híbrida:** Identifica nomes próprios em textos livres (logs, observações) onde Regex comuns falham.
  * **🔗 Integridade Relacional:** Mantém a consistência entre tabelas, permitindo que o sistema continue funcional após a anonimização.
  * **⚡ Streaming de Dados:** Arquitetura baseada em *chunks* para processar grandes volumes com baixo consumo de memória.
  * **⏱️ ETA Dinâmico:** Estimativa de conclusão baseada na performance real do modelo de IA por linha.

-----

## 🚀 Como Executar

O uso via **Docker** é o método recomendado, pois isola as dependências de sistema e os drivers de banco de dados.

### 🐳 Via Docker (Recomendado)

```bash
# 1. Build da Imagem
docker build -t aegis-pro .

# 2. Execução do Container
docker run -p 8501:8501 aegis-pro
```

### 🐍 Instalação Manual (Local)

```bash
pip install -r requirements.txt
python -m spacy download pt_core_news_lg
streamlit run app.py
```

-----

## 🛠️ Guia de Conectividade (Networking)

> [\!IMPORTANT]
> Se o seu banco de dados estiver na mesma máquina onde o Docker está rodando, utilize os seguintes endereços no campo **Host**:
>
>   * **Windows/Mac:** `host.docker.internal`
>   * **Linux:** `172.17.0.1`

-----

## ⚙️ Arquitetura do Pipeline

O Aegis opera em um ciclo de quatro etapas automatizadas:

1.  **Mapping:** Escaneamento de chaves primárias e estrangeiras.
2.  **Classification:** Separação entre dados técnicos (`SKIP`), diretos (`SENSITIVE`) e textos livres (`TEXT`).
3.  **Bypass:** Desativação temporária de restrições de integridade no destino para carga em alta velocidade.
4.  **Anonymization:** Substituição por dados sintéticos consistentes via Faker e IA.

-----

## 📋 Configuração da Interface

| Parâmetro | Finalidade | Exemplo |
| :--- | :--- | :--- |
| **Host** | Endereço do Servidor | `10.5.1.20` ou `host.docker.internal` |
| **Porta** | Porta do Serviço | `5432` (PG) ou `1433` (MSSQL) |
| **Banco Origem** | Fonte dos dados reais | `db_producao` |
| **Banco Destino** | Alvo da anonimização | `db_homologacao` |
| **Chunk Size** | Lote de processamento | `1000` |

-----

> [\!CAUTION]
> **SEGURANÇA EM PRIMEIRO LUGAR** \> O software executa comandos de limpeza (`TRUNCATE`) no banco de destino. Certifique-se de que o destino **não** seja o ambiente de produção.

-----

**Desenvolvido para segurança da informação em escala.** 🏛️
