# BPMN KG Context Embedding Guide 
*(Process / Participant / Lane / FlowNode / Embedded / Group)*

This document summarizes **what** we collect for each node type, **from where** (Neo4j relations), and **how** those pieces are used to build texts for semantic embeddings.

---

## Legend (Nodes & Relations)
- **Nodes**: `Process`, `Participant`, `Lane`, `Activity`, `Event`, `Gateway`, `Data`, `Group`
- **Key Relations**  
  - Structure: `(:Participant)-[:EXECUTES]->(:Process)`, `(:Process)-[:HAS_LANE]->(:Lane)`, `(:Lane)-[:OWNS_NODE]->(n)`, `(:Process)-[:OWNS_NODE]->(n)`  
  - Flow: `(:FlowNode)-[:SEQUENCE_FLOW]->(:FlowNode)`, `(:FlowNode)-[:MESSAGE_FLOW]->(:FlowNode)`  
  - Embedded: `(:Activity)-[:CONTAINS]->(inner)`, `(:Activity)-[:CONTAINS_PROCESS]->(entry)`, `(:Activity {activityType:'CallActivity'})-[:CALLS_PROCESS]->(:Process)`  
  - Data I/O: `(n)-[:READS_FROM]->(:Data)`, `(n)-[:WRITES_TO]->(:Data)`  
  - Grouping: `(:Group)-[:GROUPS]->(member)`  
  - Special: `(:Event {position:'Boundary'})-[:HAS_BOUNDARY_EVENT]->(:Activity)`, `(a)-[:COMPENSATES]->(b)`, `(a)-[:LINK_TO]->(b)`, `(b)-[:LINK_FROM]->(a)`
- **Scope nodes** (per process): *lane-owned* `HAS_LANE/OWNS_NODE` ∪ *process-owned* `OWNS_NODE`  
  Process-owned nodes behave as lane **`Unassigned`** for handoff detection.

---

## 1) Participant Context  →  *scope_text*, *interactions_text*, *(catalog_text)*

| Section | We collect | Cypher anchors | Notes |
|---|---|---|---|
| **core** | `participantName`, `processName` | `(pa:Participant)-[:EXECUTES]->(pr:Process)` | One process per participant is typical (BPMN collaboration). |
| **reps** | `lanes`, `rep_tasks`(Activities), `rep_events`(Events), `data_reads`, `data_writes` | Scope nodes via `(pr)-[:HAS_LANE]->(:Lane)-[:OWNS_NODE]->(n)` ∪ `(pr)-[:OWNS_NODE]->(n)` + `READS_FROM/WRITES_TO` | Representative names are deduped & truncated for readability. |
| **intra_pool** | `handoffs` (lane-change `SEQUENCE_FLOW`) | `a-[:SEQUENCE_FLOW]->b` with `lane(a) ≠ lane(b)` or `Unassigned` crossings | Same-lane flows are **excluded** (covered at FlowNode level). |
| **cross_pool** | OUT/IN message exchanges | `MESSAGE_FLOW` where `counterparty(process) ≠ pr` | Aggregated by counterparty `Participant` (topics included when available). |

**Builder output**:  
- *scope_text* = role + lanes + representative activities/events + typical inputs/outputs  
- *interactions_text* = cross-pool OUT/IN + intra-pool handoffs (lane-change only)  
- *(catalog_text)* = compact lane/step index (optional)

---

## 2) Process Context  →  *scope_text*, *decomposition_text*

| Section | We collect | Cypher anchors | Notes |
|---|---|---|---|
| **core** | `processName`, `modelKey`, `participants` | `(pa)-[:EXECUTES]->(pr)` |  |
| **structure** | `lanes`, `start_events`, `end_events`, `embedded{name,kind}`, `groups` | Lanes: `(pr)-[:HAS_LANE]->(l)`; Events: scope filter `{position:'Start'|'End'}`; Embedded: `Activity.activityType ∈ {SubProcess,Transaction,CallActivity,AdHocSubProcess}`; Groups: `(g)-[:GROUPS]->(member in process scope)` |  |
| **flow_counts** | `activities`, `events`, `gateways`, `sequence_flows`, `message_flows`, `lanes`, `groups` | Counts over scope nodes/edges | Size signal for overview. |
| **handoffs** | lane-change pairs with counts + examples | `SEQUENCE_FLOW` with `lane(a) ≠ lane(b)`; collect examples `(flowName, condition)` |  |
| **cross_pool_summary** | totals only: `OUT`, `IN` | `MESSAGE_FLOW` where counterparty process ≠ `pr` | **Numbers only**; Process does **not** store interactions text. |

**Builder output**:  
- *scope_text* = participants, lane count, entry/exit points, coarse size, cross-pool counts  
- *decomposition_text* = lanes, embedded units(kind), groups, lane-change summary (+few examples)

---

## 3) Lane Context  →  *scope_text*, *interactions_text*

| Section | We collect | Cypher anchors | Notes |
|---|---|---|---|
| **core** | `laneName`, `processName`, `participantName` | `(pr)-[:HAS_LANE]->(l)`, `(pa)-[:EXECUTES]->(pr)` |  |
| **reps** | `rep_tasks`, `rep_events`, `data_reads`, `data_writes` | `(l)-[:OWNS_NODE]->(n)` + `READS_FROM/WRITES_TO` |  |
| **handoffs** | `to_other_lanes`, `from_other_lanes` | `SEQUENCE_FLOW` within same `pr` where lane changes (incl. `Unassigned`) | OUT/IN phrasing in builder. |
| **cross_pool** | OUT/IN message exchanges tied to this lane | `(l)-[:OWNS_NODE]->(s) -[:MESSAGE_FLOW]-> (t)` (and reverse) + counterparty process derivation | Aggregated by counterparty `Participant` with topics. |

**Builder output**:  
- *scope_text* = lane placement + representative steps + I/O  
- *interactions_text* = handoffs OUT/IN (lane-change) + cross-pool OUT/IN

---

## 4) FlowNode Context (Activity/Event/Gateway)  →  *function_text*, *neighbor_text*

| Section | We collect | Cypher anchors | Notes |
|---|---|---|---|
| **core** | `name`, `type`(Activity/Event/Gateway), `activityType`(for Activity), `eventPosition`/`eventDetailType`(for Event), plus `roleName`(lane), `processName`, `participantName` | Ownership via `HAS_LANE/OWNS_NODE` or `OWNS_NODE`; participant via `EXECUTES` | `activityType` distinguishes SubProcess/CallActivity/User/etc. |
| **data_io** | `reads`, `writes` | `READS_FROM/WRITES_TO` |  |
| **special** | `msg_out`, `msg_in`, `link_to`, `link_from`, `boundary_on`, `compensates`, `calls` | `MESSAGE_FLOW`, `LINK_TO/LINK_FROM`, `HAS_BOUNDARY_EVENT`, `COMPENSATES`, `CALLS_PROCESS` | Messaging counts are summarized in *function_text*; message peers go to *neighbor_text*. |
| **degree** | `preds` (name/cond/default), `succs` (name/cond/default), `indegree`, `outdegree` | `SEQUENCE_FLOW` in/out with `condition` and `isDefault` |  |

**Builder output**:  
- *function_text* = identity (type/subtype) + context + I/O + special relations (+called process)  
- *neighbor_text* = **all 1-hop**: Seq IN/OUT (cond/default) + Msg IN/OUT (counterpart & topics)

---

## 5) Embedded Activity Context (SubProcess / Transaction / CallActivity / AdHoc)  →  *scope_text*, *interactions_text*, *(catalog_text)*

| Section | We collect | Cypher anchors | Notes |
|---|---|---|---|
| **core** | `name`, `kind`(`activityType`), `participantName`, `processName` | Owning process via lane-owned or process-owned membership |  |
| **markers** | `loop`, `multi_instance`, `adhoc`, `transaction`, `event_subprocess` | Activity properties (`hasStandardLoop`, `multiInstanceLoopCardinality`/`isSequential`, type flags) | Best-effort mapping from parser properties. |
| **internals** | `top_steps`, `data_reads`, `data_writes`, `called_process_summary`(CallActivity) | `CONTAINS` / `CONTAINS_PROCESS` (+ I/O), `CALLS_PROCESS` | Entry/exit conditions may be empty if not modeled. |
| **cross_pool** | OUT/IN messages of internal nodes | Internal nodes’ `MESSAGE_FLOW` where counterparty process ≠ owner | Aggregated by counterparty participant. |
| **handoffs** | Internal lane-change pairs | Internal `SEQUENCE_FLOW` with lane changes (incl. `Unassigned`) | OUT/IN phrasing in builder. |

**Builder output**:  
- *scope_text* = identity(kind) + context + markers + top steps + internal I/O + (called process)  
- *interactions_text* = cross-pool OUT/IN + internal handoffs OUT/IN  
- *(catalog_text)* = Kind/Markers/Steps (compact)

---

## 6) Group Context  →  *scope_text*

| Section | We collect | Cypher anchors | Notes |
|---|---|---|---|
| **core** | `groupName`, `processes`, `participants` | From `(g)-[:GROUPS]->(m)` + ownership inference to `(pr)` and `(pa)-[:EXECUTES]->(pr)` |  |
| **members** | `activities`, `events`, `gateways`, `data`, `lanes` | Typed filtering over grouped members | Group has *no execution semantics* → interactions not collected. |

**Builder output**:  
- *scope_text* = thematic purpose (implicit) + related processes/participants + composition summary

---

## 7) Interaction Semantics (Handoff & Cross-pool)

- **Intra-pool handoff**: a `SEQUENCE_FLOW` where `lane(source) ≠ lane(target)` inside the same process.  
  Process-owned nodes are treated as lane **`Unassigned`** so `lane↔Unassigned` also counts.
- **Cross-pool interaction**: a `MESSAGE_FLOW` where the counterparty belongs to a **different** process.  
  Counterparty display name prefers `Participant.name`, then `Process.name` as fallback.

---

## 8) Text → Embedding Property Map (by Node Type)

| Node Type | Text Fields | Embedding Properties |
|---|---|---|
| Participant | `scope_text`, `interactions_text`, *(opt) `catalog_text`* | `emb_scope`, `emb_interactions`, *(opt) `emb_catalog`* |
| Process | `scope_text`, `decomposition_text` | `emb_scope`, `emb_decomposition` |
| Lane | `scope_text`, `interactions_text` | `emb_scope`, `emb_interactions` |
| FlowNode | `function_text`, `neighbor_text` | `emb_function`, `emb_neighbor` |
| Embedded Activity | `scope_text`, `interactions_text`, *(opt) `catalog_text`* | `emb_scope`, `emb_interactions`, *(opt) `emb_catalog`* |
| Group | `scope_text` | `emb_scope` |

---