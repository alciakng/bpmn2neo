# Neo4j Graph Schema Guide

> This document defines **the hierarchical structure** and comprehensively enumerates **all roles, nodes, relationships, directions, and key properties** of **Neo4j nodes created from BPMN graph conversion.**

## Part 1. Node

> Node labels are set exactly as listed below.

1. **Container**
   - Purpose: Root container for a set of BPMN models (optional).
   - Example props: `containerType='BPMN_MODEL_COLLECTION'`, `description`, `modelKey`.

2. **BPMNModel**
   - Purpose: Collaboration/file-level model root.
   - Props: `modelKey` (XML file key). If there is no Collaboration, may be `<filename>_model`.

3. **Participant**
   - Represents a BPMN **Pool**.
   - Props: `processRef` (when present), `name`, `modelKey`.

4. **Process**
   - BPMN `process` element.
   - Props (only set when present): `processType`, `isClosed`, `isExecutable`, `definitionalCollaborationRef`, `name`, `modelKey`.

5. **Lane**
   - BPMN `lane` element (a partition inside Process or SubProcess).
   - Props: `partitionElementRef` (when present), `name`, `modelKey`.
   - **Note:** In BPMN, lanes belong to a `LaneSet` within a `Process`/`SubProcess`.

6. **Activity**
   - Includes all Tasks and specialized activities: `User`, `Service`, `Send`, `Receive`, `Manual`, `BusinessRule`, `Script`, base `Task`, `CallActivity`, `SubProcess`, `Transaction`, `AdHocSubProcess`.
   - Props (subset): `activityType` (`User|Service|...|Task|Call|SubProcess|Transaction|AdHocSubProcess`), loop/MI flags (`hasStandardLoop`, `multiInstanceLoopCardinality`, `isSequential` ...), `calledElement` (for CallActivity), `name`, `modelKey`.

7. **Event**
   - Start / End / Intermediate / Boundary.
   - Props (subset): `position` (Start|End|Intermediate|Boundary), `detailType` (Timer|Message|Signal|Escalation|Error|Conditional|Link|Compensate|Terminate|Cancel ...), `isInterrupting` (Start/Boundary), `parallelMultiple` (Start), `timerTimeDate|timerCycle|timerDuration`, `messageRef`, `signalRef`, `errorRef`, `linkName`, `compensateActivityRef`, `waitForCompletion`, `name`, `modelKey`.

8. **Gateway**
   - Parallel / Exclusive / Inclusive / Complex / EventBased.
   - Props: `gatewayDirection` (unless Unspecified), `default` (when present), `name`, `modelKey`.

9. **Data**
   - Consolidates **DataObjectReference** and **DataStoreReference**.
   - Props: `dataType` (`ObjectReference|Store`), `itemSubjectRef`, `isCollection`, `capacity` (store), `name`, `modelKey`.

10. **TextAnnotation**
    - Text annotations.
    - Props: `text` (when present), `modelKey`.

11. **Group**
    - BPMN Grouping artifact.
    - Props: `categoryValue` (mapped CategoryValue), `tags?`, `notes?`, `name`, `modelKey`.
    - *Execution semantics:* None. Acts as a thematic cluster.


---

## Part 2. Relationship Types (Direction, Meaning, Props)

> All relationships also include `modelKey` unless noted.

### 2.1 Model containment
- **HAS_MODEL** : `Container → BPMNModel`
- **HAS_PARTICIPANT** : `BPMNModel → Participant`

### 2.2 Pool → Process
- **EXECUTES** : `Participant → Process`  
  - Source: `participant@processRef`

### 2.3 Process → Lane (NEW, recommended)
- **HAS_LANE** : `Process → Lane`  
  - Added to reflect BPMN norm (LaneSet under Process/SubProcess).  
  - *Optional extension:* also allow `SubProcess → Lane` where lanes exist inside a SubProcess.

> **Optional denormalized link (for query convenience):**
> - **HAS_LANE** : `Participant → Lane`  
>   - *Derived* via `Participant → EXECUTES → Process → HAS_LANE → Lane`. Rebuilt on each ingestion (delete→insert policy).

### 2.4 Ownership of FlowNodes (partitioning)
- **OWNS_NODE** :
  - `Lane → (Activity|Event|Gateway)` when lane assignment exists.
  - `Process → (Activity|Event|Gateway)` for nodes without lane assignment.

### 2.5 Control/Messaging flows
- **SEQUENCE_FLOW** : `(Activity|Event|Gateway) → (Activity|Event|Gateway)`  
  - Props: `flowType='SequenceFlow'`, `flowName` (if any), `isDefault` (true if default flow), `condition` (expression), `isImmediate` (false stored only).
- **MESSAGE_FLOW** : `(Activity|Event|Gateway) → (Activity|Event|Gateway)`  
  - Typically cross-pool.
  - Props: `flowType='MessageFlow'`, `flowName?`, `messageRef?`.

### 2.6 Subprocess containment
- **CONTAINS_PROCESS** : `SubProcess → (first entry FlowNode)`  
  - Props: `connectionType='subprocess_entry'`, `isFirstNode=true`.
- **CONTAINS** : `SubProcess → (internal elements)`  
  - Props: `connectionType='containment'`.

### 2.7 Call Activity
- **CALLS_PROCESS** : `CallActivity → Process`  
  - Props: `calledElement` mapped when available.

### 2.8 Event-specific relations
- **HAS_BOUNDARY_EVENT** : `Activity → (Boundary)Event`
- **COMPENSATES** : `(Compensation)Event → Activity`  
  - Props: `compensationType='specific'|...` when present.
- **LINK_TO** : `Link(throw)Event → Link(catch)Event`  
  - Props: `linkName`.

### 2.9 Data associations
- **READS_FROM** : `Activity → Data`  (dataInputAssociation)  
  - Props: `associationType='input'`.
- **WRITES_TO** : `Activity → Data`  (dataOutputAssociation)  
  - Props: `associationType='output'`.

### 2.10 Documentation artifacts
- **ANNOTATES** : `TextAnnotation → (Activity|Event|Gateway|Process|Participant|Lane|Data)`  
  - Direction normalized so **TextAnnotation is always the source**.
- **GROUPS** : `Group → (Activity|Event|Gateway|Data|SubProcess|CallActivity|...)`  
  - Direction normalized so **Group is always the source**.


---

## Part 3. Hierarchy & Scope Rules

- **Canonical hierarchy**
  1. `Container → BPMNModel`
  2. `BPMNModel → Participant`
  3. `Participant → EXECUTES → Process`
  4. `Process → HAS_LANE → Lane` *(added)*
  5. `Lane → OWNS_NODE → (Activity|Event|Gateway)`; or `Process → OWNS_NODE → FlowNode` if no lane.
- **FlowNode set:** `type ∈ {Activity, Event, Gateway}`.
- **Optional:** `SubProcess → HAS_LANE → Lane` if lanes exist inside the embedded subprocess.


---

## Part 4. Interaction Modeling (Search/Embedding oriented)

- **Cross-pool interactions:** via **MESSAGE_FLOW** edges among FlowNodes owned by different Participants (derived through EXECUTES + HAS_LANE/OWNS_NODE).
- **Intra-pool handoff:** via **SEQUENCE_FLOW** where the **Lane of source ≠ Lane of target** (within the same Process).
- **Boundary/compensate/link:** special event interactions: `HAS_BOUNDARY_EVENT`, `COMPENSATES`, `LINK_TO`.
- **Data I/O:** `READS_FROM`, `WRITES_TO` from Activities to Data nodes.


---

## Part 5. Key Properties by Node Type (non-exhaustive)

- **Participant**: `id`, `name`, `processRef?`, `modelKey`
- **Process**: `id`, `name?`, `processType?`, `isClosed?`, `isExecutable?`, `definitionalCollaborationRef?`, `modelKey`
- **Lane**: `id`, `name`, `partitionElementRef?`, `modelKey`
- **Activity**: `id`, `name?`, `activityType`, loop/MI/ad-hoc flags, `calledElement?` (CallActivity), `modelKey`
- **Event**: `id`, `name?`, `position`, `detailType`, specific definition props (timer/message/signal/error/conditional/link/compensate/terminate/cancel), `isInterrupting?`, `parallelMultiple?`, `modelKey`
- **Gateway**: `id`, `name?`, `gatewayDirection?`, `default?`, `modelKey`
- **Data**: `id`, `name?`, `dataType`, `itemSubjectRef?`, `isCollection?`, `capacity?` (store), `modelKey`
- **TextAnnotation**: `id`, `text?`, `modelKey`
- **Group**: `id`, `name?`, `categoryValue?`, `tags?`, `notes?`, `modelKey`

**Relation properties (selected):**
- **SEQUENCE_FLOW**: `flowType='SequenceFlow'`, `flowName?`, `isDefault?`, `condition?`, `isImmediate=false?`, `modelKey`
- **MESSAGE_FLOW**: `flowType='MessageFlow'`, `flowName?`, `messageRef?`, `modelKey`
- **CONTAINS_PROCESS**: `connectionType='subprocess_entry'`, `isFirstNode=true`, `modelKey`
- **CONTAINS**: `connectionType='containment'`, `modelKey`
- **READS_FROM / WRITES_TO**: `associationType='input'|'output'`, `modelKey`
- **LINK_TO**: `linkName`, `modelKey`
- **COMPENSATES**: `compensationType?`, `modelKey`
- **ANNOTATES / GROUPS**: `modelKey`


---

## Part 6. Query Snippets (Cypher)

> Replace `$modelKey` or `$processKey` accordingly.

### 6.1 Resolve a Process by key
```cypher
MATCH (pr:Process)
WHERE pr.key = $processKey OR pr.bpmn_key = $processKey OR pr.processKey = $processKey OR pr.name = $processKey
RETURN pr, id(pr) AS pid;
```

### 6.2 List Participants for a Process (via EXECUTES)
```cypher
MATCH (pa:Participant)-[:EXECUTES]->(pr:Process)
WHERE pr.modelKey = $modelKey
RETURN pa.name, id(pa);
```

### 6.3 Lanes of a Process (HAS_LANE)
```cypher
MATCH (pr:Process)-[:HAS_LANE]->(l:Lane)
WHERE pr.modelKey = $modelKey
RETURN l.name, id(l);
```

### 6.4 FlowNodes owned by a Lane or by a Process (OWNS_NODE)
```cypher
// Lane-owned nodes
MATCH (l:Lane)-[:OWNS_NODE]->(n)
WHERE l.modelKey = $modelKey AND n.type IN ['Activity','Event','Gateway']
RETURN id(n) AS nid, n.name, n.type;

// Nodes without lane (owned by process)
MATCH (pr:Process)-[:OWNS_NODE]->(n)
WHERE pr.modelKey = $modelKey AND n.type IN ['Activity','Event','Gateway']
RETURN id(n) AS nid, n.name, n.type;
```

### 6.5 Cross-pool MessageFlows
```cypher
MATCH (a)-[:OWNS_NODE]->(s)-[mf:MESSAGE_FLOW]->(t)<-[:OWNS_NODE]-(b)
WHERE s.type IN ['Activity','Event','Gateway'] AND t.type IN ['Activity','Event','Gateway']
WITH s, t, mf, a, b
// derive participants of source and target
OPTIONAL MATCH (pa:Participant)-[:EXECUTES]->(:Process)-[:HAS_LANE]->(a)
OPTIONAL MATCH (pb:Participant)-[:EXECUTES]->(:Process)-[:HAS_LANE]->(b)
RETURN s.name AS fromNode, pa.name AS fromParticipant, t.name AS toNode, pb.name AS toParticipant, mf.messageRef AS messageRef;
```

### 6.6 Intra-pool handoffs: lane change across SequenceFlow
```cypher
MATCH (l1:Lane)-[:OWNS_NODE]->(n1)-[sf:SEQUENCE_FLOW]->(n2)<-[:OWNS_NODE]-(l2:Lane)
WHERE n1.type IN ['Activity','Event','Gateway'] AND n2.type IN ['Activity','Event','Gateway'] AND l1 <> l2
RETURN l1.name AS fromLane, l2.name AS toLane, sf.flowName AS artifact, sf.condition AS decision;
```

### 6.7 SubProcess inner elements
```cypher
MATCH (sp:Activity {activityType:'SubProcess'})-[:CONTAINS_PROCESS]->(entry)
OPTIONAL MATCH (sp)-[:CONTAINS]->(m)
RETURN sp.name, id(sp) AS spid, id(entry) AS entryId, collect(id(m)) AS memberIds;
```

### 6.8 CallActivity
```cypher
MATCH (ca:Activity {activityType:'CallActivity'})-[:CALLS_PROCESS]->(pr:Process)
RETURN ca.name, pr.name, pr.processType, id(ca), id(pr);
```

### 6.9 Group members
```cypher
MATCH (g:Group)-[:GROUPS]->(m)
WHERE g.modelKey = $modelKey
RETURN g.name, labels(m) AS memberLabels, id(m) AS memberId;
```


## Part 7. Embedding/Indexing Notes (Context)

- **Granularity:** Process (scope/decomposition only), Participant (scope & interactions), Lane (scope & interactions), Activity/Event/Gateway (function & neighbor), Embedded (SubProcess/Transaction/CallActivity with scope/interactions/catalog), Group (scope only).
- **Interactions sources:** MESSAGE_FLOW for cross-pool; SEQUENCE_FLOW lane-change for handoff; boundary/compensate/link as special interactions.
- **Group:** no execution semantics; use as thematic anchor with `GROUPS` membership.


---

## Part 8. Invariants & Sanity Checks

- Every node/rel must carry `modelKey`.
- A `Participant` with `processRef` **must** have exactly one `EXECUTES` to a `Process` with the same BPMN id.
- A `Lane` **must** have exactly one incoming `HAS_LANE` from its owning `Process` (or `SubProcess` if modeled there).
- `OWNS_NODE` should come from **either** Lane or Process (not both) for a given FlowNode.
- `SEQUENCE_FLOW` and `MESSAGE_FLOW` endpoints **must** be FlowNodes (`type ∈ {Activity,Event,Gateway}`).


---

## Part 9. Glossary

- **Pool:** Participant
- **Lane:** Partition within a Process or SubProcess (LaneSet)
- **FlowNode:** Any of Activity / Event / Gateway
- **Data:** DataObjectReference or DataStoreReference
- **SubProcess containment:** `CONTAINS_PROCESS` (entry node), `CONTAINS` (members)
- **CallActivity linkage:** `CALLS_PROCESS`
- **Boundary/Compensate/Link:** event-specific relations
- **HAS_LANE:** (added) Process→Lane (and optional SubProcess→Lane)
