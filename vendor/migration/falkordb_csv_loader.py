#!/usr/bin/env python3
"""FalkorDB CSV Loader

Loads nodes and edges from CSV files in the 'csv_output' folder into FalkorDB.
Uses the falkordb-py library with batch processing and proper error handling.

Optional: can provision a Redis ACL user for accessing the migrated graph(s).
"""

import os
import re
import csv
import json
import argparse
import sys
from datetime import datetime
from typing import Dict, List, Any, Optional, Set
from falkordb import FalkorDB
INT64_MIN = -(2 ** 63)
INT64_MAX = (2 ** 63) - 1


class FalkorDBCSVLoader:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        graph_name: str = "graph",
        csv_dir: str = "csv_output",
        username: str = None,
        password: str = None,
        merge_mode: bool = False,
        multi_graph_mode: bool = False,
        debug: bool = False,
    ):
        """
        Initialize FalkorDB connection
        
        :param host: FalkorDB host
        :param port: FalkorDB port  
        :param graph_name: Target graph name (used as prefix in multi-graph mode)
        :param csv_dir: Directory containing CSV files
        :param username: FalkorDB username (optional)
        :param password: FalkorDB password (optional)
        :param merge_mode: If True, use MERGE instead of CREATE for upsert behavior
        :param multi_graph_mode: If True, load each tenant subfolder into separate graphs
        :param debug: If True, print debug messages before each request sent to FalkorDB
        """
        self.host = host
        self.port = port
        self.graph_name = graph_name
        self.csv_dir = csv_dir
        self.merge_mode = merge_mode
        self.multi_graph_mode = multi_graph_mode
        self.debug = bool(debug)
        self.active_graph_name: Optional[str] = None
        self.node_type_schema = {}
        self.relationship_type_schema = {}
        self._load_type_schema()
        
        try:
            print(f"Connecting to FalkorDB at {host}:{port}...")
            self.db = FalkorDB(host=host, port=port, username=username, password=password)
            
            if not multi_graph_mode:
                self.graph = self.db.select_graph(graph_name)
                self.active_graph_name = graph_name
                print(f"✅ Connected to FalkorDB graph '{graph_name}'")
            else:
                self.graph = None  # Will be set per tenant
                print("✅ Connected to FalkorDB in multi-graph mode")
        except Exception as e:
            print(f"❌ Failed to connect to FalkorDB: {e}")
            sys.exit(1)

    @staticmethod
    def _query_preview(query: str, limit: int = 220) -> str:
        compact = " ".join(str(query).split())
        if len(compact) > limit:
            return compact[: limit - 3] + "..."
        return compact

    def _debug_log(self, message: str) -> None:
        if not self.debug:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [DEBUG] {message}")

    def _graph_query(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        phase: str,
    ):
        graph_for_log = self.active_graph_name or self.graph_name
        preview = self._query_preview(query)
        if params is None:
            self._debug_log(
                f"{phase}: sending GRAPH.QUERY to '{graph_for_log}' and waiting for server response | query={preview}"
            )
            return self.graph.query(query)

        params_info = ""
        if isinstance(params, dict):
            params_info = f", params_keys={list(params.keys())}"
            if "batch" in params and isinstance(params["batch"], list):
                params_info += f", batch_size={len(params['batch'])}"
        self._debug_log(
            f"{phase}: sending GRAPH.QUERY to '{graph_for_log}' and waiting for server response{params_info} | query={preview}"
        )
        return self.graph.query(query, params)

    def _execute_command(self, *command_args, phase: str, db_client=None):
        if command_args:
            command_preview = " ".join(str(arg) for arg in command_args[:8])
            if len(command_args) > 8:
                command_preview += " ..."
        else:
            command_preview = "<empty command>"
        self._debug_log(
            f"{phase}: sending command to FalkorDB and waiting for server response | command={command_preview}"
        )
        target_db = db_client or self.db
        return target_db.execute_command(*command_args)

    def _schema_candidate_paths(self) -> List[str]:
        """Return candidate locations for property type schema sidecar."""
        normalized_csv_dir = os.path.normpath(self.csv_dir)
        candidates = [os.path.join(normalized_csv_dir, 'property_types.json')]
        parent_dir = os.path.dirname(normalized_csv_dir)
        if parent_dir and parent_dir != normalized_csv_dir:
            candidates.append(os.path.join(parent_dir, 'property_types.json'))
        return candidates

    def _load_type_schema(self):
        """Load optional schema with declared property types per label/relationship."""
        self.node_type_schema = {}
        self.relationship_type_schema = {}

        for schema_path in self._schema_candidate_paths():
            if not os.path.exists(schema_path):
                continue
            try:
                with open(schema_path, 'r', encoding='utf-8') as schema_file:
                    schema_data = json.load(schema_file)

                node_schema = schema_data.get('nodes', {})
                relationship_schema = schema_data.get('relationships', {})

                if isinstance(node_schema, dict):
                    self.node_type_schema = {
                        str(label): {
                            str(prop): str(prop_type).lower()
                            for prop, prop_type in props.items()
                        }
                        for label, props in node_schema.items()
                        if isinstance(props, dict)
                    }

                if isinstance(relationship_schema, dict):
                    self.relationship_type_schema = {
                        str(rel_type): {
                            str(prop): str(prop_type).lower()
                            for prop, prop_type in props.items()
                        }
                        for rel_type, props in relationship_schema.items()
                        if isinstance(props, dict)
                    }

                print(f"✅ Loaded property type schema from {schema_path}")
                return
            except Exception as e:
                print(f"⚠️ Failed to load property type schema {schema_path}: {e}")

    @staticmethod
    def _is_integer_literal(value: str) -> bool:
        if not isinstance(value, str) or not value:
            return False
        if value.startswith('-'):
            return value[1:].isdigit()
        return value.isdigit()

    @staticmethod
    def _is_float_literal(value: str) -> bool:
        if not isinstance(value, str) or not value:
            return False
        return '.' in value and value.replace('.', '', 1).lstrip('-').isdigit()

    @staticmethod
    def _parse_int64(value: str) -> Optional[int]:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        if parsed < INT64_MIN or parsed > INT64_MAX:
            return None
        return parsed

    @staticmethod
    def _to_float_vector(values: List[Any]) -> Optional[List[float]]:
        vector = []
        for item in values:
            if item is None:
                return None
            text = str(item).strip()
            if text == '':
                return None
            try:
                vector.append(float(text))
            except (TypeError, ValueError):
                return None
        return vector
    @staticmethod
    def _looks_like_vector_property_name(property_name: Optional[str]) -> bool:
        if not isinstance(property_name, str):
            return False
        normalized = property_name.strip().lower()
        if not normalized:
            return False
        return (
            normalized == 'embedding'
            or normalized == 'document_embedding'
            or normalized.endswith('_embedding')
            or normalized.startswith('embedding_')
        )

    def _parse_vector_literal(self, value: Any) -> Any:
        """Parse serialized vector values (e.g. 1;2;3 or [1,2,3]) into float arrays."""
        if isinstance(value, (list, tuple)):
            parsed_vector = self._to_float_vector(list(value))
            return parsed_vector if parsed_vector is not None else value

        if not isinstance(value, str):
            return value

        text = value.strip()
        if not text:
            return value

        if text.startswith('[') and text.endswith(']'):
            try:
                json_value = json.loads(text)
            except Exception:
                json_value = None
            if isinstance(json_value, list):
                parsed_vector = self._to_float_vector(json_value)
                if parsed_vector is not None:
                    return parsed_vector

        delimiter = ';' if ';' in text else (',' if ',' in text else None)
        if delimiter is None:
            return value

        items = [part.strip() for part in text.split(delimiter)]
        parsed_vector = self._to_float_vector(items)
        return parsed_vector if parsed_vector is not None else value
    @staticmethod
    def _parse_list_literal(value: Any) -> Any:
        """Parse JSON list literals while leaving non-list values untouched."""
        if isinstance(value, list):
            return value
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text or not (text.startswith('[') and text.endswith(']')):
            return value
        try:
            parsed = json.loads(text)
        except Exception:
            return value
        return parsed if isinstance(parsed, list) else value

    def _coerce_scalar(
        self,
        value: str,
        declared_type: Optional[str] = None,
        property_name: Optional[str] = None,
    ) -> Any:
        """Coerce CSV value using declared type when available, with safe fallback."""
        if value is None or value == '':
            return None

        normalized_type = declared_type.lower() if isinstance(declared_type, str) else None

        if normalized_type == 'string':
            return value
        if normalized_type == 'integer':
            parsed = self._parse_int64(value)
            return parsed if parsed is not None else value
        if normalized_type == 'float':
            try:
                return float(value)
            except (TypeError, ValueError):
                return value
        if normalized_type == 'boolean':
            lowered = value.lower()
            if lowered in ('true', '1'):
                return True
            if lowered in ('false', '0'):
                return False
            return value
        if normalized_type == 'list':
            return self._parse_list_literal(value)
        if normalized_type == 'vector':
            return self._parse_vector_literal(value)

        parsed_vector = self._parse_vector_literal(value)
        if isinstance(parsed_vector, list):
            if self._looks_like_vector_property_name(property_name):
                return parsed_vector
            if isinstance(value, str) and ';' in value:
                return parsed_vector

        # Backward-compatible fallback when schema is unavailable.
        if self._is_integer_literal(value):
            parsed = self._parse_int64(value)
            return parsed if parsed is not None else value
        if self._is_float_literal(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return value
        return value

    def _coerce_identifier(self, value: str) -> Any:
        """Coerce IDs to int64 when safe; keep as string otherwise."""
        if self._is_integer_literal(value):
            parsed = self._parse_int64(value)
            if parsed is not None:
                return parsed
        return value

    @staticmethod
    def _is_vector_declared_type(declared_type: Optional[str]) -> bool:
        return isinstance(declared_type, str) and declared_type.lower() == 'vector'

    @staticmethod
    def _parse_json_object(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if value in (None, ''):
            return {}
        if not isinstance(value, str):
            return {}
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _escape_cypher_identifier(identifier: str) -> str:
        safe_identifier = str(identifier).replace('`', '``')
        return f"`{safe_identifier}`"

    @staticmethod
    def _escape_cypher_string_literal(value: str) -> str:
        return str(value).replace('\\', '\\\\').replace("'", "\\'")

    @staticmethod
    def _first_present(mapping: Dict[str, Any], keys: List[str]) -> Any:
        for key in keys:
            if key in mapping and mapping[key] not in (None, ''):
                return mapping[key]
        return None
    @staticmethod
    def _unwrap_first_value(value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return value[0] if value else None
        return value

    @staticmethod
    def _to_positive_int(value: Any) -> Optional[int]:
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        try:
            cast_value = int(value)
        except (TypeError, ValueError):
            return None
        return cast_value if cast_value > 0 else None

    def _infer_vector_dimension_from_csv(
        self,
        label_or_type: str,
        property_name: str,
        entity_type: str,
    ) -> Optional[int]:
        filename_prefix = 'edges_' if str(entity_type).upper() == 'RELATIONSHIP' else 'nodes_'
        candidate_filename = f"{filename_prefix}{label_or_type}.csv"
        candidate_path = os.path.join(self.csv_dir, candidate_filename)
        if not os.path.exists(candidate_path):
            return None

        try:
            with open(candidate_path, 'r', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    raw_value = row.get(property_name)
                    if not raw_value:
                        continue
                    parsed_value = self._parse_vector_literal(raw_value)
                    if isinstance(parsed_value, list) and parsed_value:
                        return len(parsed_value)
        except Exception:
            return None

        return None

    def _extract_vector_index_options(
        self,
        index: Dict[str, Any],
        label_or_type: str,
        property_name: str,
    ) -> Optional[Dict[str, Any]]:
        options_payload = self._parse_json_object(index.get('options'))
        provider_payload = self._parse_json_object(index.get('provider'))

        merged = {}
        candidate_maps = []
        if isinstance(options_payload.get('indexConfig'), dict):
            candidate_maps.append(options_payload.get('indexConfig', {}))
        candidate_maps.append(options_payload)
        candidate_maps.append(provider_payload)

        for candidate in candidate_maps:
            if not isinstance(candidate, dict):
                continue
            for key, value in candidate.items():
                if key not in merged and value not in (None, ''):
                    merged[key] = value

        dimension_value = self._first_present(
            merged,
            ['vector.dimensions', 'vector.dimension', 'dimensions', 'dimension', 'dim'],
        )
        similarity_value = self._first_present(
            merged,
            [
                'vector.similarity_function',
                'vector.similarityFunction',
                'similarityFunction',
                'similarity_function',
                'distance',
                'metric',
            ],
        )

        dimension = self._to_positive_int(dimension_value)
        if dimension is None:
            dimension = self._infer_vector_dimension_from_csv(
                label_or_type=label_or_type,
                property_name=property_name,
                entity_type=str(index.get('entity_type', 'NODE')),
            )

        similarity_value = self._unwrap_first_value(similarity_value)
        if dimension is None:
            return None
        if similarity_value is None:
            similarity_value = 'cosine'

        similarity = str(similarity_value).strip().lower()
        if similarity in ('l2', 'euclid', 'euclidian'):
            similarity = 'euclidean'
        if similarity not in ('cosine', 'euclidean'):
            return None

        vector_options = {
            'dimension': dimension,
            'similarityFunction': similarity,
        }

        m_value = self._to_positive_int(self._first_present(merged, ['vector.hnsw.m', 'M', 'm']))
        ef_construction_value = self._to_positive_int(
            self._first_present(merged, ['vector.hnsw.ef_construction', 'efConstruction', 'ef_construction'])
        )
        ef_runtime_value = self._to_positive_int(
            self._first_present(merged, ['vector.hnsw.ef_runtime', 'efRuntime', 'ef_runtime'])
        )

        if m_value is not None:
            vector_options['M'] = m_value
        if ef_construction_value is not None:
            vector_options['efConstruction'] = ef_construction_value
        if ef_runtime_value is not None:
            vector_options['efRuntime'] = ef_runtime_value

        return vector_options

    def _build_vector_index_query(
        self,
        index: Dict[str, Any],
        label_or_type: str,
        property_name: str,
    ) -> Optional[str]:
        vector_options = self._extract_vector_index_options(index, label_or_type, property_name)
        if not vector_options:
            return None

        label_or_type_identifier = self._escape_cypher_identifier(label_or_type)
        property_identifier = self._escape_cypher_identifier(property_name)
        entity_type = str(index.get('entity_type', 'NODE')).upper()

        option_parts = [
            f"dimension:{vector_options['dimension']}",
            f"similarityFunction:'{vector_options['similarityFunction']}'",
        ]
        if 'M' in vector_options:
            option_parts.append(f"M:{vector_options['M']}")
        if 'efConstruction' in vector_options:
            option_parts.append(f"efConstruction:{vector_options['efConstruction']}")
        if 'efRuntime' in vector_options:
            option_parts.append(f"efRuntime:{vector_options['efRuntime']}")
        options_clause = ', '.join(option_parts)

        if entity_type == 'RELATIONSHIP':
            return (
                f"CREATE VECTOR INDEX FOR ()-[r:{label_or_type_identifier}]->() "
                f"ON (r.{property_identifier}) OPTIONS {{{options_clause}}}"
            )
        return (
            f"CREATE VECTOR INDEX FOR (n:{label_or_type_identifier}) "
            f"ON (n.{property_identifier}) OPTIONS {{{options_clause}}}"
        )

    def _build_vector_set_clauses(
        self,
        entity_alias: str,
        map_expression: str,
        vector_property_names: Set[str],
    ) -> str:
        clauses = []
        for property_name in sorted(vector_property_names):
            if not property_name:
                continue
            key_literal = self._escape_cypher_string_literal(property_name)
            property_identifier = self._escape_cypher_identifier(property_name)
            clauses.append(
                f"FOREACH (_ IN CASE WHEN {map_expression}['{key_literal}'] IS NULL OR size({map_expression}['{key_literal}']) = 0 THEN [] ELSE [1] END | "
                f"SET {entity_alias}.{property_identifier} = vecf32({map_expression}['{key_literal}']))"
            )
        return ' '.join(clauses)
    
    def read_csv_file(self, file_path: str) -> List[Dict[str, Any]]:
        """Read CSV file and return list of dictionaries"""
        rows = []
        try:
            with open(file_path, 'r', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    rows.append(row)
            print(f"  Read {len(rows)} rows from {file_path}")
            return rows
        except Exception as e:
            print(f"❌ Error reading {file_path}: {e}")
            return []
    
    def create_id_indexes_for_all_labels(self):
        """Create index on 'id' property for each node label found in CSV files"""
        if not os.path.exists(self.csv_dir):
            return
        
        print("🔧 Creating ID indexes for all node labels...")
        
        # Find all node CSV files to determine labels
        csv_files = os.listdir(self.csv_dir)
        node_files = [f for f in csv_files if f.startswith('nodes_') and f.endswith('.csv')]
        
        labels = set()
        for node_file in node_files:
            file_path = os.path.join(self.csv_dir, node_file)
            # Prefer labels column (supports multi-label nodes); fall back to filename tokens.
            try:
                with open(file_path, 'r', encoding='utf-8') as handle:
                    reader = csv.DictReader(handle)
                    for i, row in enumerate(reader):
                        raw = (row.get('labels') or '').strip()
                        if raw:
                            for label in raw.split(':'):
                                label = label.strip()
                                if label:
                                    labels.add(label)
                        if i >= 50:
                            break
            except Exception:
                pass
            raw_label = node_file.replace('nodes_', '').replace('.csv', '')
            for part in raw_label.replace('__', ':').split(':'):
                part = part.strip()
                if part and not re.fullmatch(r'[0-9a-fA-F]{8,10}', part):
                    labels.add(part)

        # Also create a generic index-free path is not available; ensure each concrete label is indexed.
        created_count = 0
        for label in sorted(labels):
            # Labels may contain special characters; escape them.
            label_ident = self._escape_cypher_identifier(label)
            try:
                query = f"CREATE INDEX ON :{label_ident}(id)"
                print(f"  Creating ID index: {query}")
                self._graph_query(
                    query,
                    phase=f"schema:id-index:{label}",
                )
                created_count += 1
            except Exception as e:
                error_msg = str(e).lower()
                if any(keyword in error_msg for keyword in ['already exists', 'equivalent', 'already indexed', 'index exists']):
                    pass
                else:
                    print(f"  ❌ Error creating ID index on {label}.id: {e}")
        
        if created_count > 0:
            print(f"✅ Created {created_count} ID indexes")
        else:
            print("  No new ID indexes created")
    
    def create_indexes_from_csv(self):
        """Create indexes from indexes.csv file, but skip unique constraints"""
        indexes_file = os.path.join(self.csv_dir, 'indexes.csv')
        if not os.path.exists(indexes_file):
            print("⚠️ No indexes.csv file found, skipping index creation")
            return
        
        print("🔧 Creating indexes from CSV...")
        indexes = self.read_csv_file(indexes_file)
        
        created_count = 0
        skipped_count = 0
        
        for index in indexes:
            labels = index.get('labels', '').strip()
            properties = index.get('properties', '').strip()
            uniqueness = index.get('uniqueness', 'NON_UNIQUE')
            index_type = index.get('type', '').upper()
            
            # Skip system indexes, unique constraints, and indexes without labels/properties
            if not labels or not properties or index_type == 'LOOKUP' or uniqueness == 'UNIQUE':
                skipped_count += 1
                continue
            
            # Split labels and properties (in case there are multiple)
            label_list = [lbl.strip() for lbl in labels.split(';') if lbl.strip()]
            prop_list = [p.strip() for p in properties.split(';') if p.strip()]
            
            # Create index for each label-property combination
            for label in label_list:
                for prop in prop_list:
                    try:
                        if index_type == 'VECTOR':
                            query = self._build_vector_index_query(index, label, prop)
                            if not query:
                                print(
                                    f"  ⚠️ Skipping vector index {label}.{prop}: missing/unsupported vector options"
                                )
                                skipped_count += 1
                                continue
                        else:
                            # Create regular index
                            query = f"CREATE INDEX ON :{label}({prop})"
                        print(f"  Creating: {query}")
                        self._graph_query(
                            query,
                            phase=f"schema:index:{label}.{prop}",
                        )
                        created_count += 1
                        
                    except Exception as e:
                        error_msg = str(e).lower()
                        if any(keyword in error_msg for keyword in ['already exists', 'equivalent', 'already indexed', 'index exists']):
                            # Silently skip - index already exists, which is what we want
                            pass
                        else:
                            print(f"  ❌ Error creating index on {label}.{prop}: {e}")
        
        print(f"✅ Created {created_count} indexes from CSV, skipped {skipped_count}")
    
    def create_supporting_indexes_for_constraints(self):
        """Create supporting indexes required for unique constraints"""
        constraints_file = os.path.join(self.csv_dir, 'constraints.csv')
        if not os.path.exists(constraints_file):
            return
        
        print("🔧 Creating supporting indexes for constraints...")
        constraints = self.read_csv_file(constraints_file)
        
        if not constraints:
            return
        
        created_count = 0
        
        for constraint in constraints:
            labels = constraint.get('labels', '').strip()
            properties = constraint.get('properties', '').strip()
            constraint_type = constraint.get('type', '').upper()
            
            # Only create indexes for UNIQUE constraints
            if not labels or not properties or 'UNIQUE' not in constraint_type:
                continue
            
            # Split labels and properties
            label_list = [lbl.strip() for lbl in labels.split(';') if lbl.strip()]
            prop_list = [p.strip() for p in properties.split(';') if p.strip()]
            
            # Create supporting index for each label
            for label in label_list:
                try:
                    # Create index with all properties required for the constraint
                    if len(prop_list) == 1:
                        query = f"CREATE INDEX FOR (n:{label}) ON (n.{prop_list[0]})"
                    else:
                        prop_str = ', '.join([f'n.{prop}' for prop in prop_list])
                        query = f"CREATE INDEX FOR (n:{label}) ON ({prop_str})"
                    
                    print(f"  Creating supporting index: {query}")
                    self._graph_query(
                        query,
                        phase=f"schema:supporting-index:{label}",
                    )
                    created_count += 1
                    
                except Exception as e:
                    error_msg = str(e).lower()
                    if any(keyword in error_msg for keyword in ['already indexed', 'already exists', 'equivalent', 'index exists']):
                        # Silently skip - supporting index already exists, which is what we want
                        pass
                    else:
                        print(f"  ❌ Error creating supporting index for {label}({', '.join(prop_list)}): {e}")
        
        if created_count > 0:
            print(f"✅ Created {created_count} supporting indexes")
    
    def create_constraints_from_csv(self):
        """Create constraints from constraints.csv file"""
        constraints_file = os.path.join(self.csv_dir, 'constraints.csv')
        if not os.path.exists(constraints_file):
            print("⚠️ No constraints.csv file found, skipping constraint creation")
            return
        
        print("🔒 Creating constraints...")
        constraints = self.read_csv_file(constraints_file)
        
        if not constraints:
            print("  No constraints to create")
            return
        
        created_count = 0
        skipped_count = 0
        
        for constraint in constraints:
            labels = constraint.get('labels', '').strip()
            properties = constraint.get('properties', '').strip()
            constraint_type = constraint.get('type', '').upper()
            entity_type = constraint.get('entity_type', 'NODE').upper()

            # Skip constraints without labels/properties
            if not labels or not properties:
                skipped_count += 1
                continue
            
            # Split labels and properties (in case there are multiple)
            label_list = [lbl.strip() for lbl in labels.split(';') if lbl.strip()]
            prop_list = [p.strip() for p in properties.split(';') if p.strip()]
            
            # Create constraint for each label-property combination
            for label in label_list:
                # FalkorDB's create_constraint function expects a list of properties
                try:
                    if 'UNIQUE' in constraint_type:
                        # Create unique constraint using Redis command
                        # GRAPH.CONSTRAINT CREATE key UNIQUE NODE label PROPERTIES propCount prop [prop...]
                        command_args = [
                            'GRAPH.CONSTRAINT', 'CREATE', self.graph_name, 'UNIQUE', 
                            entity_type, label, 'PROPERTIES', str(len(prop_list))
                        ] + prop_list
                        
                        result = self._execute_command(
                            *command_args,
                            phase=f"schema:constraint:{label}",
                        )
                        created_count += 1
                        print(f"  ✅ Successfully created UNIQUE constraint on {label}({', '.join(prop_list)}), status: {result}")
                    else:
                        # Handle other constraint types if necessary (e.g., MANDATORY)
                        print(f"  ⚠️ Constraint type '{constraint_type}' not supported by this loader, skipping {label}.{prop_list}")
                        skipped_count += 1
                except Exception as e:
                    error_msg = str(e).lower()
                    if 'already exists' in error_msg or 'constraint already exists' in error_msg:
                        print(f"  ⚠️ Constraint on {label}({', '.join(prop_list)}) already exists, skipping")
                    else:
                        print(f"  ❌ Error creating constraint on {label}({', '.join(prop_list)}): {e}")
                        skipped_count += 1
        
        if created_count > 0:
            print(f"✅ Created {created_count} constraints")
        if skipped_count > 0:
            print(f"⚠️ Skipped {skipped_count} constraints")
    

    def _parse_labels_field(self, raw_labels: str, fallback_label: str = '') -> List[str]:
        """Parse colon-separated multi-label field into concrete labels."""
        labels = []
        for part in str(raw_labels or '').split(':'):
            label = part.strip()
            if label and label not in labels:
                labels.append(label)
        if not labels and fallback_label:
            # Filename may encode multi-label as LabelA__LabelB
            for part in fallback_label.replace('__', ':').split(':'):
                label = part.strip()
                # Drop trailing hash suffixes produced by safe_filename_component when present
                if re.search(r'_[0-9a-fA-F]{8,10}$', label):
                    maybe = re.sub(r'_[0-9a-fA-F]{8,10}$', '', label)
                    if maybe:
                        label = maybe
                if label and label not in labels:
                    labels.append(label)
        return labels or ['Node']

    def load_nodes_batch(self, file_path: str, batch_size: int = 5000):
        """Load nodes from CSV file in batches (multi-label aware)."""
        start_time = datetime.now()
        print(f"[{start_time.strftime('%Y-%m-%d %H:%M:%S')}] Loading nodes from {file_path}...")
        
        filename = os.path.basename(file_path)
        raw_label = filename.replace('nodes_', '').replace('.csv', '')
        rows = self.read_csv_file(file_path)
        if not rows:
            return
        
        if rows:
            print(f"  CSV headers: {list(rows[0].keys())}")
        
        total_loaded = 0
        
        for i in range(0, len(rows), batch_size):
            batch_start_time = datetime.now()
            batch = rows[i:i + batch_size]

            # Group rows by exact multi-label set so each UNWIND query can use a fixed label pattern.
            grouped = {}
            for j, row in enumerate(batch):
                node_id = row.get('id', '')
                if node_id == '' or node_id is None:
                    continue
                label_list = self._parse_labels_field(row.get('labels', ''), raw_label)
                label_key = tuple(label_list)

                properties = {}
                vector_property_keys = set()
                for key, value in row.items():
                    if key in ('id', 'labels') or value == '':
                        continue
                    clean_key = key
                    if ':' in key:
                        parts = key.split(':')
                        if len(parts) == 2 and parts[0] == parts[1]:
                            clean_key = parts[0]
                    # Prefer schema from any label on the node
                    declared_type = None
                    for label in label_list:
                        declared_type = self.node_type_schema.get(label, {}).get(clean_key)
                        if declared_type:
                            break
                    if declared_type is None:
                        declared_type = self.node_type_schema.get(raw_label, {}).get(clean_key)
                    coerced_value = self._coerce_scalar(value, declared_type, clean_key)
                    properties[clean_key] = coerced_value
                    if isinstance(coerced_value, list) and (
                        self._is_vector_declared_type(declared_type)
                        or self._looks_like_vector_property_name(clean_key)
                    ):
                        vector_property_keys.add(clean_key)

                if i == 0 and j < 3:
                    print(f"    Record {j+1}: labels={label_list}, id={node_id}")

                grouped.setdefault(label_key, {'rows': [], 'vector_keys': set()})
                grouped[label_key]['rows'].append({
                    'id': self._coerce_identifier(str(node_id)),
                    'props': properties,
                })
                grouped[label_key]['vector_keys'] |= vector_property_keys

            for label_key, payload in grouped.items():
                batch_data = payload['rows']
                if not batch_data:
                    continue
                label_pattern = ':'.join(self._escape_cypher_identifier(lbl) for lbl in label_key)
                label_set_clause = ' '.join(
                    f"SET n:{self._escape_cypher_identifier(lbl)}" for lbl in label_key
                )
                vector_set_clause = self._build_vector_set_clauses('n', 'row.props', payload['vector_keys'])
                fallback_vector_set_clause = self._build_vector_set_clauses('n', '$props', payload['vector_keys'])
                try:
                    if self.merge_mode:
                        # Merge on id only, then ensure all labels exist on the same node.
                        unwind_query = (
                            f"UNWIND $batch AS row "
                            f"MERGE (n {{id: row.id}}) "
                            f"{label_set_clause} "
                            f"SET n += row.props"
                        )
                    else:
                        unwind_query = (
                            f"UNWIND $batch AS row "
                            f"CREATE (n:{label_pattern}) "
                            f"SET n.id = row.id, n += row.props"
                        )
                    if vector_set_clause:
                        unwind_query = f"{unwind_query} {vector_set_clause}"
                    self._graph_query(
                        unwind_query,
                        {'batch': batch_data},
                        phase=f"nodes:batch:{'|'.join(label_key)}",
                    )
                    total_loaded += len(batch_data)
                except Exception as e:
                    print(f"❌ Error loading batch for labels {label_key}: {e}")
                    print("Falling back to individual queries for this batch...")
                    for row_payload in batch_data:
                        try:
                            if self.merge_mode:
                                fallback_query = (
                                    f"MERGE (n {{id: $id}}) "
                                    f"{label_set_clause} "
                                    f"SET n += $props"
                                )
                            else:
                                fallback_query = (
                                    f"CREATE (n:{label_pattern}) "
                                    f"SET n.id = $id, n += $props"
                                )
                            if fallback_vector_set_clause:
                                fallback_query = f"{fallback_query} {fallback_vector_set_clause}"
                            self._graph_query(
                                fallback_query,
                                {'id': row_payload['id'], 'props': row_payload['props']},
                                phase=f"nodes:fallback-single:{'|'.join(label_key)}",
                            )
                            total_loaded += 1
                        except Exception as e2:
                            print(f"❌ Error loading node: {e2}")
                            print(f"Node payload: {row_payload}")
            
            batch_end_time = datetime.now()
            batch_duration = batch_end_time - batch_start_time
            print(f"[{batch_end_time.strftime('%Y-%m-%d %H:%M:%S')}] Batch complete: Loaded {len(batch)} nodes (Duration: {batch_duration})")
        
        end_time = datetime.now()
        duration = end_time - start_time
        print(f"[{end_time.strftime('%Y-%m-%d %H:%M:%S')}] ✅ Loaded {total_loaded} nodes from {filename} (Duration: {duration})")


    def _resolve_relationship_type(self, file_path: str, rows: List[Dict[str, Any]]) -> str:
        """Resolve relationship type from CSV content or sidecar mapping, not only filename."""
        filename = os.path.basename(file_path)
        # 1) Prefer explicit type column values
        for row in rows[:20]:
            rel_type = (row.get('type') or '').strip()
            if rel_type:
                return rel_type
        # 2) Sidecar written by extractor
        mapping_path = os.path.join(os.path.dirname(file_path), 'relationship_type_files.json')
        if os.path.exists(mapping_path):
            try:
                with open(mapping_path, 'r', encoding='utf-8') as handle:
                    mapping = json.load(handle) or {}
                entry = mapping.get(filename) or {}
                if entry.get('mapped_type'):
                    return str(entry['mapped_type'])
                if entry.get('original_type'):
                    return str(entry['original_type'])
            except Exception:
                pass
        # 3) Filename fallback
        return filename.replace('edges_', '').replace('.csv', '')

    def load_edges_batch(self, file_path: str, batch_size: int = 5000):
        """Load edges from CSV file in batches (endpoint match by id; multi-label safe)."""
        start_time = datetime.now()
        print(f"[{start_time.strftime('%Y-%m-%d %H:%M:%S')}] Loading edges from {file_path}...")

        rows = self.read_csv_file(file_path)
        if not rows:
            return

        rel_type = self._resolve_relationship_type(file_path, rows)
        rel_type_ident = self._escape_cypher_identifier(rel_type)
        relationship_property_types = self.relationship_type_schema.get(rel_type, {})
        # Also try original filename key for schema
        filename = os.path.basename(file_path)
        file_key = filename.replace('edges_', '').replace('.csv', '')
        if not relationship_property_types:
            relationship_property_types = self.relationship_type_schema.get(file_key, {})

        print(f"  Relationship type: {rel_type}")
        total_loaded = 0
        vector_property_keys = set()
        for property_name, declared_type in relationship_property_types.items():
            if not self._is_vector_declared_type(declared_type):
                continue
            vector_property_keys.add(property_name)
            if ':' in property_name:
                parts = property_name.split(':')
                if len(parts) == 2 and parts[0] == parts[1]:
                    vector_property_keys.add(parts[0])

        for i in range(0, len(rows), batch_size):
            batch_start_time = datetime.now()
            batch = rows[i:i + batch_size]
            batch_data = []
            for j, row in enumerate(batch):
                source_id = row.get('source', '')
                target_id = row.get('target', '')
                if not source_id or not target_id:
                    continue

                properties = {}
                for key, value in row.items():
                    if key in ['source', 'target', 'type', 'source_label', 'target_label'] or not value:
                        continue
                    clean_key = key
                    if ':' in key:
                        parts = key.split(':')
                        if len(parts) == 2 and parts[0] == parts[1]:
                            clean_key = parts[0]
                    declared_type = relationship_property_types.get(key) or relationship_property_types.get(clean_key)
                    coerced_value = self._coerce_scalar(value, declared_type, clean_key)
                    properties[clean_key] = coerced_value
                    if isinstance(coerced_value, list) and (
                        self._is_vector_declared_type(declared_type)
                        or self._looks_like_vector_property_name(clean_key)
                    ):
                        vector_property_keys.add(clean_key)

                if i == 0 and j < 3:
                    print(
                        f"    Record {j+1}: source={source_id}, target={target_id}, "
                        f"source_label={row.get('source_label','')}, target_label={row.get('target_label','')}"
                    )

                batch_data.append({
                    'source_id': self._coerce_identifier(source_id),
                    'target_id': self._coerce_identifier(target_id),
                    'props': properties,
                })

            if not batch_data:
                continue

            vector_set_clause = self._build_vector_set_clauses('r', 'row.props', vector_property_keys)
            fallback_vector_set_clause = self._build_vector_set_clauses('r', '$props', vector_property_keys)

            try:
                # Always match endpoints by migrated id. This is multi-label safe and avoids
                # dropping edges when source/target nodes carry multiple labels.
                if self.merge_mode:
                    unwind_query = (
                        f"UNWIND $batch AS row "
                        f"MATCH (a {{id: row.source_id}}) "
                        f"MATCH (b {{id: row.target_id}}) "
                        f"MERGE (a)-[r:{rel_type_ident}]->(b) "
                        f"SET r += row.props"
                    )
                else:
                    unwind_query = (
                        f"UNWIND $batch AS row "
                        f"MATCH (a {{id: row.source_id}}) "
                        f"MATCH (b {{id: row.target_id}}) "
                        f"CREATE (a)-[r:{rel_type_ident}]->(b) "
                        f"SET r += row.props"
                    )
                if vector_set_clause:
                    unwind_query = f"{unwind_query} {vector_set_clause}"

                self._graph_query(
                    unwind_query,
                    {'batch': batch_data},
                    phase=f"edges:batch:{rel_type}",
                )
                total_loaded += len(batch_data)
            except Exception as e:
                print(f"❌ Error loading batch: {e}")
                print("Falling back to individual queries for this batch...")
                for row_payload in batch_data:
                    try:
                        if self.merge_mode:
                            fallback_query = (
                                f"MATCH (a {{id: $source_id}}) "
                                f"MATCH (b {{id: $target_id}}) "
                                f"MERGE (a)-[r:{rel_type_ident}]->(b) "
                                f"SET r += $props"
                            )
                        else:
                            fallback_query = (
                                f"MATCH (a {{id: $source_id}}), (b {{id: $target_id}}) "
                                f"CREATE (a)-[r:{rel_type_ident}]->(b) "
                                f"SET r += $props"
                            )
                        if fallback_vector_set_clause:
                            fallback_query = f"{fallback_query} {fallback_vector_set_clause}"
                        self._graph_query(
                            fallback_query,
                            {
                                'source_id': row_payload['source_id'],
                                'target_id': row_payload['target_id'],
                                'props': row_payload['props'],
                            },
                            phase=f"edges:fallback-single:{rel_type}",
                        )
                        total_loaded += 1
                    except Exception as e2:
                        print(f"❌ Error loading edge: {e2}")
                        print(f"Edge payload: {row_payload}")

            batch_end_time = datetime.now()
            batch_duration = batch_end_time - batch_start_time
            print(f"[{batch_end_time.strftime('%Y-%m-%d %H:%M:%S')}] Batch complete: Loaded {len(batch)} edges (Duration: {batch_duration})")

        end_time = datetime.now()
        duration = end_time - start_time
        print(f"[{end_time.strftime('%Y-%m-%d %H:%M:%S')}] ✅ Loaded {total_loaded} {rel_type} relationships (Duration: {duration})")

    def load_all_csvs(self, batch_size: int = 5000):
        """Load all CSV files from the csv_output directory"""
        if not os.path.exists(self.csv_dir):
            print(f"❌ Directory {self.csv_dir} does not exist")
            return
        
        # Check for multi-graph mode (presence of tenant_* subdirectories)
        if self.multi_graph_mode:
            self._load_multi_graph_csvs(batch_size)
        else:
            self._load_single_graph_csvs(batch_size)
    
    def _load_single_graph_csvs(self, batch_size: int = 5000):
        """Load CSV files into a single graph"""
        csv_files = os.listdir(self.csv_dir)
        node_files = [f for f in csv_files if f.startswith('nodes_') and f.endswith('.csv')]
        edge_files = [f for f in csv_files if f.startswith('edges_') and f.endswith('.csv')]
        
        print(f"Found {len(node_files)} node files and {len(edge_files)} edge files")
        
        # Create indexes and constraints first (for better performance)
        print("\n🗼️ Setting up database schema...")
        self.create_id_indexes_for_all_labels()  # CREATE INDEX ON :Label(id); required before edge MATCH by id
        self.create_indexes_from_csv()
        self.create_supporting_indexes_for_constraints()
        self.create_constraints_from_csv()
        
        # Load nodes first (populate id property), then edges MATCH endpoints by id
        nodes_start_time = datetime.now()
        print(f"\n[{nodes_start_time.strftime('%Y-%m-%d %H:%M:%S')}] 📥 Loading nodes...")
        for node_file in node_files:
            file_path = os.path.join(self.csv_dir, node_file)
            self.load_nodes_batch(file_path, batch_size)
        
        nodes_end_time = datetime.now()
        nodes_duration = nodes_end_time - nodes_start_time
        print(f"[{nodes_end_time.strftime('%Y-%m-%d %H:%M:%S')}] ✅ All nodes loaded (Total duration: {nodes_duration})")
        
        # Then load edges
        edges_start_time = datetime.now()
        print(f"\n[{edges_start_time.strftime('%Y-%m-%d %H:%M:%S')}] 🔗 Loading edges...")
        for edge_file in edge_files:
            file_path = os.path.join(self.csv_dir, edge_file)
            self.load_edges_batch(file_path, batch_size)
        
        edges_end_time = datetime.now()
        edges_duration = edges_end_time - edges_start_time
        print(f"[{edges_end_time.strftime('%Y-%m-%d %H:%M:%S')}] ✅ All edges loaded (Total duration: {edges_duration})")
        
        total_end_time = datetime.now()
        total_duration = total_end_time - nodes_start_time
        print(f"\n[{total_end_time.strftime('%Y-%m-%d %H:%M:%S')}] ✅ Successfully loaded data into graph '{self.graph_name}' (Total loading time: {total_duration})")
    
    def _load_multi_graph_csvs(self, batch_size: int = 5000):
        """Load CSV files from tenant subdirectories into separate graphs"""
        # Find all tenant subdirectories
        subdirs = [d for d in os.listdir(self.csv_dir) 
                   if os.path.isdir(os.path.join(self.csv_dir, d)) and d.startswith('tenant_')]
        
        if not subdirs:
            print(f"⚠️  No tenant subdirectories found in {self.csv_dir}")
            print("   Falling back to single-graph mode...")
            self._load_single_graph_csvs(batch_size)
            return
        
        print(f"\n🗂️  Found {len(subdirs)} tenant directories: {subdirs}")
        print("   Each will be loaded into a separate graph\n")
        
        overall_start_time = datetime.now()
        
        for tenant_dir in sorted(subdirs):
            tenant_path = os.path.join(self.csv_dir, tenant_dir)
            
            # Extract tenant name from directory (remove 'tenant_' prefix)
            tenant_name = tenant_dir.replace('tenant_', '')
            graph_name = f"{self.graph_name}_{tenant_name}"
            
            print(f"\n{'='*80}")
            print(f"📊 Processing tenant: {tenant_name}")
            print(f"   Target graph: {graph_name}")
            print(f"   Source directory: {tenant_path}")
            print(f"{'='*80}\n")
            
            # Switch to this tenant's graph
            self.graph = self.db.select_graph(graph_name)
            self.active_graph_name = graph_name
            
            # Temporarily update csv_dir to point to tenant directory
            original_csv_dir = self.csv_dir
            self.csv_dir = tenant_path
            
            try:
                # Load this tenant's data
                tenant_start_time = datetime.now()
                self._load_single_graph_csvs(batch_size)
                tenant_duration = datetime.now() - tenant_start_time
                print(f"\n✅ Completed loading tenant '{tenant_name}' in {tenant_duration}")
                
            except Exception as e:
                print(f"\n❌ Error loading tenant '{tenant_name}': {e}")
            finally:
                # Restore original csv_dir
                self.csv_dir = original_csv_dir
        
        overall_duration = datetime.now() - overall_start_time
        print(f"\n{'='*80}")
        print("✅ Multi-graph loading complete!")
        print(f"   Loaded {len(subdirs)} tenants into separate graphs")
        print(f"   Total time: {overall_duration}")
        print(f"{'='*80}")
    
    
    def verify_node_attributes(self, label: str = "Person", limit: int = 5):
        """Verify what attributes were loaded for a specific node type"""
        try:
            query = f"MATCH (n:{label}) RETURN n LIMIT {limit}"
            result = self.graph.query(query)
            print(f"\n🔍 Sample {label} nodes with their attributes:")
            for i, record in enumerate(result.result_set):
                node = record[0]
                print(f"  Node {i+1}: {node}")
                
        except Exception as e:
            print(f"❌ Error verifying node attributes: {e}")
    
    def get_graph_stats(self):
        """Get statistics about the loaded graph"""
        try:
            # Count nodes by label
            node_result = self.graph.query("MATCH (n) RETURN labels(n) as labels, count(n) as count")
            print("\n📊 Graph Statistics:")
            print("Nodes:")
            for record in node_result.result_set:
                labels = record[0] if record[0] else ['Unknown']
                count = record[1]
                print(f"  {labels}: {count}")
            
            # Count relationships by type
            rel_result = self.graph.query("MATCH ()-[r]->() RETURN type(r) as type, count(r) as count")
            print("Relationships:")
            for record in rel_result.result_set:
                rel_type = record[0]
                count = record[1]
                print(f"  {rel_type}: {count}")
                
        except Exception as e:
            print(f"❌ Error getting graph statistics: {e}")
    

    def _default_acl_key_pattern(self) -> str:
        """Compute a default Redis ACL key pattern that matches the migrated graph keyspace."""
        # In single-graph mode the graph key is typically the graph name.
        # In multi-graph mode this loader creates graphs like: {graph_name}_{tenant}
        if self.multi_graph_mode:
            return f"{self.graph_name}_*"
        return f"{self.graph_name}*"

    def provision_acl_user(
        self,
        provision_username: str,
        provision_password: str,
        commands_mode: str = "graph-only",
        key_pattern: Optional[str] = None,
        admin_username: Optional[str] = None,
        admin_password: Optional[str] = None,
        lockdown_default_user: bool = False,
    ) -> None:
        """Create or update a Redis ACL user for accessing the migrated graph(s).

        This is idempotent and overwrites any existing user definition (via ACL SETUSER ... reset ...).

        IMPORTANT: creating an ACL user does not block unauthenticated access unless the Redis
        'default' user is disabled or requires a password.
        """

        if not provision_username:
            raise ValueError("provision_username is required")
        if provision_password is None:
            raise ValueError("provision_password is required")

        # Use separate admin connection if provided.
        acl_db = self.db
        if admin_username is not None or admin_password is not None:
            if not admin_username or admin_password is None:
                raise ValueError("Both admin_username and admin_password must be provided")
            acl_db = FalkorDB(
                host=self.host,
                port=self.port,
                username=admin_username,
                password=admin_password,
            )

        raw_pattern = key_pattern.strip() if key_pattern else self._default_acl_key_pattern()
        # Allow users to pass patterns with or without leading '~'.
        if raw_pattern.startswith("~"):
            raw_pattern = raw_pattern[1:]

        if commands_mode not in ("graph-only", "all"):
            raise ValueError("commands_mode must be either 'graph-only' or 'all'")

        if commands_mode == "all":
            command_tokens = ["+@all"]
        else:
            # Minimal set suitable for clients that talk to FalkorDB via Redis.
            # +@connection covers AUTH/HELLO/PING and other connection-level commands.
            command_tokens = [
                "+@connection",
                "+graph.query",
                "+graph.ro_query",
                "+graph.explain",
                "+graph.profile",
                "+graph.list",
            ]

        print(
            "🔐 Provisioning FalkorDB ACL user for graph access "
            f"(user='{provision_username}', key_pattern='~{raw_pattern}', permissions='{commands_mode}')..."
        )

        # Overwrite semantics: reset removes previous passwords/keys/commands.
        setuser_args = [
            "ACL",
            "SETUSER",
            provision_username,
            "reset",
            "on",
            f">{provision_password}",
            f"~{raw_pattern}",
            *command_tokens,
        ]

        try:
            self._execute_command(*setuser_args, phase="acl:setuser", db_client=acl_db)
            # Validate existence (do not print secrets).
            self._execute_command("ACL", "GETUSER", provision_username, phase="acl:getuser", db_client=acl_db)
        except Exception as e:
            raise RuntimeError(f"ACL provisioning failed: {e}")

        if lockdown_default_user:
            print(
                "⚠️  Locking down Redis 'default' user (ACL SETUSER default off). "
                "Make sure you keep the provisioned credentials safe."
            )
            try:
                self._execute_command("ACL", "SETUSER", "default", "off", phase="acl:lockdown-default", db_client=acl_db)
            except Exception as e:
                raise RuntimeError(f"Failed to disable Redis 'default' user: {e}")

        print("✅ ACL user provisioned successfully")

    def close(self):
        """Close the database connection"""
        # FalkorDB doesn't require explicit closing
        pass


def main():
    parser = argparse.ArgumentParser(
        description='Load CSV files into FalkorDB',
        epilog='''
Examples:
  # Load into a single graph
  python3 falkordb_csv_loader.py mygraph

  # Load multi-tenant data into separate graphs
  python3 falkordb_csv_loader.py mygraph --multi-graph

  # This will create graphs: mygraph_tenant1, mygraph_tenant2, etc.
''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('graph_name', help='Target graph name in FalkorDB (used as prefix in multi-graph mode)')
    parser.add_argument('--host', default='localhost', help='FalkorDB host')
    parser.add_argument('--port', type=int, default=6379, help='FalkorDB port')

    # Connection credentials for the loader itself
    parser.add_argument('--username', help='FalkorDB username used for loading (optional)')
    parser.add_argument('--password', help='FalkorDB password used for loading (optional)')

    # Optional: provision an ACL user for post-migration access
    parser.add_argument('--provision-username', help='Create/update a Redis ACL user for accessing the migrated graph(s)')
    parser.add_argument('--provision-password', help='Password for the provisioned ACL user')
    parser.add_argument(
        '--provision-key-pattern',
        help=(
            'Redis ACL key pattern to grant (with or without leading ~). '
            'Default: {graph_name}* (single graph) or {graph_name}_* (multi-graph)'
        ),
    )
    parser.add_argument(
        '--provision-commands',
        choices=['graph-only', 'all'],
        default='graph-only',
        help='ACL permissions mode (default: graph-only)',
    )
    parser.add_argument('--admin-username', help='Admin username to use for ACL provisioning (optional)')
    parser.add_argument('--admin-password', help='Admin password to use for ACL provisioning (optional)')
    parser.add_argument(
        '--lockdown-default-user',
        action='store_true',
        help="Disable Redis 'default' user after provisioning (makes auth required; use with care)",
    )

    parser.add_argument('--batch-size', type=int, default=5000, help='Batch size for loading (default: 5000)')
    parser.add_argument('--stats', action='store_true', help='Show graph statistics after loading')
    parser.add_argument('--csv-dir', default='csv_output', help='Directory containing CSV files (default: csv_output)')
    parser.add_argument('--merge-mode', action='store_true', help='Use MERGE instead of CREATE for upsert behavior')
    parser.add_argument('--multi-graph', action='store_true', help='Enable multi-graph mode: load each tenant_* subfolder into a separate graph')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging (prints every query/command sent to FalkorDB before execution)')

    args = parser.parse_args()

    # Validate provisioning arguments
    if (args.provision_username and not args.provision_password) or (args.provision_password and not args.provision_username):
        parser.error('--provision-username and --provision-password must be provided together')
    if args.lockdown_default_user and not args.provision_username:
        parser.error('--lockdown-default-user requires --provision-username/--provision-password')
    if (args.admin_username and not args.admin_password) or (args.admin_password and not args.admin_username):
        parser.error('--admin-username and --admin-password must be provided together')
    
    loader = FalkorDBCSVLoader(
        host=args.host,
        port=args.port,
        graph_name=args.graph_name,
        csv_dir=args.csv_dir,
        username=args.username,
        password=args.password,
        merge_mode=args.merge_mode,
        multi_graph_mode=args.multi_graph,
        debug=args.debug,
    )

    try:
        # Load everything (indexes, constraints, and data)
        loader.load_all_csvs(args.batch_size)

        # Optional: provision ACL user after successful load
        if args.provision_username:
            loader.provision_acl_user(
                provision_username=args.provision_username,
                provision_password=args.provision_password,
                commands_mode=args.provision_commands,
                key_pattern=args.provision_key_pattern,
                admin_username=args.admin_username,
                admin_password=args.admin_password,
                lockdown_default_user=args.lockdown_default_user,
            )

        if args.stats:
            loader.get_graph_stats()
            loader.verify_node_attributes("Person", 3)

    except KeyboardInterrupt:
        print("\n❌ Loading interrupted by user")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)
    finally:
        loader.close()


if __name__ == "__main__":
    main()
