# BPMN Embedding Rules Documentation

## Overview

This document describes the embedding rules and strategies used to generate vector representations of BPMN elements in the bpmn2neo project. The embedding system uses OpenAI's text-embedding models to create semantic representations of BPMN nodes at different hierarchical levels.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Embedding Pipeline](#embedding-pipeline)
- [Node Levels and Context Building](#node-levels-and-context-building)
- [LLM-Based Context Generation](#llm-based-context-generation)
- [Embedding Rules by Level](#embedding-rules-by-level)
- [Vector Storage](#vector-storage)
- [Error Handling and Fallbacks](#error-handling-and-fallbacks)

---

## Architecture Overview

The embedding system consists of five main components:

1. **Embedder** (`embedder.py`): OpenAI embedding client wrapper
2. **Reader** (`reader.py`): Reads BPMN graph data from Neo4j
3. **Builder** (`builder.py`): Builds hierarchical context texts
4. **ContextWriter** (`context_writer.py`): LLM-based context generation with fallbacks
5. **Orchestrator** (`orchestrator.py`): Orchestrates the end-to-end pipeline

### Processing Flow

```
Neo4j Graph → Reader → Builder → ContextWriter → Embedder → Neo4j (embeddings stored)
              ↑                                              ↓
              └──────────── Orchestrator ────────────────────┘
```

---

## Embedding Pipeline

The orchestrator implements a **bottom-up hierarchical pipeline**:

1. **FlowNode Level**: Embed individual BPMN flow nodes (Tasks, Events, Gateways)
2. **Lane Level**: Embed lanes with aggregated FlowNode contexts
3. **Process Level**: Embed processes with aggregated Lane contexts
4. **Participant Level**: Embed participants with aggregated Process contexts
5. **Model Level**: Embed the entire BPMN model with aggregated Participant contexts

### Pipeline Characteristics

- **Sequential Processing**: Each level must complete before the next begins
- **Context Aggregation**: Parent nodes aggregate context from child nodes
- **Error Resilience**: Failed embeddings are logged but don't stop the pipeline
- **Multi-tenant Support**: Uses `containerType`, `containerId`, and `modelKey` for data isolation

---

## Node Levels and Context Building

### Level 1: FlowNode (Task, Event, Gateway)

**Target Nodes**: All nodes with `:FlowNode` label

**Context Components**:
- Node name and type
- Documentation/description
- Incoming/outgoing sequence flows
- Parent lane information
- Parent process information

**Context Building Strategy**:
```
FlowNode.name (FlowNode.type)
Documentation: [text]
Incoming: [source nodes]
Outgoing: [target nodes]
Lane: [lane name]
Process: [process name]
```

**No LLM Generation**: FlowNodes use direct property concatenation without LLM enhancement.

---

### Level 2: Lane

**Target Nodes**: All nodes with `:Lane` label

**Context Components**:
- Lane name
- All FlowNodes within the lane
- FlowNode contexts (aggregated)
- Parent process information

**LLM Enhancement**: Uses GPT-4o-mini to generate structured lane descriptions

**Prompt Template**:
```
Based on the FlowNode information below, write a concise summary of this lane's purpose and workflow.

Lane Name: {lane_name}

FlowNodes:
{flownode_contexts}

Write a 2-3 sentence summary focusing on:
1. What this lane is responsible for
2. Key activities or processes it handles
```

**Fallback Strategy**:
1. Try LLM generation
2. If LLM fails → Use simple concatenation
3. If no FlowNodes → Use lane name only

---

### Level 3: Process

**Target Nodes**: All nodes with `:Process` label

**Context Components**:
- Process name
- All Lanes within the process
- Lane contexts (aggregated)
- Parent participant information

**LLM Enhancement**: Uses GPT-4o-mini to generate structured process descriptions

**Prompt Template**:
```
Based on the Lane information below, write a comprehensive summary of this process.

Process Name: {process_name}

Lanes:
{lane_contexts}

Write a 3-4 sentence summary focusing on:
1. The overall purpose of this process
2. How different lanes collaborate
3. Key workflow stages or phases
```

**Fallback Strategy**:
1. Try LLM generation
2. If LLM fails → Use simple concatenation
3. If no Lanes → Use process name only

---

### Level 4: Participant

**Target Nodes**: All nodes with `:Participant` label

**Context Components**:
- Participant name
- All Processes within the participant
- Process contexts (aggregated)
- Parent model information

**LLM Enhancement**: Uses GPT-4o-mini to generate structured participant descriptions

**Prompt Template**:
```
Based on the Process information below, write a comprehensive summary of this participant's role.

Participant Name: {participant_name}

Processes:
{process_contexts}

Write a 3-4 sentence summary focusing on:
1. The participant's overall role and responsibilities
2. How different processes work together
3. Key business functions or capabilities
```

**Fallback Strategy**:
1. Try LLM generation
2. If LLM fails → Use simple concatenation
3. If no Processes → Use participant name only

---

### Level 5: Model

**Target Nodes**: All nodes with `:Model` label

**Context Components**:
- Model name
- All Participants within the model
- Participant contexts (aggregated)
- Overall model metadata

**LLM Enhancement**: Uses GPT-4o-mini to generate comprehensive model descriptions

**Prompt Template**:
```
Based on the Participant information below, write a comprehensive summary of this BPMN model.

Model Name: {model_name}

Participants:
{participant_contexts}

Write a 4-5 sentence summary focusing on:
1. The overall business process or system represented
2. How different participants interact
3. Key business objectives or outcomes
4. Main workflow stages or phases
```

**Fallback Strategy**:
1. Try LLM generation
2. If LLM fails → Use simple concatenation
3. If no Participants → Use model name only

---

## LLM-Based Context Generation

### Configuration

- **Model**: `gpt-4o-mini`
- **Temperature**: 0.3 (consistent, focused output)
- **Max Tokens**: 500
- **System Role**: "You are an assistant that summarizes BPMN elements concisely."

### Context Writer Features

1. **Structured Prompts**: Each level has tailored prompts for optimal context generation
2. **Retry Logic**: Retries LLM calls on temporary failures
3. **Fallback Strategy**: Gracefully degrades to simple concatenation on LLM failure
4. **Token Management**: Limits output tokens to prevent excessive context length
5. **Error Logging**: Comprehensive error logging for debugging

### Example LLM Output (Lane Level)

**Input**:
```
Lane Name: Customer Service
FlowNodes:
- Receive Customer Request (StartEvent)
- Validate Request (Task)
- Process Request (Task)
- Send Response (Task)
- Request Completed (EndEvent)
```

**LLM Output**:
```
The Customer Service lane is responsible for handling incoming customer requests through a systematic workflow.
It validates incoming requests, processes them according to business rules, and sends appropriate responses.
This lane ensures all customer inquiries are handled efficiently from receipt to completion.
```

---

## Embedding Rules by Level

### General Rules (All Levels)

1. **Model**: OpenAI `text-embedding-3-small` (1536 dimensions)
2. **Input**: UTF-8 encoded text context
3. **Output**: Float vector array (1536 dimensions)
4. **Storage**: Stored in Neo4j node property `embedding`
5. **Multi-tenancy**: Each embedding respects `modelKey` isolation

### FlowNode Embedding Rules

- **Context Source**: Direct property aggregation (no LLM)
- **Priority Properties**: `name`, `documentation`, sequence flows, parent info
- **Empty Handling**: Use node ID if name is missing
- **Special Cases**:
  - Events: Include event type and trigger information
  - Gateways: Include gateway type and branching logic
  - Tasks: Include task type and assignments

### Lane Embedding Rules

- **Context Source**: LLM-generated summary + FlowNode contexts
- **Aggregation**: All child FlowNodes within lane boundaries
- **Context Length**: Limited to prevent token overflow
- **Weighting**: Equal weight to all FlowNodes

### Process Embedding Rules

- **Context Source**: LLM-generated summary + Lane contexts
- **Aggregation**: All child Lanes within process boundaries
- **Cross-Reference**: Includes message flows to other processes
- **Collaboration**: Captures inter-process communication patterns

### Participant Embedding Rules

- **Context Source**: LLM-generated summary + Process contexts
- **Aggregation**: All child Processes within participant boundaries
- **Role Clarity**: Emphasizes participant's organizational role
- **Business Context**: Captures business function representation

### Model Embedding Rules

- **Context Source**: LLM-generated summary + Participant contexts
- **Aggregation**: All child Participants in the model
- **Holistic View**: Represents entire business process ecosystem
- **Strategic Focus**: Emphasizes overall business objectives

---

## Vector Storage

### Neo4j Property Schema

Each embedded node has the following properties:

```cypher
// Example FlowNode with embedding
(:Task {
  id: "task_001",
  name: "Process Invoice",
  modelKey: "invoice_model_v1",
  embedding: [0.123, -0.456, 0.789, ...],  // 1536 float values
  contextText: "Process Invoice (Task)..."  // Original context text
})
```

### Property Definitions

| Property | Type | Description |
|----------|------|-------------|
| `embedding` | Float[] | 1536-dimensional vector from OpenAI |
| `contextText` | String | Original text used for embedding generation |
| `modelKey` | String | Multi-tenant isolation key |
| `embeddedAt` | DateTime | Timestamp of embedding generation |

### Indexing Strategy

- **Vector Index**: Not currently implemented (future enhancement)
- **Property Index**: Index on `modelKey` for multi-tenant queries
- **Composite Index**: Index on `(id, modelKey)` for uniqueness

---

## Error Handling and Fallbacks

### Embedding Failures

1. **Network Errors**: Retry with exponential backoff
2. **API Rate Limits**: Wait and retry with delay
3. **Invalid Input**: Log error, skip node, continue pipeline
4. **Token Limit Exceeded**: Truncate context and retry

### LLM Context Generation Failures

1. **Primary Strategy**: Use LLM for rich context
2. **Fallback 1**: Simple concatenation of child contexts
3. **Fallback 2**: Use node name only
4. **Fallback 3**: Use node ID if name is missing

### Pipeline Robustness

- **Level Isolation**: Failure at one level doesn't stop subsequent levels
- **Node Isolation**: Failure on one node doesn't stop other nodes
- **Logging**: All errors logged with context for debugging
- **Metrics**: Count of successful/failed embeddings per level

### Example Error Handling

```python
try:
    embedding = embedder.embed(context_text)
    node['embedding'] = embedding
    success_count += 1
except Exception as e:
    logger.error(f"Failed to embed {node['id']}: {e}")
    failed_count += 1
    continue  # Skip to next node
```

---

## Usage Example

### Running the Embedding Pipeline

```python
from bpmn2neo.embedder.orchestrator import EmbeddingOrchestrator

# Initialize orchestrator
orchestrator = EmbeddingOrchestrator(
    neo4j_client=neo4j_client,
    openai_api_key=openai_api_key,
    container_type="project",
    container_id="proj_123",
    model_key="invoice_model_v1"
)

# Run full pipeline (all levels)
orchestrator.run_all()
```

### Querying Embeddings

```cypher
// Find similar tasks using vector similarity (conceptual)
MATCH (t:Task {modelKey: 'invoice_model_v1'})
WHERE t.embedding IS NOT NULL
RETURN t.id, t.name, t.contextText
```

---

## Performance Considerations

### Optimization Strategies

1. **Batch Processing**: Process multiple nodes in parallel where possible
2. **Context Caching**: Cache LLM-generated contexts for reuse
3. **Incremental Updates**: Only re-embed changed nodes
4. **Level Skipping**: Skip levels with no new data

### Typical Processing Times

- **FlowNode**: ~1-2 seconds per node (no LLM)
- **Lane**: ~3-5 seconds per lane (LLM + children)
- **Process**: ~5-10 seconds per process (LLM + children)
- **Participant**: ~10-15 seconds per participant (LLM + children)
- **Model**: ~15-30 seconds per model (LLM + all children)

### Cost Considerations

- **Embedding API**: ~$0.0001 per 1K tokens (text-embedding-3-small)
- **LLM API**: ~$0.0015 per 1K tokens (gpt-4o-mini input/output)
- **Typical Model**: ~$1-5 per complete embedding pipeline for medium-sized BPMN

---

## Design Principles

1. **Hierarchical Context**: Build context from bottom-up for rich semantic representation
2. **LLM Enhancement**: Use LLM to create human-readable, contextual summaries
3. **Graceful Degradation**: Always have fallback strategies for robustness
4. **Multi-tenancy**: Strict isolation using modelKey for data security
5. **Observability**: Comprehensive logging for debugging and monitoring
6. **Extensibility**: Easy to add new node types or embedding strategies

---

## Future Enhancements

1. **Vector Similarity Search**: Implement Neo4j vector index for semantic search
2. **Incremental Embedding**: Only re-embed changed subgraphs
3. **Custom Embedding Models**: Support for domain-specific embedding models
4. **Embedding Versioning**: Track and compare different embedding versions
5. **Metadata Enrichment**: Include more BPMN metadata in context generation
6. **Performance Optimization**: Parallel processing and caching strategies

---

## References

- [OpenAI Embeddings Documentation](https://platform.openai.com/docs/guides/embeddings)
- [Neo4j Graph Data Science](https://neo4j.com/docs/graph-data-science/current/)
- [BPMN 2.0 Specification](https://www.omg.org/spec/BPMN/2.0/)
