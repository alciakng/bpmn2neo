# core/bpmn_parser.py

import os
import xml.etree.ElementTree as ET
from typing import Dict, List, Any, Optional, Set
from dataclasses import dataclass

from bpmn2neo.config.logger import Logger
from bpmn2neo.settings import ContainerSettings  # Project-wide structured logger

class Parser:
    """SRP: Dedicated BPMN XML parser that produces nodes and relationships.

    - Dependency injection: requires ContainerSettings; optional logger injection.
    - Structured logging with [PARSE] prefix.
    - Defensive try/except on every public/private method for robust diagnostics.
    """

    def __init__(self, container_config: ContainerSettings, logger: Optional[Any] = None):
        """Initialize parser with container config and optional logger (DI)."""
        try:
            self.container_config = container_config
            self.logger = logger if logger is not None else Logger.get_logger(self.__class__.__name__)
            # Parsing buffers
            self.nodes: List[Dict[str, Any]] = []
            self.relationships: List[Dict[str, Any]] = []
            # Internal indices
            self._modelkey_by_collab: Dict[str, str] = {}
            self._participant_by_process: Dict[str, str] = {}
            self._process_lanes_info: Dict[str, bool] = {}
            self._default_flows_by_process: Dict[str, Set[str]] = {}
            self._lane_membership: Dict[str, Dict[str, Set[str]]] = {}
            self._flow_nodes_by_process: Dict[str, Set[str]] = {}
            self._model_key_by_node_id: Dict[str, Optional[str]] = {}
            self._link_map: Dict[str, Dict[str, Dict[str, Set[str]]]] = {}
            # Hierarchy tracking
            self._subprocess_contents: Dict[str, List[str]] = {}
            self.logger.info("[PARSE][INIT] Parser initialized", extra={"extra": {
                "container_type": container_config.container_type,
                "container_id": container_config.container_id
            }})
        except Exception as e:
            # If initialization fails, there is no safe fallback; re-raise.
            Logger.get_logger(self.__class__.__name__).exception("[PARSE][INIT] Initialization failed")
            raise

    def parse(self, xml_file_path: str, model_key: str) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Parse BPMN XML and return (nodes, relationships)."""
        try:
            # Reset state
            try:
                self._reset_state()
                self.logger.info("[PARSE][INIT] State reset")
            except Exception:
                self.logger.exception("[PARSE][INIT] Failed to reset state")
                raise

            # Load XML tree
            try:
                tree = ET.parse(xml_file_path)
                root = tree.getroot()
            except Exception:
                self.logger.exception("[PARSE][INIT] Failed to parse XML file")
                raise

            # Resolve model key (prefer explicit param; else derive from filename)
            try:
                # If model_key is falsy (None / ""), fallback to filename-derived key
                self.logger.info(
                    "[PARSE] Start",
                    extra={"extra": {
                        "file": xml_file_path,
                        "model_key": model_key,
                        "provided_model_key": model_key,
                        "derived_from_filename": model_key
                    }},
                )
            except Exception:
                self.logger.exception("[PARSE][INIT] Failed to resolve filename/model_key")
                raise

            # Ensure container node
            try:
                self._ensure_container_node()
                self.logger.info("[PARSE][CONTAINER] Container node ensured")
            except Exception:
                self.logger.exception("[PARSE][CONTAINER] Failed to ensure container node")
                raise

            # Phase 1
            try:
                self.logger.info("[PARSE][PHASE1] Parsing collaborations & participants")
                self._parse_collaborations(root, model_key)
                self.logger.info("[PARSE][PHASE1] Done")
            except Exception:
                self.logger.exception("[PARSE][PHASE1] Failed")
                raise

            # Phase 2
            try:
                self.logger.info("[PARSE][PHASE2] Parsing processes & internal elements")
                self._parse_processes(root)
                self.logger.info("[PARSE][PHASE2] Done")
            except Exception:
                self.logger.exception("[PARSE][PHASE2] Failed")
                raise

            # Phase 3
            try:
                self.logger.info("[PARSE][PHASE3] Parsing message flows")
                self._parse_message_flows(root)
                self.logger.info("[PARSE][PHASE3] Done")
            except Exception:
                self.logger.exception("[PARSE][PHASE3] Failed")
                raise

            # Phase 4
            try:
                self.logger.info("[PARSE][PHASE4] Parsing annotations & groups")
                self._parse_annotations_and_groups(root)
                self.logger.info("[PARSE][PHASE4] Done")
            except Exception:
                self.logger.exception("[PARSE][PHASE4] Failed")
                raise

            # Phase 5
            try:
                self.logger.info("[PARSE][PHASE5] Finalizing link events")
                self._finalize_link_events()
                self.logger.info("[PARSE][PHASE5] Done")
            except Exception:
                self.logger.exception("[PARSE][PHASE5] Failed")
                raise

            self.logger.info(
                "[PARSE] Finished",
                extra={"extra": {"nodes": len(self.nodes), "rels": len(self.relationships)}},
            )
            return self.nodes.copy(), self.relationships.copy()

        except Exception:
            # Ensure top-level parse errors are bubbled up (Loader handles it).
            self.logger.exception("[PARSE] Unhandled error in parse()")
            raise


    # ====================== Utilities ======================

    def _reset_state(self):
        """Reset internal buffers and indices."""
        try:
            self.nodes.clear()
            self.relationships.clear()
            self._modelkey_by_collab.clear()
            self._participant_by_process.clear()
            self._process_lanes_info.clear()
            self._default_flows_by_process.clear()
            self._lane_membership.clear()
            self._flow_nodes_by_process.clear()
            self._model_key_by_node_id.clear()
            self._link_map.clear()
        except Exception:
            self.logger.exception("[PARSE][STATE] Failed to clear internal buffers/indices")
            raise

    def get_model_keys(self) -> List[str]:
        """Return extracted model keys."""
        try:
            keys = set(self._modelkey_by_collab.values())
            if not keys:
                for n in self.nodes:
                    mk = n.get("properties", {}).get("modelKey")
                    if mk:
                        keys.add(mk)
            return list(keys)
        except Exception:
            self.logger.exception("[PARSE][STATE] Failed to build model key list")
            raise

    def _find_by_local_name(self, parent: ET.Element, tag_name: str) -> List[ET.Element]:
        """Find elements by local tag name."""
        try:
            return [elem for elem in parent.iter() if elem.tag.split('}')[-1] == tag_name]
        except Exception:
            self.logger.exception("[PARSE][XML] Failed in _find_by_local_name")
            raise

    def _extract_attributes(self, element: ET.Element) -> Dict[str, Any]:
        """Extract element attributes into a plain dict."""
        try:
            props: Dict[str, Any] = {}
            for k, v in element.attrib.items():
                clean_key = k.split('}')[-1] if '}' in k else k
                props[clean_key] = v
            return props
        except Exception:
            self.logger.exception("[PARSE][XML] Failed in _extract_attributes")
            raise

    def _attach_common_properties(self, properties: Dict[str, Any], model_key: Optional[str] = None):
        """Attach container and (optionally) model key to properties."""
        try:
            properties['containerType'] = self.container_config.container_type
            properties['containerId'] = self.container_config.container_id
            if model_key:
                properties['modelKey'] = model_key
        except Exception:
            self.logger.exception("[PARSE][STATE] Failed in _attach_common_properties")
            raise

    def _ensure_container_node(self):
        """Ensure a container node is present in the buffer."""
        try:
            self.nodes.append({
                'id': self.container_config.container_id,
                'type': self.container_config.container_type,
                'name': self.container_config.container_name,
                'properties': {
                    'containerType': self.container_config.container_type,
                    'containerId': self.container_config.container_id
                }
            })
        except Exception:
            self.logger.exception("[PARSE][CONTAINER] Failed to append container node")
            raise

    # ========== Property extraction helpers ==========

    def _get_process_specific_properties(self, process: ET.Element) -> Dict[str, Any]:
        """Extract non-default process-specific properties."""
        try:
            props: Dict[str, Any] = {}
            process_type = process.get('processType', 'None')
            if process_type != 'None':
                props['processType'] = process_type
                self.logger.debug(f"[PARSE][PROCESS] processType={process_type}")
            if process.get('isClosed') == 'true':
                props['isClosed'] = True
                self.logger.debug("[PARSE][PROCESS] isClosed=True")
            is_executable = process.get('isExecutable')
            if is_executable:
                props['isExecutable'] = is_executable == 'true'
                self.logger.debug(f"[PARSE][PROCESS] isExecutable={is_executable}")
            definitional_collab = process.get('definitionalCollaborationRef')
            if definitional_collab:
                props['definitionalCollaborationRef'] = definitional_collab
                self.logger.debug(f"[PARSE][PROCESS] definitionalCollaborationRef={definitional_collab}")
            supports = process.get('supports')
            if supports:
                props['supports'] = supports
                self.logger.debug(f"[PARSE][PROCESS] supports={supports}")
            if props:
                self.logger.info(f"[PARSE][PROCESS] Extracted {len(props)} process properties")
            return props
        except Exception:
            self.logger.exception("[PARSE][PROCESS] Failed extracting process properties")
            raise

    def _get_activity_specific_properties(self, element: ET.Element, activity_type: str) -> Dict[str, Any]:
        """Extract activity-type specific and non-default properties."""
        try:
            props: Dict[str, Any] = {}
            if element.get('isForCompensation') == 'true':
                props['isForCompensation'] = True
            if element.get('default'):
                props['default'] = element.get('default')
            start_qty = element.get('startQuantity', '1')
            if start_qty != '1':
                props['startQuantity'] = int(start_qty)
            completion_qty = element.get('completionQuantity', '1')
            if completion_qty != '1':
                props['completionQuantity'] = int(completion_qty)
            if activity_type in ['User', 'Service', 'BusinessRule']:
                implementation = element.get('implementation')
                if implementation:
                    props['implementation'] = implementation
            if activity_type == 'Script':
                script_format = element.get('scriptFormat')
                if script_format:
                    props['scriptFormat'] = script_format
            if activity_type in ['Send', 'Receive']:
                message_ref = element.get('messageRef')
                if message_ref:
                    props['messageRef'] = message_ref
                operation_ref = element.get('operationRef')
                if operation_ref:
                    props['operationRef'] = operation_ref
            if activity_type == 'Receive':
                if element.get('instantiate') == 'true':
                    props['instantiate'] = True
            if activity_type == 'Call':
                called_element = element.get('calledElement')
                if called_element:
                    props['calledElement'] = called_element
            if activity_type == 'SubProcess':
                if element.get('triggeredByEvent') == 'true':
                    props['triggeredByEvent'] = True
            if activity_type == 'Transaction':
                method = element.get('method', '##Compensate')
                if method != '##Compensate':
                    props['method'] = method
            return props
        except Exception:
            self.logger.exception("[PARSE][ACTIVITY] Failed extracting activity properties")
            raise

    def _extract_activity_characteristics(self, element: ET.Element) -> Dict[str, Any]:
        """Extract loop/multi-instance characteristics of an activity."""
        try:
            props: Dict[str, Any] = {}
            for child in element:
                local_name = child.tag.split('}')[-1]
                if local_name == 'standardLoopCharacteristics':
                    props['hasStandardLoop'] = True
                    if child.get('testBefore') == 'false':
                        props['standardLoopTestBefore'] = False
                    loop_max = child.get('loopMaximum')
                    if loop_max:
                        props['standardLoopMaximum'] = int(loop_max)
                elif local_name == 'multiInstanceLoopCharacteristics':
                    props['hasMultiInstance'] = True
                    if child.get('isSequential') == 'true':
                        props['multiInstanceSequential'] = True
                    behavior = child.get('behavior', 'All')
                    if behavior != 'All':
                        props['multiInstanceBehavior'] = behavior
                elif local_name.endswith('Characteristics'):
                    characteristic_type = local_name.replace('Characteristics', '')
                    props[f'has{characteristic_type}'] = True
                    for attr_name, attr_value in child.attrib.items():
                        clean_attr = attr_name.split('}')[-1]
                        props[f'{characteristic_type}_{clean_attr}'] = attr_value
            return props
        except Exception:
            self.logger.exception("[PARSE][ACTIVITY] Failed extracting activity characteristics")
            raise

    def _get_event_specific_properties(self, element: ET.Element, position: str, event_definitions: List[ET.Element]) -> Dict[str, Any]:
        """Extract non-default event properties depending on position."""
        try:
            props: Dict[str, Any] = {}
            if position == 'Start':
                if element.get('isInterrupting') == 'false':
                    props['isInterrupting'] = False
                if element.get('parallelMultiple') == 'true':
                    props['parallelMultiple'] = True
            elif position == 'Boundary':
                attached_ref = element.get('attachedToRef')
                if attached_ref:
                    props['attachedToRef'] = attached_ref
                if element.get('cancelActivity') == 'false':
                    props['cancelActivity'] = False
            elif position in ['Intermediate']:
                if element.get('parallelMultiple') == 'true':
                    props['parallelMultiple'] = True
            if len(event_definitions) > 1:
                props['eventDefinitionCount'] = len(event_definitions)
                def_types = [ed.tag.split('}')[-1].replace('EventDefinition', '').capitalize() for ed in event_definitions]
                props['eventDefinitionTypes'] = def_types
            return props
        except Exception:
            self.logger.exception("[PARSE][EVENT] Failed extracting event properties")
            raise

    def _get_gateway_specific_properties(self, element: ET.Element, gateway_type: str) -> Dict[str, Any]:
        """Extract non-default gateway properties."""
        try:
            props: Dict[str, Any] = {}
            direction = element.get('gatewayDirection', 'Unspecified')
            if direction != 'Unspecified':
                props['gatewayDirection'] = direction
            if gateway_type in ['Exclusive', 'Inclusive', 'Complex']:
                default_flow = element.get('default')
                if default_flow:
                    props['default'] = default_flow
            if gateway_type == 'EventBased':
                event_gateway_type = element.get('eventGatewayType', 'Exclusive')
                if event_gateway_type != 'Exclusive':
                    props['eventGatewayType'] = event_gateway_type
                if element.get('instantiate') == 'true':
                    props['instantiate'] = True
            return props
        except Exception:
            self.logger.exception("[PARSE][GATEWAY] Failed extracting gateway properties")
            raise

    def _get_data_specific_properties(self, element: ET.Element, data_type: str) -> Dict[str, Any]:
        """Extract non-default data element properties."""
        try:
            props: Dict[str, Any] = {}
            item_subject_ref = element.get('itemSubjectRef')
            if item_subject_ref:
                props['itemSubjectRef'] = item_subject_ref
            if element.get('isCollection') == 'true':
                props['isCollection'] = True
            if data_type == 'Store':
                capacity = element.get('capacity')
                if capacity:
                    props['capacity'] = int(capacity)
                if element.get('isUnlimited') == 'false':
                    props['isUnlimited'] = False
            return props
        except Exception:
            self.logger.exception("[PARSE][DATA] Failed extracting data properties")
            raise

    def _get_data_references_properties(self, element: ET.Element, data_type: str) -> Dict[str, Any]:
        """Extract non-default properties for <dataObjectReference>/<dataStoreReference>."""
        try:
            props: Dict[str, Any] = {}
            if data_type == 'ObjectReference':
                ref_value = element.get('dataObjectRef')
                if ref_value:
                    props['dataObjectRef'] = ref_value
            elif data_type == 'StoreReference':
                ref_value = element.get('dataStoreRef')
                if ref_value:
                    props['dataStoreRef'] = ref_value
            try:
                for child in list(element):
                    tag = child.tag
                    local = tag.split('}', 1)[1] if '}' in tag else tag
                    if local == 'dataState':
                        name_attr = child.get('name')
                        if name_attr:
                            props['dataState'] = name_attr
                        break
            except Exception:
                # Tolerate malformed content
                self.logger.debug("[PARSE][DATA-REF] dataState parsing skipped due to child iteration error")
            return props
        except Exception:
            self.logger.exception("[PARSE][DATA-REF] Failed extracting reference properties")
            raise

    # ====================== Parsing methods ======================

    def _parse_collaborations(self, root: ET.Element, final_model_key: str):
        """Parse <collaboration> and its <participant> elements."""
        try:
            collaborations = self._find_by_local_name(root, 'collaboration')
            if not collaborations:
                # Fallback: create a default BPMNModel
                model_key = final_model_key
                self._modelkey_by_collab['__default__'] = model_key
                props = {'id': f'{model_key}_model', 'modelKey': model_key}
                self._attach_common_properties(props, model_key)
                self.nodes.append({
                    'id': f'{model_key}_model',
                    'type': 'BPMNModel',
                    'name': model_key,
                    'properties': props
                })
                rel_props: Dict[str, Any] = {}
                self._attach_common_properties(rel_props, model_key)
                self.relationships.append({
                    'source': self.container_config.container_id,
                    'target': f'{model_key}_model',
                    'type': 'HAS_MODEL',
                    'properties': rel_props
                })
                self.logger.info("[PARSE][PHASE1] No collaboration; default model created")
                return
            for collab in collaborations:
                collab_id = collab.get('id')
                model_key = final_model_key
                self._modelkey_by_collab[collab_id] = model_key
                props = self._extract_attributes(collab)
                props['modelKey'] = model_key
                self._attach_common_properties(props, model_key)
                self.nodes.append({
                    'id': collab_id,
                    'type': 'BPMNModel',
                    'name': final_model_key,
                    'properties': props
                })
                rel_props: Dict[str, Any] = {}
                self._attach_common_properties(rel_props, model_key)
                self.relationships.append({
                    'source': self.container_config.container_id,
                    'target': collab_id,
                    'type': 'HAS_MODEL',
                    'properties': rel_props
                })
                self._parse_participants(collab, collab_id, model_key)
        except Exception:
            self.logger.exception("[PARSE][PHASE1] Collaboration/Participant parsing failed")
            raise

    def _parse_participants(self, collab: ET.Element, collab_id: str, model_key: str):
        """Parse <participant> under a collaboration."""
        try:
            for part in self._find_by_local_name(collab, 'participant'):
                part_id = part.get('id')
                part_name = part.get('name', '') or part_id
                process_ref = part.get('processRef')
                props = self._extract_attributes(part)
                props['processRef'] = process_ref
                self._attach_common_properties(props, model_key)
                self.nodes.append({
                    'id': part_id,
                    'type': 'Participant',
                    'name': part_name,
                    'properties': props
                })
                rel_props: Dict[str, Any] = {}
                self._attach_common_properties(rel_props, model_key)
                self.relationships.append({
                    'source': collab_id,
                    'target': part_id,
                    'type': 'HAS_PARTICIPANT',
                    'properties': rel_props
                })
                if process_ref:
                    self._participant_by_process[process_ref] = part_id
        except Exception:
            self.logger.exception("[PARSE][PHASE1] Participant parsing failed")
            raise

    def _parse_processes(self, root: ET.Element):
        """Parse <process> blocks and their inner BPMN elements."""
        try:
            for process in self._find_by_local_name(root, 'process'):
                process_id = process.get('id')
                process_name = process.get('name', '') or process_id
                model_key = self._resolve_model_key_for_process(process_id)
                # Collect default flows
                self._default_flows_by_process[process_id] = self._collect_default_flows(process)
                # Process node
                props = self._extract_attributes(process)
                props.update(self._get_process_specific_properties(process))
                self._attach_common_properties(props, model_key)
                self.nodes.append({
                    'id': process_id,
                    'type': 'Process',
                    'name': process_name,
                    'properties': props
                })
                # Participant → Process
                participant_id = self._participant_by_process.get(process_id)
                if participant_id:
                    rel_props: Dict[str, Any] = {}
                    self._attach_common_properties(rel_props, model_key)
                    self.relationships.append({
                        'source': participant_id,
                        'target': process_id,
                        'type': 'EXECUTES',
                        'properties': rel_props
                    })
                # Inner
                self._parse_process_elements(process, process_id, model_key)
                self._parse_lanes(process, process_id, model_key)
                self._parse_sequence_flows(process, process_id, model_key)
                # Ownership
                self._apply_lane_ownership(process_id, model_key)
        except Exception:
            self.logger.exception("[PARSE][PHASE2] Process parsing failed")
            raise

    def _parse_process_elements(self, process: ET.Element, process_id: str, model_key: Optional[str]):
        """Parse events, activities, gateways, and data elements inside a process."""
        try:
            self._flow_nodes_by_process.setdefault(process_id, set())
            # Events
            event_types = [
                ('startEvent', 'Start'), ('endEvent', 'End'),
                ('intermediateCatchEvent', 'Intermediate'), ('intermediateThrowEvent', 'Intermediate'),
                ('boundaryEvent', 'Boundary')
            ]
            for tag, position in event_types:
                for event in self._find_by_local_name(process, tag):
                    self._parse_event(event, tag, position, process_id, model_key)
            # Activities
            activity_types = [
                ('userTask', 'User'), ('serviceTask', 'Service'), ('sendTask', 'Send'),
                ('receiveTask', 'Receive'), ('manualTask', 'Manual'), ('businessRuleTask', 'BusinessRule'),
                ('scriptTask', 'Script'), ('task', 'Task'), ('callActivity', 'Call'),
                ('subProcess', 'SubProcess'), ('transaction', 'Transaction')
            ]
            for tag, activity_type in activity_types:
                for activity in self._find_by_local_name(process, tag):
                    self._parse_activity(activity, tag, activity_type, process_id, model_key)
            # Gateways
            gateway_types = [
                ('parallelGateway', 'Parallel'), ('exclusiveGateway', 'Exclusive'),
                ('inclusiveGateway', 'Inclusive'), ('complexGateway', 'Complex'),
                ('eventBasedGateway', 'EventBased')
            ]
            for tag, gateway_type in gateway_types:
                for gateway in self._find_by_local_name(process, tag):
                    self._parse_gateway(gateway, gateway_type, process_id, model_key)
            # Data
            self._parse_data_elements(process, process_id, model_key)
            self._parse_data_references(process, process_id, model_key)
            self._parse_data_associations(process, process_id, model_key)
        except Exception:
            self.logger.exception("[PARSE][PHASE2] Inner process element parsing failed")
            raise

    def _parse_event(self, event: ET.Element, tag: str, position: str, process_id: str, model_key: Optional[str]):
        """Parse a single event element."""
        try:
            event_id = event.get('id')
            event_name = event.get('name', '') or event_id
            props = self._extract_attributes(event)
            props['position'] = position
            props['detailType'] = self._get_event_detail_type(event)
            self._attach_common_properties(props, model_key)
            self.nodes.append({
                'id': event_id,
                'type': 'Event',
                'name': event_name,
                'properties': props
            })
            self._flow_nodes_by_process[process_id].add(event_id)
            # Special: boundary
            if tag == 'boundaryEvent':
                attached = event.get('attachedToRef')
                if attached:
                    rel_props: Dict[str, Any] = {}
                    self._attach_common_properties(rel_props, model_key)
                    self.relationships.append({
                        'source': attached,
                        'target': event_id,
                        'type': 'HAS_BOUNDARY_EVENT',
                        'properties': rel_props
                    })
            # Link/Compensate
            for child in event:
                local_name = child.tag.split('}')[-1]
                if local_name == 'linkEventDefinition':
                    link_name = child.get('name') or ''
                    if link_name:
                        props['linkName'] = link_name
                    kind = 'throw' if tag == 'intermediateThrowEvent' else 'catch'
                    self._record_link_event(process_id, link_name, kind, event_id)
                elif local_name == 'compensateEventDefinition':
                    if tag == 'boundaryEvent':
                        props['isForCompensation'] = True
                    activity_ref = child.get('activityRef')
                    if activity_ref:
                        wfc = child.get('waitForCompletion', 'true')
                        wfc_bool = (str(wfc).lower() != 'false')
                        rel_props = {'compensationType': 'specific', 'waitForCompletion': wfc_bool}
                        # Note: Keeping original behavior of assigning only compensationType
                        rel_props = {'compensationType': 'specific'}
                        self._attach_common_properties(rel_props, model_key)
                        self.relationships.append({
                            'source': event_id,
                            'target': activity_ref,
                            'type': 'COMPENSATES',
                            'properties': rel_props
                        })
        except Exception:
            self.logger.exception("[PARSE][EVENT] Failed to parse event")
            raise

    def _parse_activity(self, activity: ET.Element, tag: str, activity_type: str, process_id: str, model_key: Optional[str]):
        """Parse a single activity element."""
        try:
            activity_id = activity.get('id')
            activity_name = activity.get('name', '') or activity_id
            props = self._extract_attributes(activity)
            props['activityType'] = activity_type
            props.update(self._get_activity_specific_properties(activity, activity_type))
            props.update(self._extract_activity_characteristics(activity))
            self._attach_common_properties(props, model_key)
            self.nodes.append({
                'id': activity_id,
                'type': 'Activity',
                'name': activity_name,
                'properties': props
            })
            self._flow_nodes_by_process[process_id].add(activity_id)
            if activity_type in ['SubProcess', 'AdHocSubProcess', 'Transaction']:
                self._flow_nodes_by_process.setdefault(activity_id, set())
                self._parse_subprocess_contents(activity, activity_id, model_key)
            if tag == 'callActivity':
                called = activity.get('calledElement')
                if called:
                    rel_props: Dict[str, Any] = {}
                    self._attach_common_properties(rel_props, model_key)
                    self.relationships.append({
                        'source': activity_id,
                        'target': called,
                        'type': 'CALLS_PROCESS',
                        'properties': rel_props
                    })
        except Exception:
            self.logger.exception("[PARSE][ACTIVITY] Failed to parse activity")
            raise

    def _parse_subprocess_contents(self, subprocess: ET.Element, subprocess_id: str, model_key: Optional[str]):
        """Parse inner FlowNodes of a subprocess and record containment edges."""
        try:
            self.logger.info(f"[PARSE][SUBPROCESS] Parse contents: {subprocess_id}")
            internal_elements: List[str] = []
            flow_node_tags = [
                ('startEvent', 'Start'), ('endEvent', 'End'),
                ('intermediateCatchEvent', 'Intermediate'), ('intermediateThrowEvent', 'Intermediate'),
                ('boundaryEvent', 'Boundary'),
                ('userTask', 'User'), ('serviceTask', 'Service'), ('sendTask', 'Send'),
                ('receiveTask', 'Receive'), ('manualTask', 'Manual'), ('businessRuleTask', 'BusinessRule'),
                ('scriptTask', 'Script'), ('task', 'Task'), ('callActivity', 'Call'),
                ('subProcess', 'SubProcess'), ('transaction', 'Transaction'), ('adHocSubProcess', 'AdHocSubProcess'),
                ('parallelGateway', 'Parallel'), ('exclusiveGateway', 'Exclusive'),
                ('inclusiveGateway', 'Inclusive'), ('complexGateway', 'Complex'),
                ('eventBasedGateway', 'EventBased')
            ]
            for tag, _ in flow_node_tags:
                for element in self._find_by_local_name(subprocess, tag):
                    element_id = element.get('id')
                    if element_id and element_id != subprocess_id:
                        rel_props = {'connectionType': 'containment'}
                        self._attach_common_properties(rel_props, model_key)
                        self.relationships.append({
                            'source': subprocess_id,
                            'target': element_id,
                            'type': 'CONTAINS',
                            'properties': rel_props
                        })
                        internal_elements.append(element_id)
            self.logger.info(f"[PARSE][SUBPROCESS] {subprocess_id} contains {len(internal_elements)} elements")
        except Exception:
            self.logger.exception("[PARSE][SUBPROCESS] Failed to parse subprocess contents")
            raise

    def _parse_gateway(self, gateway: ET.Element, gateway_type: str, process_id: str, model_key: Optional[str]):
        """Parse a single gateway element."""
        try:
            gateway_id = gateway.get('id')
            gateway_name = gateway.get('name', '') or gateway_id
            props = self._extract_attributes(gateway)
            props['gatewayType'] = gateway_type
            if 'default' in props:
                props['defaultFlow'] = props['default']
            self._attach_common_properties(props, model_key)
            self.nodes.append({
                'id': gateway_id,
                'type': 'Gateway',
                'name': gateway_name,
                'properties': props
            })
            self._flow_nodes_by_process[process_id].add(gateway_id)
        except Exception:
            self.logger.exception("[PARSE][GATEWAY] Failed to parse gateway")
            raise

    def _parse_data_elements(self, process: ET.Element, process_id: str, model_key: Optional[str]):
        """Parse Data Objects and Data Stores (definition nodes)."""
        try:
            for dref in self._find_by_local_name(process, 'dataObject'):
                data_id = dref.get('id')
                data_name = dref.get('name', '') or data_id
                props = self._extract_attributes(dref)
                props['dataType'] = 'Object'
                data_props = self._get_data_specific_properties(dref, 'ObjectReference')
                props.update(data_props)
                self._attach_common_properties(props, model_key)
                self.nodes.append({
                    'id': data_id,
                    'type': 'Data',
                    'name': data_name,
                    'properties': props
                })
            for dsref in self._find_by_local_name(process, 'dataStore'):
                data_id = dsref.get('id')
                data_name = dsref.get('name', '') or data_id
                props = self._extract_attributes(dsref)
                props['dataType'] = 'Store'
                self._attach_common_properties(props, model_key)
                self.nodes.append({
                    'id': data_id,
                    'type': 'Data',
                    'name': data_name,
                    'properties': props
                })
        except Exception:
            self.logger.exception("[PARSE][DATA] Failed to parse data definitions")
            raise

    def _parse_data_references(self, process: ET.Element, process_id: str, model_key: Optional[str]):
        """Parse DataObjectReference / DataStoreReference (reference nodes) and REFERS_TO edges."""
        try:
            self.logger.info(f"[PARSE][DATA-REF] Scope: {process_id}")
            created_refs = 0
            created_refers = 0
            # ObjectReference
            for oref in self._find_by_local_name(process, 'dataObjectReference'):
                data_id = oref.get('id')
                data_name = oref.get('name', '') or data_id
                if not data_id:
                    self.logger.warning("[PARSE][DATA-REF][Object] dataObjectReference without id; skipping")
                    continue
                props = self._extract_attributes(oref)
                props['dataType'] = 'ObjectReference'
                ref_props = self._get_data_references_properties(oref, 'ObjectReference')
                props.update(ref_props)
                self._attach_common_properties(props, model_key)
                self.nodes.append({'id': data_id, 'type': 'DataReference', 'name': data_name, 'properties': props})
                created_refs += 1
                self.logger.info(f"[PARSE][DATA-REF] Created DataReference(Object): {data_id} (name={data_name})")
                def_id = props.get('dataObjectRef')
                if def_id:
                    rel_props: Dict[str, Any] = {}
                    self._attach_common_properties(rel_props, model_key)
                    self.relationships.append({'source': data_id, 'target': def_id, 'type': 'REFERS_TO', 'properties': rel_props})
                    created_refers += 1
                    self.logger.info(f"[PARSE][DATA-REF] REFERS_TO created: {data_id} → {def_id}")
                else:
                    self.logger.debug(f"[PARSE][DATA-REF][Object] No dataObjectRef on {data_id}; REFERS_TO skipped")
            # StoreReference
            for sref in self._find_by_local_name(process, 'dataStoreReference'):
                data_id = sref.get('id')
                data_name = sref.get('name', '') or data_id
                if not data_id:
                    self.logger.warning("[PARSE][DATA-REF][Store] dataStoreReference without id; skipping")
                    continue
                props = self._extract_attributes(sref)
                props['dataType'] = 'StoreReference'
                ref_props = self._get_data_references_properties(sref, 'StoreReference')
                props.update(ref_props)
                self._attach_common_properties(props, model_key)
                self.nodes.append({'id': data_id, 'type': 'DataReference', 'name': data_name, 'properties': props})
                created_refs += 1
                self.logger.info(f"[PARSE][DATA-REF] Created DataReference(Store): {data_id} (name={data_name})")
                def_id = props.get('dataStoreRef')
                if def_id:
                    rel_props = {}
                    self._attach_common_properties(rel_props, model_key)
                    self.relationships.append({'source': data_id, 'target': def_id, 'type': 'REFERS_TO', 'properties': rel_props})
                    created_refers += 1
                    self.logger.info(f"[PARSE][DATA-REF] REFERS_TO created: {data_id} → {def_id}")
                else:
                    self.logger.debug(f"[PARSE][DATA-REF][Store] No dataStoreRef on {data_id}; REFERS_TO skipped")
            self.logger.info(f"[PARSE][DATA-REF] Complete - refs={created_refs}, refers_to={created_refers}")
        except Exception:
            self.logger.exception("[PARSE][DATA-REF] Failed to parse data references")
            raise

    def _parse_data_associations(self, process: ET.Element, process_id: str, model_key: Optional[str]):
        """Parse data input/output associations between activities and data elements."""
        try:
            self.logger.info(f"[PARSE][ASSOC] Start for process: {process_id}")
            activity_tags = {
                'userTask', 'serviceTask', 'sendTask', 'receiveTask',
                'manualTask', 'businessRuleTask', 'scriptTask', 'task',
                'callActivity', 'subProcess', 'transaction', 'AdHocSubProcess'
            }
            container_activity_tags = {'subProcess', 'transaction', 'callActivity', 'AdHocSubProcess'}
            activity_count = 0
            association_count = 0
            for activity in process.iter():
                local_name = activity.tag.split('}')[-1]
                if local_name in activity_tags:
                    activity_count += 1
                    activity_id = activity.get('id')
                    self.logger.debug(f"[PARSE][ASSOC] Inspect activity: {activity_id} ({local_name})")
                    if not activity_id:
                        self.logger.warning(f"[PARSE][ASSOC] Activity {local_name} missing id; skipping")
                        continue
                    if local_name in container_activity_tags:
                        input_assocs = [ch for ch in list(activity)
                                        if (ch.tag.split('}', 1)[-1] if '}' in ch.tag else ch.tag) == 'dataInputAssociation']
                        output_assocs = [ch for ch in list(activity)
                                         if (ch.tag.split('}', 1)[-1] if '}' in ch.tag else ch.tag) == 'dataOutputAssociation']
                    else:
                        input_assocs = self._find_by_local_name(activity, 'dataInputAssociation')
                        output_assocs = self._find_by_local_name(activity, 'dataOutputAssociation')
                    self.logger.debug(f"[PARSE][ASSOC] inputs={len(input_assocs)} outputs={len(output_assocs)} for {activity_id}")
                    # Inputs
                    for data_input_assoc in input_assocs:
                        source_ref = self._extract_source_ref(data_input_assoc)
                        self.logger.info(f"[PARSE][ASSOC][INPUT] {activity_id} ← {source_ref}")
                        if source_ref:
                            source_exists = any(node['id'] == source_ref for node in self.nodes)
                            self.logger.debug(f"[PARSE][ASSOC][INPUT] source_exists={source_exists}")
                            props = self._extract_attributes(data_input_assoc)
                            props['associationType'] = 'input'
                            self._attach_common_properties(props, model_key)
                            rel = {'source': activity_id, 'target': source_ref, 'type': 'READS_FROM', 'properties': props}
                            self.relationships.append(rel)
                            association_count += 1
                            self.logger.info(f"[PARSE][ASSOC] Created READS_FROM: {activity_id} → {source_ref}")
                        else:
                            self.logger.warning(f"[PARSE][ASSOC][INPUT] No sourceRef for {activity_id}")
                    # Outputs
                    for data_output_assoc in output_assocs:
                        target_ref = self._extract_target_ref(data_output_assoc)
                        self.logger.info(f"[PARSE][ASSOC][OUTPUT] {activity_id} → {target_ref}")
                        if target_ref:
                            target_exists = any(node['id'] == target_ref for node in self.nodes)
                            self.logger.debug(f"[PARSE][ASSOC][OUTPUT] target_exists={target_exists}")
                            props = self._extract_attributes(data_output_assoc)
                            props['associationType'] = 'output'
                            self._attach_common_properties(props, model_key)
                            rel = {'source': activity_id, 'target': target_ref, 'type': 'WRITES_TO', 'properties': props}
                            self.relationships.append(rel)
                            association_count += 1
                            self.logger.info(f"[PARSE][ASSOC] Created WRITES_TO: {activity_id} → {target_ref}")
                        else:
                            self.logger.warning(f"[PARSE][ASSOC][OUTPUT] No targetRef for {activity_id}")
            self.logger.info(f"[PARSE][ASSOC] Complete - activities={activity_count}, associations={association_count}")
        except Exception:
            self.logger.exception("[PARSE][ASSOC] Failed to parse data associations")
            raise

    def _extract_source_ref(self, data_association: ET.Element) -> Optional[str]:
        """Extract sourceRef from a data association."""
        try:
            self.logger.debug(f"[PARSE][ASSOC] Extract sourceRef from {data_association.tag}")
            source_attr = data_association.get('sourceRef')
            if source_attr:
                self.logger.debug(f"[PARSE][ASSOC] sourceRef attribute={source_attr}")
                return source_attr
            source_elem = None
            for child in data_association:
                child_local_name = child.tag.split('}')[-1]
                if child_local_name == 'sourceRef':
                    source_elem = child
                    break
            if source_elem is not None:
                source_text = source_elem.text.strip() if source_elem.text else None
                self.logger.debug(f"[PARSE][ASSOC] sourceRef child={source_text}")
                return source_text
            self.logger.warning("[PARSE][ASSOC] No sourceRef in data association")
            children_info = [(c.tag.split('}')[-1], c.text, c.attrib) for c in data_association]
            self.logger.debug(f"[PARSE][ASSOC] children={children_info}")
            return None
        except Exception:
            self.logger.exception("[PARSE][ASSOC] Failed extracting sourceRef")
            raise

    def _extract_target_ref(self, data_association: ET.Element) -> Optional[str]:
        """Extract targetRef from a data association."""
        try:
            self.logger.debug(f"[PARSE][ASSOC] Extract targetRef from {data_association.tag}")
            target_attr = data_association.get('targetRef')
            if target_attr:
                self.logger.debug(f"[PARSE][ASSOC] targetRef attribute={target_attr}")
                return target_attr
            target_elem = None
            for child in data_association:
                child_local_name = child.tag.split('}')[-1]
                if child_local_name == 'targetRef':
                    target_elem = child
                    break
            if target_elem is not None:
                target_text = target_elem.text.strip() if target_elem.text else None
                self.logger.debug(f"[PARSE][ASSOC] targetRef child={target_text}")
                return target_text
            self.logger.warning("[PARSE][ASSOC] No targetRef in data association")
            children_info = [(c.tag.split('}')[-1], c.text, c.attrib) for c in data_association]
            self.logger.debug(f"[PARSE][ASSOC] children={children_info}")
            return None
        except Exception:
            self.logger.exception("[PARSE][ASSOC] Failed extracting targetRef")
            raise

    def _parse_lanes(self, process: ET.Element, process_id: str, model_key: Optional[str]):
        """Parse <laneSet>/<lane> and connect ownership."""
        try:
            has_lanes = False
            lane_membership: Dict[str, Set[str]] = {}
            for lane_set in self._find_by_local_name(process, 'laneSet'):
                for lane in self._find_by_local_name(lane_set, 'lane'):
                    has_lanes = True
                    lane_id = lane.get('id')
                    lane_name = lane.get('name', '') or lane_id
                    props = self._extract_attributes(lane)
                    self._attach_common_properties(props, model_key)
                    self.nodes.append({'id': lane_id, 'type': 'Lane', 'name': lane_name, 'properties': props})
                    rel_props: Dict[str, Any] = {}
                    self._attach_common_properties(rel_props, model_key)
                    self.relationships.append({'source': process_id, 'target': lane_id, 'type': 'HAS_LANE', 'properties': rel_props})
                    node_ids = set()
                    for flownode_ref in self._find_by_local_name(lane, 'flowNodeRef'):
                        if flownode_ref.text and flownode_ref.text.strip():
                            node_ids.add(flownode_ref.text.strip())
                    lane_membership[lane_id] = node_ids
            self._process_lanes_info[process_id] = has_lanes
            self._lane_membership[process_id] = lane_membership
        except Exception:
            self.logger.exception("[PARSE][LANE] Failed parsing lanes")
            raise

    def _parse_sequence_flows(self, process: ET.Element, process_id: str, model_key: Optional[str]):
        """Parse <sequenceFlow> edges including conditions and default flags."""
        try:
            default_flows = self._default_flows_by_process.get(process_id, set())
            for seq_flow in self._find_by_local_name(process, 'sequenceFlow'):
                flow_id = seq_flow.get('id')
                source_ref = seq_flow.get('sourceRef')
                target_ref = seq_flow.get('targetRef')
                props = self._extract_attributes(seq_flow)
                props['flowName'] = seq_flow.get('name', '') or flow_id
                props['flowType'] = 'SequenceFlow'
                props['isDefault'] = flow_id in default_flows
                cond_elem = next((c for c in seq_flow if c.tag.split('}')[-1] == 'conditionExpression'), None)
                if cond_elem is not None and cond_elem.text:
                    props['condition'] = cond_elem.text.strip()
                    props['flowType'] = 'ConditionalSequenceFlow'
                self._attach_common_properties(props, model_key)
                self.relationships.append({'source': source_ref, 'target': target_ref, 'type': 'SEQUENCE_FLOW', 'properties': props})
        except Exception:
            self.logger.exception("[PARSE][SEQUENCE] Failed parsing sequence flows")
            raise

    def _parse_message_flows(self, root: ET.Element):
        """Parse <messageFlow> between participants."""
        try:
            for collab in self._find_by_local_name(root, 'collaboration'):
                model_key = self._modelkey_by_collab.get(collab.get('id'))
                for msg_flow in self._find_by_local_name(collab, 'messageFlow'):
                    props = self._extract_attributes(msg_flow)
                    props['flowName'] = msg_flow.get('name', '') or msg_flow.get('id')
                    props['flowType'] = 'MessageFlow'
                    message_ref = msg_flow.get('messageRef')
                    if message_ref:
                        props['messageRef'] = message_ref
                    self._attach_common_properties(props, model_key)
                    self.relationships.append({
                        'source': msg_flow.get('sourceRef'),
                        'target': msg_flow.get('targetRef'),
                        'type': 'MESSAGE_FLOW',
                        'properties': props
                    })
        except Exception:
            self.logger.exception("[PARSE][MESSAGE] Failed parsing message flows")
            raise

    def _parse_annotations_and_groups(self, root: ET.Element):
        """Parse <textAnnotation> and <group>; create ANNOTATES/GROUPS relations."""
        try:
            self._rebuild_node_modelkey_index()
            self._parse_text_annotations(root)
            self._parse_groups(root)
        except Exception:
            self.logger.exception("[PARSE][ANNOT/GROUP] Failed parsing annotations/groups")
            raise

    def _parse_text_annotations(self, root: ET.Element):
        """Parse <textAnnotation> nodes and ANNOTATES edges."""
        try:
            text_by_annotation: Dict[str, str] = {}
            for annotation in self._find_by_local_name(root, 'textAnnotation'):
                ann_id = annotation.get('id')
                text_elem = next((c for c in annotation if c.tag.split('}')[-1] == 'text'), None)
                if ann_id:
                    text_by_annotation[ann_id] = (text_elem.text.strip() if text_elem is not None and text_elem.text else "")
            if not text_by_annotation:
                return
            associations = self._collect_associations(root)
            neighbors = self._build_neighbors_map(associations, list(text_by_annotation.keys()))
            for ann_id, text in text_by_annotation.items():
                model_key = self._pick_model_key_from_neighbors(neighbors.get(ann_id, []))
                props = {'text': text}
                self._attach_common_properties(props, model_key)
                name = text[:80] if text else ann_id
                self.nodes.append({'id': ann_id, 'type': 'TextAnnotation', 'name': name, 'properties': props})
                self._model_key_by_node_id[ann_id] = model_key
            self._create_annotation_relationships(associations, text_by_annotation)
        except Exception:
            self.logger.exception("[PARSE][ANNOT] Failed parsing text annotations")
            raise

    def _parse_groups(self, root: ET.Element):
        """Parse <group> nodes and GROUPS relations."""
        try:
            cat_values: Dict[str, str] = {}
            for cv in self._find_by_local_name(root, 'categoryValue'):
                cv_id = cv.get('id')
                value = cv.get('value') or cv.get('name')
                if cv_id:
                    cat_values[cv_id] = value or cv_id
            groups = list(self._find_by_local_name(root, 'group'))
            if not groups:
                return
            associations = self._collect_associations(root)
            group_ids = [g.get('id') for g in groups if g.get('id')]
            neighbors = self._build_neighbors_map(associations, group_ids)
            for group in groups:
                group_id = group.get('id')
                if not group_id:
                    continue
                props = self._extract_attributes(group)
                cat_ref = props.get('categoryValueRef')
                display_name = None
                if cat_ref and cat_ref in cat_values:
                    display_name = cat_values[cat_ref]
                    props['categoryValue'] = display_name
                name = group.get('name') or display_name or group_id
                model_key = self._pick_model_key_from_neighbors(neighbors.get(group_id, []))
                self._attach_common_properties(props, model_key)
                self.nodes.append({'id': group_id, 'type': 'Group', 'name': name, 'properties': props})
                self._model_key_by_node_id[group_id] = model_key
            self._create_group_relationships(associations, set(group_ids))
        except Exception:
            self.logger.exception("[PARSE][GROUP] Failed parsing groups")
            raise

    # ====================== Helpers ======================

    def _resolve_model_key_for_process(self, process_id: str) -> Optional[str]:
        """Resolve model key for a process via participant → collaboration mapping."""
        try:
            participant_id = self._participant_by_process.get(process_id)
            if not participant_id:
                return self._modelkey_by_collab.get('__default__')
            for rel in self.relationships:
                if rel['type'] == 'HAS_PARTICIPANT' and rel['target'] == participant_id:
                    collab_id = rel['source']
                    return self._modelkey_by_collab.get(collab_id)
            return None
        except Exception:
            self.logger.exception("[PARSE][STATE] Failed resolving model key for process")
            raise

    def _collect_default_flows(self, process: ET.Element) -> Set[str]:
        """Collect default flow IDs from gateways in the process."""
        try:
            defaults: Set[str] = set()
            gateway_tags = ['exclusiveGateway', 'inclusiveGateway', 'parallelGateway', 'complexGateway', 'eventBasedGateway']
            for tag in gateway_tags:
                for gateway in self._find_by_local_name(process, tag):
                    default_flow = gateway.get('default')
                    if default_flow:
                        defaults.add(default_flow)
            return defaults
        except Exception:
            self.logger.exception("[PARSE][SEQUENCE] Failed collecting default flows")
            raise

    def _get_event_detail_type(self, event: ET.Element) -> str:
        """Detect event definition type from child elements."""
        try:
            type_mapping = {
                'messageEventDefinition': 'Message',
                'timerEventDefinition': 'Timer',
                'errorEventDefinition': 'Error',
                'escalationEventDefinition': 'Escalation',
                'conditionalEventDefinition': 'Conditional',
                'linkEventDefinition': 'Link',
                'compensateEventDefinition': 'Compensate',
                'signalEventDefinition': 'Signal',
                'terminateEventDefinition': 'Terminate'
            }
            for child in event:
                local_name = child.tag.split('}')[-1]
                if local_name in type_mapping:
                    return type_mapping[local_name]
            return 'None'
        except Exception:
            self.logger.exception("[PARSE][EVENT] Failed determining event detail type")
            raise

    def _apply_lane_ownership(self, process_id: str, model_key: Optional[str]):
        """Apply ownership edges from lane/process to flow nodes."""
        try:
            all_flow_nodes = self._flow_nodes_by_process.get(process_id, set())
            if not self._process_lanes_info.get(process_id, False):
                for node_id in all_flow_nodes:
                    rel_props: Dict[str, Any] = {}
                    self._attach_common_properties(rel_props, model_key)
                    self.relationships.append({'source': process_id, 'target': node_id, 'type': 'OWNS_NODE', 'properties': rel_props})
                return
            lane_map = self._lane_membership.get(process_id, {})
            assigned_nodes = set()
            for lane_id, node_ids in lane_map.items():
                for node_id in node_ids:
                    if node_id in all_flow_nodes:
                        rel_props = {}
                        self._attach_common_properties(rel_props, model_key)
                        self.relationships.append({'source': lane_id, 'target': node_id, 'type': 'OWNS_NODE', 'properties': rel_props})
                        assigned_nodes.add(node_id)
            unassigned = all_flow_nodes - assigned_nodes
            if unassigned:
                for node_id in unassigned:
                    rel_props = {}
                    self._attach_common_properties(rel_props, model_key)
                    self.relationships.append({'source': process_id, 'target': node_id, 'type': 'OWNS_NODE', 'properties': rel_props})
        except Exception:
            self.logger.exception("[PARSE][LANE] Failed applying lane ownership")
            raise

    def _record_link_event(self, scope_id: str, link_name: str, kind: str, event_id: str):
        """Record link events to connect later."""
        try:
            if not link_name:
                return
            scope_map = self._link_map.setdefault(scope_id, {})
            link_group = scope_map.setdefault(link_name, {'throw': set(), 'catch': set()})
            link_group[kind].add(event_id)
        except Exception:
            self.logger.exception("[PARSE][LINK] Failed recording link event")
            raise

    def _finalize_link_events(self):
        """Create LINK_TO edges between throw/catch link events recorded."""
        try:
            self._rebuild_node_modelkey_index()
            for scope_id, name_map in self._link_map.items():
                for link_name, groups in name_map.items():
                    throw_events = groups.get('throw', set())
                    catch_events = groups.get('catch', set())
                    if not throw_events or not catch_events:
                        continue
                    for throw_id in throw_events:
                        for catch_id in catch_events:
                            model_key = (self._model_key_by_node_id.get(throw_id) or self._model_key_by_node_id.get(catch_id))
                            rel_props = {'linkName': link_name}
                            self._attach_common_properties(rel_props, model_key)
                            self.relationships.append({'source': throw_id, 'target': catch_id, 'type': 'LINK_TO', 'properties': rel_props})
        except Exception:
            self.logger.exception("[PARSE][LINK] Failed finalizing link events")
            raise

    def _rebuild_node_modelkey_index(self):
        """Rebuild an index from node id to modelKey."""
        try:
            self._model_key_by_node_id.clear()
            for node in self.nodes:
                model_key = node.get('properties', {}).get('modelKey')
                self._model_key_by_node_id[node['id']] = model_key
        except Exception:
            self.logger.exception("[PARSE][STATE] Failed rebuilding node→modelKey index")
            raise

    def _collect_associations(self, root: ET.Element) -> List[Dict[str, Any]]:
        """Collect all <association> elements as lightweight tuples."""
        try:
            associations: List[Dict[str, Any]] = []
            for assoc in self._find_by_local_name(root, 'association'):
                source = assoc.get('sourceRef')
                target = assoc.get('targetRef')
                props = self._extract_attributes(assoc)
                associations.append({'sourceRef': source, 'targetRef': target, 'props': props})
            return associations
        except Exception:
            self.logger.exception("[PARSE][ASSOC] Failed collecting associations")
            raise

    def _build_neighbors_map(self, associations: List[Dict[str, Any]], target_ids: List[str]) -> Dict[str, List[str]]:
        """Build neighbor map for a set of target node ids using associations."""
        try:
            neighbors: Dict[str, List[str]] = {tid: [] for tid in target_ids if tid}
            for assoc in associations:
                source, target = assoc['sourceRef'], assoc['targetRef']
                if source in neighbors and target:
                    neighbors[source].append(target)
                if target in neighbors and source:
                    neighbors[target].append(source)
            return neighbors
        except Exception:
            self.logger.exception("[PARSE][ASSOC] Failed building neighbors map")
            raise

    def _pick_model_key_from_neighbors(self, neighbor_ids: List[str]) -> Optional[str]:
        """Pick first available modelKey from neighbor nodes."""
        try:
            for neighbor_id in neighbor_ids:
                model_key = self._model_key_by_node_id.get(neighbor_id)
                if model_key:
                    return model_key
            return None
        except Exception:
            self.logger.exception("[PARSE][STATE] Failed picking model key from neighbors")
            raise

    def _create_annotation_relationships(self, associations: List[Dict[str, Any]], annotations: Dict[str, str]):
        """Create ANNOTATES(source: annotation → target: neighbor) edges."""
        try:
            for assoc in associations:
                source, target, props = assoc['sourceRef'], assoc['targetRef'], assoc['props']
                if source in annotations and target:
                    model_key = (self._model_key_by_node_id.get(source) or self._model_key_by_node_id.get(target))
                    rel_props = dict(props)
                    self._attach_common_properties(rel_props, model_key)
                    self.relationships.append({'source': source, 'target': target, 'type': 'ANNOTATES', 'properties': rel_props})
                elif target in annotations and source:
                    model_key = (self._model_key_by_node_id.get(target) or self._model_key_by_node_id.get(source))
                    rel_props = dict(props)
                    self._attach_common_properties(rel_props, model_key)
                    self.relationships.append({'source': target, 'target': source, 'type': 'ANNOTATES', 'properties': rel_props})
        except Exception:
            self.logger.exception("[PARSE][ANNOT] Failed creating annotation relationships")
            raise

    def _create_group_relationships(self, associations: List[Dict[str, Any]], group_ids: Set[str]):
        """Create GROUPS(source: group → target: neighbor) edges."""
        try:
            for assoc in associations:
                source, target, props = assoc['sourceRef'], assoc['targetRef'], assoc['props']
                if source in group_ids and target:
                    model_key = (self._model_key_by_node_id.get(source) or self._model_key_by_node_id.get(target))
                    rel_props = dict(props)
                    self._attach_common_properties(rel_props, model_key)
                    self.relationships.append({'source': source, 'target': target, 'type': 'GROUPS', 'properties': rel_props})
                elif target in group_ids and source:
                    model_key = (self._model_key_by_node_id.get(target) or self._model_key_by_node_id.get(source))
                    rel_props = dict(props)
                    self._attach_common_properties(rel_props, model_key)
                    self.relationships.append({'source': target, 'target': source, 'type': 'GROUPS', 'properties': rel_props})
        except Exception:
            self.logger.exception("[PARSE][GROUP] Failed creating group relationships")
            raise
