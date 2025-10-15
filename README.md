# bpmn2neo

> Load BPMN 2.0 diagrams into Neo4j and create node embeddings for Graph-RAG.

[![PyPI - Version](https://img.shields.io/pypi/v/bpmn2neo.svg)](https://pypi.org/project/bpmn2neo/)
[![Python](https://img.shields.io/pypi/pyversions/bpmn2neo.svg)](https://pypi.org/project/bpmn2neo/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Neo4j](https://img.shields.io/badge/Neo4j-5.x-008CC1?logo=neo4j&logoColor=white)](https://neo4j.com/)
[![BPMN 2.0](https://img.shields.io/badge/BPMN-2.0-6B4FA3)](https://www.omg.org/spec/BPMN/2.0)

---

## 1) Overview
<img width="547" height="395" alt="Image" src="https://github.com/user-attachments/assets/2027ec46-9a81-4468-ab81-ec340980a74c" />

**bpmn2neo** ingests BPMN 2.0 (`.bpmn`) files, persists a clean BPMN knowledge graph in **Neo4j 5.x**, then generates **hierarchical embeddings** for Graph-RAG.  
The library is production-oriented: clean configuration via Pydantic `.env`, and a minimal public API.

- **Parser & Loader**: turn `.bpmn` XML into a normalized Neo4j schema.
- **Embedding Orchestrator**: builds node-level context and embeddings in a top-down order (Collaboration → Participant → Process → Lane → FlowNode).
- **Graph-RAG ready**: each node stores explanatory text and vector.

---

## 2) Key Features

### BPMN → Neo4j Loader
- Robust XML parsing with clear phases (collaboration/participants → processes → lane → flownode → message flow/data objec/etc.).
- Builds nodes and relationships, mapping each BPMN element’s tag attributes to node properties.
- Structured logs for each phase, with try/except and error reporting.

👉 **Schema details**: see the guide  
**[Neo4j Graph Schema Guide](docs/Neo4j%20Graph%20Schema%20Guide.md)**

### Hierarchical Context & Embedding
- For each node, the pipeline runs:  
  **Reader → Builder → ContextWriter → Embedder**
- Generates text per node, then embeds using the configured embedding model.
- **Order**: FlowNode → Lane → Process → Participant → Model (Collaboration).  
  This preserves hierarchy so parents can summarize/aggregate children.

👉 **Embedding details**: see the guide  
**[BPMN KG Context Embedding Guide](docs/BPMN%20KG%20Context%20Embedding%20Guide.md)**

---

## 3) Usage

### Installation
```bash
pip install bpmn2neo
```

### setting .env
```env
# =========================
# bpmn2neo .env
# =========================
# --- Neo4j (REQUIRED) ---
B2N_NEO4J__URI="YOUR_URI"        # or neo4j+s://<host>:7687
B2N_NEO4J__USERNAME="YOUR_USERNAME"
B2N_NEO4J__PASSWORD="YOUR_PASSWORD"      # if you prefer keyring, comment this and use PASSWORD_ALIAS below
B2N_NEO4J__DATABASE="YOUR_DATABASE"

# Optional Neo4j tweaks:
# B2N_NEO4J__PASSWORD_ALIAS=bpmn2neo/neo4j      # resolve secret via OS keyring
# B2N_NEO4J__LOG_LEVEL=INFO                     # INFO/DEBUG/WARN/ERROR

# --- OpenAI (REQUIRED for embeddings/text) ---
B2N_OPENAI__API_KEY="YOUR_API_KEY"     # if you prefer keyring, comment this and use API_KEY_ALIAS below
B2N_OPENAI__EMBEDDING_MODEL="text-embedding-3-large"

# Optional OpenAI tweaks:
# B2N_OPENAI__API_KEY_ALIAS=bpmn2neo/openai     # resolve secret via OS keyring
# B2N_OPENAI__EMBEDDING_DIMENSION=3072
# B2N_OPENAI__TRANSLATION_MODEL=gpt-4o-mini
# B2N_OPENAI__TEMPERATURE=0.2
# B2N_OPENAI__MAX_TOKENS_FULL=600
# B2N_OPENAI__MAX_TOKENS_SUMMARY=200
# B2N_OPENAI__MAX_RETRIES=3
# B2N_OPENAI__TIMEOUT=60
# B2N_OPENAI__LOG_LEVEL=INFO

# --- Container metadata  ---
B2N_CONTAINER__CREATE_CONTAINER=True # default : true
B2N_CONTAINER__CONTAINER_TYPE="YOUR_CONTAINER_TYPE"
B2N_CONTAINER__CONTAINER_ID="YOUR_CONTAINER_ID"
B2N_CONTAINER__CONTAINER_NAME="YOUR_CONTAINER_NAME"

# --- Runtime (optional) ---
# B2N_RUNTIME__LOG_LEVEL=INFO                  # global log level
# B2N_RUNTIME__PARALLELISM=1                   # worker parallelism
# B2N_RUNTIME__BATCH_SIZE=64                   # embedding batch size
# B2N_RUNTIME__CACHE_DIR=/tmp/bpmn2neo_cache   # local cache dir if you need one
# B2N_RUNTIME__DRY_RUN=false                   # true: do not write to DB
# B2N_RUNTIME__FAIL_FAST=false                 # true: stop on first error

### Python API (recommended)
```python
from bpmn2neo import load_bpmn_to_neo4j, create_node_embeddings, load_and_embed
from bpmn2neo.settings import Settings

s = Settings()  # reads .env via pydantic-settings

# 1) Load only
model_keys = load_bpmn_to_neo4j(
    bpmn_path="./data/bpmn/Order Process for Pizza.bpmn", #Your Path
    settings=s,
)
print("model_keys:", model_keys)

# 2) Embedding only
create_node_embeddings(model_key=model_keys[0], settings=s)

# 3) Pipeline (load + embed)
result = load_and_embed(
    bpmn_path="./data/bpmn/Order Process for Pizza.bpmn", #Your Path
    settings=s,
)
print("final model_key:", result["model_key"])
```



### Requirements
- **Python** 3.10+
- **Neo4j** 5.x (Bolt reachable).  
  Ensure the user has `MATCH/CREATE/MERGE/SET/DELETE` and index/constraint privileges.
- **OpenAI** API key (for embeddings & text generation).

---

## 4) Project Structure

```
bpmn2neo/
├─ docs/
│  ├─ Neo4j Graph Schema Guide.md
│  └─ BPMN KG Context Embedding Guide.md
├─ src/bpmn2neo/
│  ├─ config/
│  │  ├─ exceptions.py        # Domain exceptions (Config/Neo4j/etc.)
│  │  ├─ logger.py            # Structured logger (JSON-friendly)
│  │  └─ neo4j_repo.py        # Thin Neo4j driver wrapper + helpers
│  ├─ embedder/
│  │  ├─ builder.py           # Build signals/texts from Reader context
│  │  ├─ context_writer.py    # Ask LLM to craft node explanations
│  │  ├─ embedder.py          # Vectorize and persist embeddings
│  │  ├─ orchestrator.py      # Orchestrates Reader→Builder→Writer→Embedder
│  │  └─ reader.py            # Read graph context per node for embedding
│  ├─ loader/
│  │  ├─ loader.py            # High-level load flow (schema ensure + write)
│  │  └─ parser.py            # BPMN XML → nodes/relationships
│  ├─ settings.py             # Pydantic-based config (.env)
│  ├─ cli.py                  # Optional CLI entry
│  └─ __init__.py             # Public API: load, embed, pipeline
└─ pyproject.toml
```
---

## 5) License

Licensed under **Apache License 2.0**.  
See [LICENSE](LICENSE).  
Notes:
- No trademark rights are granted.  
- Contributions (if any) are accepted under the same license.  
- Patent grant/termination follows Apache-2.0 terms.

---

## Configuration & Tips

- **Settings resolution**: `Settings()` reads environment variables (and `.env`) using `pydantic-settings`. Keys are prefixed (e.g., `B2N_NEO4J__URI`).
- **Model key**: if not provided, the loader derives it from the BPMN filename (stem).
- **Security**: you may use keyring aliases instead of plain secrets (see comments in `settings.py`).

---

## Troubleshooting

- **Cannot connect to Neo4j**  
  - Verify `B2N_NEO4J__URI` (`neo4j://host:7687` or `neo4j+s://...` for Aura).
  - Check firewall, DB auth, and database name.

- **OpenAI errors**  
  - Ensure `B2N_OPENAI__API_KEY` set; consider rate limits and retries.
---



