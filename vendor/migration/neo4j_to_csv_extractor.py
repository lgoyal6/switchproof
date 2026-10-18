#!/usr/bin/env python3
"""
Neo4j to CSV Extractor
Extract nodes and edges from Neo4j database to CSV files for FalkorDB import
Does not require APOC library - uses pure Cypher queries
"""

import csv
import json
from typing import Dict, List, Any, Set, Optional
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError, ServiceUnavailable
import argparse
import os
import sys
from collections import defaultdict


def escape_cypher_ident(name: str) -> str:
    """Escape a Neo4j/FalkorDB identifier for safe use in Cypher."""
    safe = str(name).replace('`', '``')
    return f'`{safe}`'


def safe_filename_component(name: str, max_len: int = 120) -> str:
    """Make a filesystem-safe filename component while remaining unique."""
    import hashlib
    import re
    original = str(name)
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', original).strip('._')
    if not safe:
        safe = 'item'
    if safe != original or len(safe) > max_len:
        digest = hashlib.sha1(original.encode('utf-8')).hexdigest()[:10]
        safe = f'{safe[:max_len]}_{digest}'
    return safe


def labels_to_file_key(labels: List[str]) -> str:
    """Stable file key for a multi-label set."""
    normalized = [str(label) for label in labels if label]
    if not normalized:
        return 'Unknown'
    return '__'.join(sorted(normalized))



class Neo4jToCSVExtractor:
    def __init__(self, uri: str, username: str, password: str, database: str = "neo4j", config_file: Optional[str] = None, 
                 tenant_mode: Optional[str] = None, tenant_filter_key: Optional[str] = None):
        """Initialize connection to Neo4j database
        
        Args:
            uri: Neo4j connection URI
            username: Neo4j username
            password: Neo4j password
            database: Neo4j database name
            config_file: Optional migration configuration file
            tenant_mode: Optional multi-tenant segregation mode ('label' or 'property')
            tenant_filter_key: The label or property name to filter tenants by
        """
        print(f"Connecting to Neo4j at {uri} with username '{username}'...")
        
        # Load configuration
        self.config = self._load_config(config_file)
        
        # Multi-tenant configuration
        self.tenant_mode = tenant_mode
        self.tenant_filter_key = tenant_filter_key
        self.tenants = []
        self.node_property_types = defaultdict(dict)
        self.relationship_property_types = defaultdict(dict)
        self.vector_node_properties = defaultdict(set)
        self.vector_relationship_properties = defaultdict(set)
        # elementId(n) -> export id used in CSV/FalkorDB (from node id property or fallback)
        self._export_id_by_element_id: Dict[str, Any] = {}
        self.node_id_property = str(self.config.get("node_id_property") or "id")
        
        try:
            self.driver = GraphDatabase.driver(uri, auth=(username, password))
            self.database = database
            self.output_dir = "csv_output"
            
            # Test the connection
            self._test_connection()
            
            # Ensure output directory exists
            os.makedirs(self.output_dir, exist_ok=True)
            
            # Discover tenants if multi-tenant mode is enabled
            if self.tenant_mode:
                self._discover_tenants()
            
            self._collect_vector_index_hints()
            self._collect_vector_value_hints()
            
            print("✅ Successfully connected to Neo4j!")
            
        except AuthError as e:
            print(f"❌ Authentication failed: {e}")
            print("\n🔧 Troubleshooting tips:")
            print("1. Check your username and password")
            print("2. Make sure Neo4j is running")
            print("3. Try connecting with Neo4j Browser first")
            print("4. Check if you need to change the initial password")
            print("\n💡 Common solutions:")
            print("   - Default password might be 'neo4j' (change required on first login)")
            print("   - Try connecting to Neo4j Browser at http://localhost:7474")
            print("   - Reset password if needed: neo4j-admin set-initial-password <newpassword>")
            sys.exit(1)
        except ServiceUnavailable as e:
            print(f"❌ Cannot connect to Neo4j service: {e}")
            print("\n🔧 Troubleshooting tips:")
            print("1. Make sure Neo4j is running")
            print("2. Check the URI (default: bolt://localhost:7687)")
            print("3. Verify firewall settings")
            print("4. Check Neo4j logs for errors")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            sys.exit(1)
    
    def _load_config(self, config_file: Optional[str]) -> Dict[str, Any]:
        """Load migration configuration from JSON file"""
        default_config = {
            "label_mappings": {},
            "property_mappings": {},
            "relationship_mappings": {},
            "relationship_property_mappings": {},
            "excluded_labels": [],
            "excluded_relationships": [],
            "excluded_properties": {},
            "data_transformations": {},
            # Node join key used in CSV id column + FalkorDB indexes/edge matching.
            # Prefer this node property; if missing, fall back to elementId(n) (never id(n)).
            "node_id_property": "id",
        }
        
        if not config_file:
            config_file = "migrate_config.json"
        
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    config = json.load(f)
                    print(f"✅ Loaded configuration from {config_file}")
                    # Merge with defaults
                    for key in default_config:
                        if key not in config:
                            config[key] = default_config[key]
                    return config
            except Exception as e:
                print(f"⚠️ Error loading config file {config_file}: {e}")
                print("Using default configuration")
        else:
            print(f"📄 Config file {config_file} not found, using default configuration")
        
        return default_config
    
    def _test_connection(self):
        """Test the database connection"""
        try:
            with self.driver.session(database=self.database) as session:
                # Simple query to test connection
                result = session.run("RETURN 1 as test")
                result.single()
        except Exception as e:
            print(f"❌ Connection test failed: {e}")
            raise
    
    def _discover_tenants(self):
        """Discover all tenants in the database based on tenant mode"""
        if not self.tenant_mode or not self.tenant_filter_key:
            return
        
        print(f"🔍 Discovering tenants using {self.tenant_mode} mode with key '{self.tenant_filter_key}'...")
        
        with self.driver.session(database=self.database) as session:
            if self.tenant_mode == 'label':
                # Filter nodes that have the specified label
                query = f"""
                MATCH (n:{self.tenant_filter_key})
                RETURN DISTINCT '{self.tenant_filter_key}' as tenant
                """
                result = session.run(query)
                self.tenants = [record['tenant'] for record in result]
            
            elif self.tenant_mode == 'property':
                # Get distinct values of the specified property across all nodes
                query = f"""
                MATCH (n)
                WHERE n.{self.tenant_filter_key} IS NOT NULL
                RETURN DISTINCT n.{self.tenant_filter_key} as tenant
                ORDER BY tenant
                """
                result = session.run(query)
                self.tenants = [str(record['tenant']) for record in result]
        
        if self.tenants:
            print(f"  Found {len(self.tenants)} tenant(s): {self.tenants}")
            # Create subdirectories for each tenant
            for tenant in self.tenants:
                tenant_dir = self._get_tenant_dir(tenant)
                os.makedirs(tenant_dir, exist_ok=True)
        else:
            print(f"  ⚠️  No tenants found using {self.tenant_mode} mode with key '{self.tenant_filter_key}'")
    
    def _get_tenant_dir(self, tenant: str) -> str:
        """Get the output directory for a specific tenant"""
        # Sanitize tenant name for use in directory name
        safe_tenant = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in tenant)
        return os.path.join(self.output_dir, f"tenant_{safe_tenant}")
    
    def _get_tenant_filter_clause(self, tenant: Optional[str] = None) -> str:
        """Generate a WHERE clause for filtering by tenant"""
        if not self.tenant_mode or not self.tenant_filter_key or tenant is None:
            return ""
        
        if self.tenant_mode == 'label':
            # Tenant filtering by label is handled in MATCH clause
            return ""
        elif self.tenant_mode == 'property':
            # Escape single quotes in tenant value
            safe_tenant = tenant.replace("'", "\\'").replace("\\", "\\\\")
            return f"WHERE n.{self.tenant_filter_key} = '{safe_tenant}'"
        
        return ""
    
    def close(self):
        """Close the database connection"""
        if self.driver:
            self.driver.close()

    @staticmethod
    def _infer_property_type(value: Any) -> Optional[str]:
        """Infer the canonical property type from a Python value."""
        if value is None:
            return None
        if isinstance(value, bool):
            return 'boolean'
        if isinstance(value, int):
            return 'integer'
        if isinstance(value, float):
            return 'float'
        return 'string'

    @staticmethod
    def _merge_property_types(existing: Optional[str], candidate: Optional[str]) -> Optional[str]:
        """Merge observed types for a property into a single safe type."""
        if not existing:
            return candidate
        if not candidate or existing == candidate:
            return existing
        if {existing, candidate} == {'integer', 'float'}:
            return 'float'
        return 'string'

    def _record_property_type(self, property_type_map: Dict[str, str], property_name: str, value: Any):
        """Record inferred property type for schema-driven loading."""
        inferred_type = self._infer_property_type(value)
        merged_type = self._merge_property_types(property_type_map.get(property_name), inferred_type)
        if merged_type:
            property_type_map[property_name] = merged_type

    def _record_explicit_property_type(self, property_type_map: Dict[str, str], property_name: str, property_type: str):
        """Record a fixed property type that should override inferred type (e.g. vector hints)."""
        if not property_name or not property_type:
            return
        property_type_map[property_name] = property_type

    @staticmethod
    def _is_numeric_scalar(value: Any) -> bool:
        """Check whether a value is a numeric scalar (bool excluded)."""
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def _is_numeric_vector_candidate(self, value: Any) -> bool:
        """Heuristic: numeric arrays with at least 128 elements are vector candidates."""
        if not isinstance(value, list):
            return False
        if len(value) < 128:
            return False
        return all(self._is_numeric_scalar(item) for item in value)

    def _collect_vector_index_hints(self):
        """Infer vector-typed properties from Neo4j index metadata."""
        try:
            indexes = self.get_indexes()
        except Exception as e:
            print(f"⚠️ Could not inspect indexes for vector hints: {e}")
            return

        for index in indexes:
            index_type = str(index.get('type', '')).upper()
            provider = index.get('provider', {})
            provider_hint = ""
            if isinstance(provider, dict):
                provider_hint = " ".join(
                    str(provider.get(key, "")) for key in ("key", "name", "type", "provider")
                ).upper()
            elif provider:
                provider_hint = str(provider).upper()

            if "VECTOR" not in index_type and "VECTOR" not in provider_hint:
                continue

            entity_type = str(index.get('entity_type', 'NODE')).upper()
            labels_or_types = index.get('labels', []) or []
            properties = index.get('properties', []) or []

            if not isinstance(labels_or_types, list):
                labels_or_types = [labels_or_types]
            if not isinstance(properties, list):
                properties = [properties]

            for label_or_type in labels_or_types:
                for prop in properties:
                    if not label_or_type or not prop:
                        continue
                    label_or_type = str(label_or_type)
                    prop = str(prop)
                    if entity_type == 'RELATIONSHIP':
                        self.vector_relationship_properties[label_or_type].add(prop)
                    else:
                        self.vector_node_properties[label_or_type].add(prop)

        node_hints = sum(len(props) for props in self.vector_node_properties.values())
        rel_hints = sum(len(props) for props in self.vector_relationship_properties.values())
        total_hints = node_hints + rel_hints
        if total_hints > 0:
            print(
                f"🔎 Inferred {total_hints} vector property hint(s) from Neo4j indexes "
                f"(nodes={node_hints}, relationships={rel_hints})"
            )

    def _collect_vector_value_hints(self, sample_size: int = 1000):
        """Fallback: infer vector properties from sampled numeric-list values."""
        inferred_nodes = 0
        inferred_relationships = 0

        try:
            node_labels = self.get_node_labels()
        except Exception as e:
            print(f"⚠️ Could not inspect node labels for vector value hints: {e}")
            node_labels = []

        if node_labels:
            with self.driver.session(database=self.database) as session:
                for label in node_labels:
                    try:
                        query = f"""
                        MATCH (n:{escape_cypher_ident(label)})
                        RETURN n
                        LIMIT {sample_size}
                        """
                        for record in session.run(query):
                            node_data = dict(record['n'])
                            for prop, value in node_data.items():
                                if not self._is_numeric_vector_candidate(value):
                                    continue
                                if prop in self.vector_node_properties[label]:
                                    continue
                                self.vector_node_properties[label].add(prop)
                                inferred_nodes += 1
                    except Exception as e:
                        print(f"⚠️ Failed vector value hint scan for label '{label}': {e}")

        try:
            rel_types = self.get_relationship_types()
        except Exception as e:
            print(f"⚠️ Could not inspect relationship types for vector value hints: {e}")
            rel_types = []

        if rel_types:
            with self.driver.session(database=self.database) as session:
                for rel_type in rel_types:
                    try:
                        query = f"""
                        MATCH ()-[r:{escape_cypher_ident(rel_type)}]->()
                        RETURN r
                        LIMIT {sample_size}
                        """
                        for record in session.run(query):
                            rel_data = dict(record['r'])
                            for prop, value in rel_data.items():
                                if not self._is_numeric_vector_candidate(value):
                                    continue
                                if prop in self.vector_relationship_properties[rel_type]:
                                    continue
                                self.vector_relationship_properties[rel_type].add(prop)
                                inferred_relationships += 1
                    except Exception as e:
                        print(f"⚠️ Failed vector value hint scan for relationship '{rel_type}': {e}")

        total_hints = inferred_nodes + inferred_relationships
        if total_hints > 0:
            print(
                f"🔎 Inferred {total_hints} additional vector property hint(s) from sampled values "
                f"(nodes={inferred_nodes}, relationships={inferred_relationships})"
            )

    def _is_vector_node_property(self, label: str, property_name: str) -> bool:
        return property_name in self.vector_node_properties.get(label, set())

    def _is_vector_relationship_property(self, rel_type: str, property_name: str) -> bool:
        return property_name in self.vector_relationship_properties.get(rel_type, set())

    def write_type_schema(self) -> str:
        """Write inferred property types to JSON sidecar file for the loader."""
        schema_file = os.path.join(self.output_dir, "property_types.json")
        schema = {
            "format_version": 1,
            "nodes": {
                label: dict(sorted(props.items()))
                for label, props in sorted(self.node_property_types.items())
            },
            "relationships": {
                rel_type: dict(sorted(props.items()))
                for rel_type, props in sorted(self.relationship_property_types.items())
            }
        }

        with open(schema_file, 'w', encoding='utf-8') as f:
            json.dump(schema, f, indent=2, ensure_ascii=False)

        print(f"Generated property type schema: {schema_file}")
        return schema_file
    
    def get_node_labels(self) -> List[str]:
        """Get all node labels in the database"""
        with self.driver.session(database=self.database) as session:
            result = session.run("CALL db.labels()")
            labels = [record["label"] for record in result]
            # Filter out excluded labels
            excluded = self.config.get("excluded_labels", [])
            return [label for label in labels if label not in excluded]
    
    def get_relationship_types(self) -> List[str]:
        """Get all relationship types in the database"""
        with self.driver.session(database=self.database) as session:
            result = session.run("CALL db.relationshipTypes()")
            rel_types = [record["relationshipType"] for record in result]
            # Filter out excluded relationships
            excluded = self.config.get("excluded_relationships", [])
            return [rel_type for rel_type in rel_types if rel_type not in excluded]
    
    def get_node_properties(self, label: str) -> Set[str]:
        """Get all properties for nodes with a specific label"""
        with self.driver.session(database=self.database) as session:
            # Get a sample of nodes to determine properties
            query = f"""
            MATCH (n:{escape_cypher_ident(label)})
            WITH n LIMIT 1000
            UNWIND keys(n) AS key
            RETURN DISTINCT key
            """
            result = session.run(query)
            properties = {record["key"] for record in result}
            # Filter out excluded properties for this label
            excluded_props = self.config.get("excluded_properties", {}).get(label, [])
            return {prop for prop in properties if prop not in excluded_props}
    
    def get_relationship_properties(self, rel_type: str) -> Set[str]:
        """Get all properties for relationships of a specific type"""
        with self.driver.session(database=self.database) as session:
            query = f"""
            MATCH ()-[r:{escape_cypher_ident(rel_type)}]->()
            WITH r LIMIT 1000
            UNWIND keys(r) AS key
            RETURN DISTINCT key
            """
            result = session.run(query)
            return {record["key"] for record in result}
    
    def get_indexes(self) -> List[Dict[str, Any]]:
        """Get all indexes in the database"""
        indexes = []
        with self.driver.session(database=self.database) as session:
            try:
                # Try the modern SHOW INDEXES command (Neo4j 4.0+)
                result = session.run("SHOW INDEXES")
                for record in result:
                    index_info = {
                        'name': record.get('name', 'unknown'),
                        'type': record.get('type', 'unknown'),
                        'state': record.get('state', 'unknown'),
                        'population_percent': record.get('populationPercent', 0),
                        'uniqueness': record.get('uniqueness', 'NON_UNIQUE'),
                        'entity_type': record.get('entityType', 'NODE'),
                        'labels': record.get('labelsOrTypes', []),
                        'properties': record.get('properties', []),
                        'provider': record.get('provider', {}),
                        'options': record.get('options', {})
                    }
                    indexes.append(index_info)
            except Exception as e:
                print(f"SHOW INDEXES failed, trying legacy command: {e}")
                try:
                    # Fallback to legacy db.indexes() for older Neo4j versions
                    result = session.run("CALL db.indexes()")
                    for record in result:
                        index_info = {
                            'name': record.get('indexName', record.get('name', 'unknown')),
                            'type': record.get('type', 'unknown'),
                            'state': record.get('state', 'unknown'),
                            'population_percent': record.get('populationPercent', 0),
                            'uniqueness': 'UNIQUE' if record.get('uniqueness', False) else 'NON_UNIQUE',
                            'entity_type': 'NODE',  # Legacy indexes are typically node indexes
                            'labels': record.get('tokenNames', []),
                            'properties': record.get('properties', []),
                            'provider': record.get('provider', {}),
                            'options': record.get('options', record.get('indexConfig', {}))
                        }
                        indexes.append(index_info)
                except Exception as e2:
                    print(f"Legacy db.indexes() also failed: {e2}")
                    # Try even older approach
                    try:
                        result = session.run("CALL db.schema.nodeTypeProperties()")
                        schema_info = list(result)
                        if schema_info:
                            print("Found schema information, but no direct index access available")
                    except Exception as e3:
                        print(f"No index information could be retrieved: {e3}")
        
        return indexes
    
    def get_constraints(self) -> List[Dict[str, Any]]:
        """Get all constraints in the database"""
        constraints = []
        with self.driver.session(database=self.database) as session:
            try:
                # Try the modern SHOW CONSTRAINTS command (Neo4j 4.0+)
                result = session.run("SHOW CONSTRAINTS")
                for record in result:
                    constraint_info = {
                        'name': record.get('name', 'unknown'),
                        'type': record.get('type', 'unknown'),
                        'entity_type': record.get('entityType', 'NODE'),
                        'labels': record.get('labelsOrTypes', []),
                        'properties': record.get('properties', []),
                        'options': record.get('options', {})
                    }
                    constraints.append(constraint_info)
            except Exception as e:
                print(f"SHOW CONSTRAINTS failed, trying legacy command: {e}")
                try:
                    # Fallback to legacy db.constraints() for older Neo4j versions
                    result = session.run("CALL db.constraints()")
                    for record in result:
                        constraint_info = {
                            'name': record.get('name', 'unknown'),
                            'type': record.get('type', 'unknown'),
                            'entity_type': 'NODE',  # Legacy constraints are typically node constraints
                            'labels': record.get('label', '').split(':') if record.get('label') else [],
                            'properties': record.get('properties', []),
                            'options': {}
                        }
                        constraints.append(constraint_info)
                except Exception as e2:
                    print(f"Legacy db.constraints() also failed: {e2}")
        
        return constraints
    
    def extract_indexes_to_csv(self) -> str:
        """Extract all indexes to CSV file"""
        print("Extracting database indexes...")
        
        indexes = self.get_indexes()
        # Apply property mappings to indexes
        indexes = self._translate_index_properties(indexes)
        csv_filename = os.path.join(self.output_dir, "indexes.csv")
        
        with open(csv_filename, 'w', newline='', encoding='utf-8') as csvfile:
            headers = ['name', 'type', 'state', 'population_percent', 'uniqueness', 
                      'entity_type', 'labels', 'properties', 'provider', 'options']
            writer = csv.DictWriter(csvfile, fieldnames=headers)
            writer.writeheader()
            
            for index in indexes:
                # Safely handle labels field
                labels = index.get('labels', [])
                if not isinstance(labels, list):
                    labels = [str(labels)] if labels else []
                
                # Safely handle properties field
                properties = index.get('properties', [])
                if not isinstance(properties, list):
                    properties = [str(properties)] if properties else []
                
                row = {
                    'name': index.get('name', ''),
                    'type': index.get('type', ''),
                    'state': index.get('state', ''),
                    'population_percent': index.get('population_percent', 0),
                    'uniqueness': index.get('uniqueness', ''),
                    'entity_type': index.get('entity_type', ''),
                    'labels': ';'.join(labels),
                    'properties': ';'.join(properties),
                    'provider': json.dumps(index.get('provider', {})),
                    'options': json.dumps(index.get('options', {}))
                }
                writer.writerow(row)
        
        print(f"  Exported {len(indexes)} indexes to {csv_filename}")
        return csv_filename
    
    def extract_constraints_to_csv(self) -> str:
        """Extract all constraints to CSV file"""
        print("Extracting database constraints...")
        
        constraints = self.get_constraints()
        # Apply property mappings to constraints
        constraints = self._translate_constraint_properties(constraints)
        csv_filename = os.path.join(self.output_dir, "constraints.csv")
        
        with open(csv_filename, 'w', newline='', encoding='utf-8') as csvfile:
            headers = ['name', 'type', 'entity_type', 'labels', 'properties', 'options']
            writer = csv.DictWriter(csvfile, fieldnames=headers)
            writer.writeheader()
            
            for constraint in constraints:
                # Safely handle labels field
                labels = constraint.get('labels', [])
                if not isinstance(labels, list):
                    labels = [str(labels)] if labels else []
                
                # Safely handle properties field
                properties = constraint.get('properties', [])
                if not isinstance(properties, list):
                    properties = [str(properties)] if properties else []
                
                row = {
                    'name': constraint.get('name', ''),
                    'type': constraint.get('type', ''),
                    'entity_type': constraint.get('entity_type', ''),
                    'labels': ';'.join(labels),
                    'properties': ';'.join(properties),
                    'options': json.dumps(constraint.get('options', {}))
                }
                writer.writerow(row)
        
        print(f"  Exported {len(constraints)} constraints to {csv_filename}")
        return csv_filename

    def _translate_constraint_properties(self, constraints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Translate constraint properties based on the configuration."""
        
        translated_constraints = []
        
        for constraint in constraints:
            translated_constraint = constraint.copy()
            labels = translated_constraint.get('labels', [])
            properties = translated_constraint.get('properties', [])
            
            if not labels or not properties:
                translated_constraints.append(translated_constraint)
                continue

            # Assuming single label for simplicity; adjust if constraints can have multiple labels
            label = labels[0]
            
            # Get property mappings for this label
            property_mappings = self.config.get('property_mappings', {}).get(label, {})
            
            if not property_mappings:
                translated_constraints.append(translated_constraint)
                continue

            # Translate properties
            translated_properties = [property_mappings.get(prop, prop) for prop in properties]
            translated_constraint['properties'] = translated_properties
            translated_constraints.append(translated_constraint)
            
        return translated_constraints
    

    def _translate_index_properties(self, indexes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Translate index properties based on the configuration."""
        
        translated_indexes = []
        
        for index in indexes:
            translated_index = index.copy()
            labels = translated_index.get('labels', [])
            properties = translated_index.get('properties', [])
            
            if not labels or not properties:
                translated_indexes.append(translated_index)
                continue

            # Assuming single label for simplicity; adjust if indexes can have multiple labels
            label = labels[0]
            
            # Get property mappings for this label
            property_mappings = self.config.get('property_mappings', {}).get(label, {})
            
            if not property_mappings:
                translated_indexes.append(translated_index)
                continue

            # Translate properties
            translated_properties = [property_mappings.get(prop, prop) for prop in properties]
            translated_index['properties'] = translated_properties
            translated_indexes.append(translated_index)
            
        return translated_indexes
        
    def generate_falkordb_index_script(self, index_file: str, constraint_file: str) -> str:
        """Generate FalkorDB index creation script"""
        script_filename = os.path.join(self.output_dir, "create_indexes_falkordb.cypher")
        
        with open(script_filename, 'w') as f:
            f.write("// FalkorDB Index Creation Script - Generated from Neo4j Export\n")
            f.write("// Run this script in FalkorDB to recreate indexes and constraints\n\n")
            
            # Read and process indexes
            f.write("// Create Indexes\n")
            try:
                with open(index_file, 'r', encoding='utf-8') as csvfile:
                    reader = csv.DictReader(csvfile)
                    for row in reader:
                        if row['labels'] and row['properties']:
                            labels = row['labels'].split(';')
                            properties = row['properties'].split(';')
                            
                            for label in labels:
                                for prop in properties:
                                    # Convert Neo4j index to FalkorDB syntax
                                    if row['uniqueness'] == 'UNIQUE':
                                        f.write(f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;\n")
                                    else:
                                        f.write(f"CREATE INDEX ON :{label}({prop});\n")
                f.write("\n")
            except Exception as e:
                f.write(f"// Error reading index file: {e}\n\n")
            
            # Read and process constraints
            f.write("// Create Constraints\n")
            try:
                with open(constraint_file, 'r', encoding='utf-8') as csvfile:
                    reader = csv.DictReader(csvfile)
                    for row in reader:
                        if row['labels'] and row['properties']:
                            labels = row['labels'].split(';')
                            properties = row['properties'].split(';')
                            
                            for label in labels:
                                for prop in properties:
                                    constraint_type = row['type'].upper()
                                    
                                    if 'UNIQUE' in constraint_type:
                                        f.write(f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;\n")
                                    elif 'EXIST' in constraint_type or 'NOT_NULL' in constraint_type:
                                        f.write(f"// Note: FalkorDB may not support existence constraints - verify manually\n")
                                        f.write(f"// CREATE CONSTRAINT ON (n:{label}) ASSERT exists(n.{prop});\n")
                f.write("\n")
            except Exception as e:
                f.write(f"// Error reading constraint file: {e}\n\n")
        
        print(f"Generated FalkorDB index creation script: {script_filename}")
        return script_filename
    

    def _map_node_property_name(self, labels: List[str], prop: str) -> str:
        """Map a property name using the first matching label-specific mapping."""
        for label in labels:
            mapped = self.config.get('property_mappings', {}).get(label, {}).get(prop)
            if mapped:
                return mapped
        return prop

    def _node_transformation(self, labels: List[str], prop: str) -> Optional[Dict[str, Any]]:
        for label in labels:
            transformation = self.config.get('data_transformations', {}).get(label, {}).get(prop)
            if transformation:
                return transformation
        return None

    def _is_vector_node_property_any(self, labels: List[str], property_name: str) -> bool:
        return any(self._is_vector_node_property(label, property_name) for label in labels)

    def _properties_for_label_set(self, labels: List[str]) -> List[str]:
        """Union of properties across all labels on a multi-label node."""
        props: Set[str] = set()
        for label in labels:
            props |= set(self.get_node_properties(label))
            props |= set(self.vector_node_properties.get(label, set()))
        # Apply exclusions from every label
        excluded = set()
        for label in labels:
            excluded |= set(self.config.get('excluded_properties', {}).get(label, []))
        if self.tenant_mode == 'property' and self.tenant_filter_key:
            excluded.add(self.tenant_filter_key)
        # Join key is emitted as the dedicated CSV "id" column (and indexed in FalkorDB).
        excluded.add(self.node_id_property)
        return sorted(prop for prop in props if prop not in excluded)


    def _cypher_node_id_expr(self, node_alias: str = "n") -> str:
        """Cypher expression for stable export id without deprecated id()."""
        prop = self.node_id_property
        prop_lit = str(prop).replace("\\", "\\\\").replace("'", "\\'")
        # Prefer explicit property when present on the node; otherwise elementId.
        # Use keys() membership to avoid Neo4j UnknownPropertyKeyWarning when the
        # configured id property has never been stored in the database.
        return (
            f"CASE WHEN '{prop_lit}' IN keys({node_alias}) "
            f"THEN {node_alias}.{escape_cypher_ident(prop)} "
            f"ELSE elementId({node_alias}) END"
        )

    def _normalize_export_id(self, value: Any) -> Any:
        """Normalize export ids for CSV + loader consistency."""
        if value is None:
            return None
        # neo4j temporal/spatial unlikely for ids; keep primitives
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            # Keep ints clean (avoid 1.0)
            if isinstance(value, float) and value.is_integer():
                return int(value)
            return value
        return str(value)

    def _resolve_export_id_from_node_data(self, node_data: Dict[str, Any], element_id: str) -> Any:
        """Resolve CSV/FalkorDB id from node property, else elementId fallback."""
        prop = self.node_id_property
        if prop in node_data and node_data[prop] is not None and node_data[prop] != "":
            export_id = self._normalize_export_id(node_data[prop])
        else:
            export_id = self._normalize_export_id(element_id)
        self._export_id_by_element_id[str(element_id)] = export_id
        return export_id

    def _lookup_export_id(self, element_id: str, node_data: Optional[Dict[str, Any]] = None) -> Any:
        key = str(element_id)
        if key in self._export_id_by_element_id:
            return self._export_id_by_element_id[key]
        if node_data is not None:
            return self._resolve_export_id_from_node_data(node_data, element_id)
        # Last resort: use element id string
        export_id = self._normalize_export_id(element_id)
        self._export_id_by_element_id[key] = export_id
        return export_id

    def extract_unique_nodes(self, batch_size: int = 1000, tenant: Optional[str] = None) -> List[str]:
        """Extract each Neo4j node exactly once, preserving the full multi-label set.

        Multi-label nodes previously were emitted once per label (nodes_A.csv and
        nodes_B.csv), which duplicated nodes in FalkorDB. This path writes one row
        per node id, with labels stored as a colon-separated list.
        """
        tenant_info = f" (tenant: {tenant})" if tenant else ""
        print(f"Extracting unique nodes with full label sets{tenant_info}")

        excluded_labels = set(self.config.get('excluded_labels', []))
        output_dir = self._get_tenant_dir(tenant) if tenant else self.output_dir
        os.makedirs(output_dir, exist_ok=True)

        # label_key -> {path, handle, writer, properties, count}
        writers: Dict[str, Dict[str, Any]] = {}
        seen_ids: Set[Any] = set()
        csv_files: List[str] = []
        total_count = 0
        skipped_duplicate = 0
        skipped_excluded = 0

        def get_writer(label_list: List[str]):
            key = labels_to_file_key(label_list)
            if key in writers:
                return writers[key]
            properties = self._properties_for_label_set(label_list)
            mapped_properties = []
            for prop in properties:
                mapped_prop = self._map_node_property_name(label_list, prop)
                mapped_properties.append(mapped_prop)
                # Record vector/type metadata under each concrete label for schema consumers
                for label in label_list:
                    if self._is_vector_node_property(label, prop):
                        self._record_explicit_property_type(
                            self.node_property_types[label], mapped_prop, 'vector'
                        )
            file_key = safe_filename_component(key)
            csv_filename = os.path.join(output_dir, f"nodes_{file_key}.csv")
            handle = open(csv_filename, 'w', newline='', encoding='utf-8')
            headers = ['id', 'labels'] + mapped_properties
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction='ignore')
            writer.writeheader()
            entry = {
                'path': csv_filename,
                'handle': handle,
                'writer': writer,
                'properties': properties,
                'mapped_properties': mapped_properties,
                'labels': label_list,
                'count': 0,
            }
            writers[key] = entry
            csv_files.append(csv_filename)
            return entry

        try:
            skip = 0
            with self.driver.session(database=self.database) as session:
                while True:
                    tenant_filter = self._get_tenant_filter_clause(tenant)
                    # For label-based tenancy, restrict to nodes carrying the tenant label.
                    if tenant and self.tenant_mode == 'label' and self.tenant_filter_key:
                        match_clause = f"MATCH (n:{escape_cypher_ident(self.tenant_filter_key)})"
                        where_clause = tenant_filter
                    else:
                        match_clause = "MATCH (n)"
                        where_clause = tenant_filter

                    id_expr = self._cypher_node_id_expr('n')
                    query = f"""
                    {match_clause}
                    {where_clause}
                    RETURN elementId(n) as element_id, {id_expr} as node_id, labels(n) as labels, n
                    ORDER BY elementId(n)
                    SKIP {skip} LIMIT {batch_size}
                    """
                    result = session.run(query)
                    records = list(result)
                    if not records:
                        break

                    for record in records:
                        element_id = record['element_id']
                        node_data_preview = dict(record['n'])
                        node_id = self._resolve_export_id_from_node_data(node_data_preview, element_id)
                        # Prefer coalesced value from query when present
                        if record.get('node_id') is not None:
                            node_id = self._normalize_export_id(record['node_id'])
                            self._export_id_by_element_id[str(element_id)] = node_id
                        if node_id in seen_ids:
                            skipped_duplicate += 1
                            continue
                        seen_ids.add(node_id)

                        raw_labels = [str(lbl) for lbl in (record['labels'] or [])]
                        label_list = [lbl for lbl in raw_labels if lbl not in excluded_labels]
                        # Apply label mappings while preserving multi-label cardinality.
                        mapped_labels = []
                        for lbl in label_list:
                            mapped_labels.append(self.config.get('label_mappings', {}).get(lbl, lbl))
                        # De-dupe while preserving order
                        deduped = []
                        for lbl in mapped_labels:
                            if lbl not in deduped:
                                deduped.append(lbl)
                        label_list = deduped
                        if not label_list:
                            skipped_excluded += 1
                            continue

                        entry = get_writer(label_list)
                        node_data = dict(record['n'])
                        row = {
                            'id': node_id,
                            'labels': ':'.join(label_list),
                        }
                        for prop in entry['properties']:
                            value = node_data.get(prop, '')
                            if isinstance(value, list):
                                value = ';'.join(str(v) for v in value)
                            mapped_prop = self._map_node_property_name(label_list, prop)
                            transformation = self._node_transformation(label_list, prop)
                            if transformation:
                                pre = value
                                value = self._apply_transformation(value, transformation)
                                if pre != value:
                                    print(f"  🔄 Transformed {label_list}.{prop}: '{pre}' -> '{value}'")
                            if self._is_vector_node_property_any(label_list, prop):
                                for label in label_list:
                                    self._record_explicit_property_type(
                                        self.node_property_types[label], mapped_prop, 'vector'
                                    )
                            else:
                                for label in label_list:
                                    self._record_property_type(
                                        self.node_property_types[label], mapped_prop, value
                                    )
                            row[mapped_prop] = str(value) if value is not None else ''

                        entry['writer'].writerow(row)
                        entry['count'] += 1
                        total_count += 1

                    skip += batch_size
                    print(f"  Processed {total_count} unique nodes...")
        finally:
            for entry in writers.values():
                entry['handle'].close()
                print(f"  Exported {entry['count']} nodes to {entry['path']}")

        print(
            f"  Unique node export complete: {total_count} nodes, "
            f"{len(csv_files)} file(s), skipped_duplicates={skipped_duplicate}, "
            f"skipped_excluded={skipped_excluded}"
        )
        return csv_files

    def extract_nodes_by_label(self, label: str, batch_size: int = 1000, tenant: Optional[str] = None) -> str:
        """Deprecated single-label export path.

        Kept for compatibility, but multi-label safe extraction should use
        extract_unique_nodes/extract_all_nodes which emit each node once.
        """
        print(
            f"⚠️ extract_nodes_by_label({label}) is legacy and can duplicate multi-label nodes. "
            "Prefer extract_all_nodes()/extract_unique_nodes()."
        )
        # Fall back to unique extraction filtered by label presence in post is complex;
        # keep previous behavior only when explicitly requested.
        tenant_info = f" (tenant: {tenant})" if tenant else ""
        print(f"Extracting nodes with label: {label}{tenant_info}")
        properties = sorted(list(self.get_node_properties(label)))
        hinted_vector_props = sorted(self.vector_node_properties.get(label, set()))
        for vector_prop in hinted_vector_props:
            if vector_prop not in properties:
                properties.append(vector_prop)
        properties = sorted(properties)
        if tenant and self.tenant_mode == 'property' and self.tenant_filter_key:
            properties = [p for p in properties if p != self.tenant_filter_key]
        properties = [p for p in properties if p != self.node_id_property]
        output_dir = self._get_tenant_dir(tenant) if tenant else self.output_dir
        file_key = safe_filename_component(label)
        csv_filename = os.path.join(output_dir, f"nodes_{file_key}.csv")
        with open(csv_filename, 'w', newline='', encoding='utf-8') as csvfile:
            mapped_properties = []
            for prop in properties:
                mapped_prop = self.config['property_mappings'].get(label, {}).get(prop, prop)
                mapped_properties.append(mapped_prop)
                if self._is_vector_node_property(label, prop):
                    self._record_explicit_property_type(
                        self.node_property_types[label], mapped_prop, 'vector'
                    )
            headers = ['id', 'labels'] + mapped_properties
            writer = csv.DictWriter(csvfile, fieldnames=headers)
            writer.writeheader()
            skip = 0
            total_count = 0
            with self.driver.session(database=self.database) as session:
                while True:
                    tenant_filter = self._get_tenant_filter_clause(tenant)
                    label_ident = escape_cypher_ident(label)
                    id_expr = self._cypher_node_id_expr('n')
                    query = f"""
                    MATCH (n:{label_ident})
                    {tenant_filter}
                    RETURN elementId(n) as element_id, {id_expr} as node_id, labels(n) as labels, n
                    ORDER BY elementId(n)
                    SKIP {skip} LIMIT {batch_size}
                    """
                    result = session.run(query)
                    records = list(result)
                    if not records:
                        break
                    for record in records:
                        node_data = dict(record['n'])
                        export_id = self._normalize_export_id(record['node_id'])
                        self._export_id_by_element_id[str(record['element_id'])] = export_id
                        raw_labels = [str(lbl) for lbl in (record['labels'] or [])]
                        mapped_labels = [
                            self.config.get('label_mappings', {}).get(lbl, lbl) for lbl in raw_labels
                        ]
                        row = {
                            'id': export_id,
                            'labels': ':'.join(mapped_labels) if mapped_labels else label,
                        }
                        for prop in properties:
                            value = node_data.get(prop, '')
                            if isinstance(value, list):
                                value = ';'.join(str(v) for v in value)
                            mapped_prop = self.config['property_mappings'].get(label, {}).get(prop, prop)
                            transformation = self.config['data_transformations'].get(label, {}).get(prop)
                            if transformation:
                                value = self._apply_transformation(value, transformation)
                            if self._is_vector_node_property(label, prop):
                                self._record_explicit_property_type(
                                    self.node_property_types[label], mapped_prop, 'vector'
                                )
                            else:
                                self._record_property_type(self.node_property_types[label], mapped_prop, value)
                            row[mapped_prop] = str(value) if value is not None else ''
                        writer.writerow(row)
                        total_count += 1
                    skip += batch_size
                    print(f"  Processed {total_count} nodes...")
        print(f"  Exported {total_count} nodes to {csv_filename}")
        return csv_filename

    def extract_all_nodes(self, batch_size: int = 1000) -> List[str]:
        """Extract all nodes once each, preserving multi-label membership."""
        labels = self.get_node_labels()
        print(f"Found {len(labels)} node labels: {labels}")
        print("Using unique multi-label node extraction (no per-label duplication)")

        if self.tenant_mode and self.tenants:
            print(f"\n📦 Extracting nodes in multi-tenant mode ({len(self.tenants)} tenant(s))")
            csv_files: List[str] = []
            for tenant in self.tenants:
                print(f"\n--- Processing tenant: {tenant} ---")
                csv_files.extend(self.extract_unique_nodes(batch_size, tenant))
            return csv_files

        return self.extract_unique_nodes(batch_size, None)

    def _apply_transformation(self, value: Any, transformation: Dict[str, Any]) -> Any:
        """Apply data transformation based on the configuration"""
        if transformation['type'] == 'date_format':
            return self._transform_date_format(value, transformation)
        elif transformation['type'] == 'integer_to_string':
            return str(value)
        elif transformation['type'] == 'string_to_integer':
            try:
                return int(value) if value else 0
            except (ValueError, TypeError):
                return 0
        elif transformation['type'] == 'uppercase':
            return str(value).upper() if value else ''
        elif transformation['type'] == 'lowercase':
            return str(value).lower() if value else ''
        return value
    
    def _transform_date_format(self, value: Any, transformation: Dict[str, Any]) -> str:
        """Transform date format based on configuration"""
        if not value:
            return ''
        
        from_format = transformation.get('from_format', '')
        to_format = transformation.get('to_format', '')
        
        # Handle common date format transformations
        value_str = str(value)
        
        # Year only formats
        if from_format == 'yyyy' and to_format == 'yyyy-mm-dd':
            # Transform year to full date (e.g., 1964 -> 1964-01-01)
            return f"{value_str}-01-01"
        elif from_format == 'yyyy' and to_format == 'yyyy-mm':
            # Transform year to year-month (e.g., 1964 -> 1964-01)
            return f"{value_str}-01"
        elif from_format == 'yyyy-mm-dd' and to_format == 'yyyy':
            # Extract year from full date (e.g., 1964-03-15 -> 1964)
            return value_str.split('-')[0] if '-' in value_str else value_str
        elif from_format == 'yyyy-mm-dd' and to_format == 'dd/mm/yyyy':
            # Transform ISO date to DD/MM/YYYY format
            if '-' in value_str and len(value_str.split('-')) == 3:
                year, month, day = value_str.split('-')
                return f"{day}/{month}/{year}"
        elif from_format == 'dd/mm/yyyy' and to_format == 'yyyy-mm-dd':
            # Transform DD/MM/YYYY to ISO date format
            if '/' in value_str and len(value_str.split('/')) == 3:
                day, month, year = value_str.split('/')
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
        
        # If no specific transformation matches, try generic formatting
        if to_format and from_format != to_format:
            print(f"⚠️  Unsupported date transformation from '{from_format}' to '{to_format}' for value '{value}'. Using original value.")
        
        return value_str


    def extract_relationships_by_type(self, rel_type: str, batch_size: int = 1000, tenant: Optional[str] = None) -> str:
        """Extract all relationships of a specific type to CSV

        Args:
            rel_type: The relationship type to extract
            batch_size: Number of relationships to process per batch
            tenant: Optional tenant identifier for multi-tenant mode
        """
        # Map relationship type name
        mapped_rel_type = self.config['relationship_mappings'].get(rel_type, rel_type)
        tenant_info = f" (tenant: {tenant})" if tenant else ""
        print(f"Extracting relationships of type: {mapped_rel_type}{tenant_info}")

        # Get properties for this relationship type
        properties = sorted(list(self.get_relationship_properties(rel_type)))
        hinted_vector_props = sorted(self.vector_relationship_properties.get(rel_type, set()))
        for vector_prop in hinted_vector_props:
            if vector_prop not in properties:
                properties.append(vector_prop)
        properties = sorted(properties)

        # Filter out tenant property if in multi-tenant mode
        if tenant and self.tenant_mode == 'property' and self.tenant_filter_key:
            properties = [p for p in properties if p != self.tenant_filter_key]

        # Determine output directory and filename (filesystem-safe, type preserved in CSV)
        output_dir = self._get_tenant_dir(tenant) if tenant else self.output_dir
        file_key = safe_filename_component(mapped_rel_type)
        csv_filename = os.path.join(output_dir, f"edges_{file_key}.csv")

        # Track filename -> original/mapped type for loader consumers
        mapping_path = os.path.join(output_dir, 'relationship_type_files.json')
        type_file_map = {}
        if os.path.exists(mapping_path):
            try:
                with open(mapping_path, 'r', encoding='utf-8') as mf:
                    type_file_map = json.load(mf) or {}
            except Exception:
                type_file_map = {}
        type_file_map[os.path.basename(csv_filename)] = {
            'original_type': rel_type,
            'mapped_type': mapped_rel_type,
        }
        with open(mapping_path, 'w', encoding='utf-8') as mf:
            json.dump(type_file_map, mf, indent=2, ensure_ascii=False)

        with open(csv_filename, 'w', newline='', encoding='utf-8') as csvfile:
            # Map property names for headers
            mapped_properties = []
            for prop in properties:
                mapped_prop = self.config['relationship_property_mappings'].get(rel_type, {}).get(prop, prop)
                mapped_properties.append(mapped_prop)
                if self._is_vector_relationship_property(rel_type, prop):
                    self._record_explicit_property_type(
                        self.relationship_property_types[mapped_rel_type],
                        mapped_prop,
                        'vector',
                    )

            # CSV headers: source, target, type, and all mapped properties
            # Keep full multi-label endpoint labels for diagnostics; loader matches by id.
            headers = ['source', 'source_label', 'target', 'target_label', 'type'] + mapped_properties
            writer = csv.DictWriter(csvfile, fieldnames=headers)
            writer.writeheader()

            # Extract relationships in batches
            skip = 0
            total_count = 0
            rel_ident = escape_cypher_ident(rel_type)

            with self.driver.session(database=self.database) as session:
                while True:
                    # Build query with tenant filtering
                    if tenant and self.tenant_mode == 'property' and self.tenant_filter_key:
                        safe_tenant = tenant.replace("'", "\\'").replace("\\", "\\\\")
                        tenant_filter = f"WHERE a.{self.tenant_filter_key} = '{safe_tenant}' AND b.{self.tenant_filter_key} = '{safe_tenant}'"
                    else:
                        tenant_filter = ""

                    source_id_expr = self._cypher_node_id_expr('a')
                    target_id_expr = self._cypher_node_id_expr('b')
                    query = f"""
                    MATCH (a)-[r:{rel_ident}]->(b)
                    {tenant_filter}
                    RETURN elementId(a) as source_element_id,
                           {source_id_expr} as source_id,
                           labels(a) as source_label,
                           elementId(b) as target_element_id,
                           {target_id_expr} as target_id,
                           labels(b) as target_label,
                           type(r) as rel_type,
                           r
                    ORDER BY elementId(r)
                    SKIP {skip} LIMIT {batch_size}
                    """

                    try:
                        result = session.run(query)
                        records = list(result)
                    except Exception as e:
                        print(f"  ❌ Failed extracting relationship type '{rel_type}': {e}")
                        raise

                    if not records:
                        break

                    for record in records:
                        rel_data = dict(record['r'])
                        source_labels = [str(x) for x in (record['source_label'] or [])]
                        target_labels = [str(x) for x in (record['target_label'] or [])]
                        # Apply label mappings for endpoint label metadata
                        source_labels = [
                            self.config.get('label_mappings', {}).get(lbl, lbl) for lbl in source_labels
                        ]
                        target_labels = [
                            self.config.get('label_mappings', {}).get(lbl, lbl) for lbl in target_labels
                        ]
                        source_export_id = self._normalize_export_id(record['source_id'])
                        target_export_id = self._normalize_export_id(record['target_id'])
                        self._export_id_by_element_id[str(record['source_element_id'])] = source_export_id
                        self._export_id_by_element_id[str(record['target_element_id'])] = target_export_id
                        row = {
                            'source': source_export_id,
                            'source_label': ':'.join(source_labels),
                            'target': target_export_id,
                            'target_label': ':'.join(target_labels),
                            'type': mapped_rel_type,
                        }

                        # Add properties
                        for prop in properties:
                            value = rel_data.get(prop, '')
                            # Handle list/array properties
                            if isinstance(value, list):
                                value = ';'.join(str(v) for v in value)

                            # Map relationship properties using the config
                            mapped_prop = self.config['relationship_property_mappings'].get(rel_type, {}).get(prop, prop)

                            if self._is_vector_relationship_property(rel_type, prop):
                                self._record_explicit_property_type(
                                    self.relationship_property_types[mapped_rel_type],
                                    mapped_prop,
                                    'vector',
                                )
                            else:
                                self._record_property_type(
                                    self.relationship_property_types[mapped_rel_type], mapped_prop, value
                                )
                            row[mapped_prop] = str(value) if value is not None else ''

                        writer.writerow(row)
                        total_count += 1

                    skip += batch_size
                    print(f"  Processed {total_count} relationships...")

        print(f"  Exported {total_count} relationships to {csv_filename}")
        return csv_filename

    def extract_all_relationships(self, batch_size: int = 1000) -> List[str]:
        """Extract all relationships grouped by type"""
        rel_types = self.get_relationship_types()
        csv_files = []
        
        print(f"Found {len(rel_types)} relationship types: {rel_types}")
        
        # If multi-tenant mode, extract relationships for each tenant separately
        if self.tenant_mode and self.tenants:
            print(f"\n🔗 Extracting relationships in multi-tenant mode ({len(self.tenants)} tenant(s))")
            for tenant in self.tenants:
                print(f"\n--- Processing tenant: {tenant} ---")
                for rel_type in rel_types:
                    csv_file = self.extract_relationships_by_type(rel_type, batch_size, tenant)
                    csv_files.append(csv_file)
        else:
            for rel_type in rel_types:
                csv_file = self.extract_relationships_by_type(rel_type, batch_size)
                csv_files.append(csv_file)
        
        return csv_files
    
    def generate_falkordb_load_script(self, node_files: List[str], edge_files: List[str]) -> str:
        """Generate FalkorDB LOAD CSV commands"""
        script_filename = os.path.join(self.output_dir, "load_to_falkordb.cypher")
        
        with open(script_filename, 'w') as f:
            f.write("// FalkorDB Load Script - Generated from Neo4j Export\n")
            f.write("// Run this script in FalkorDB to import the data\n\n")
            
            # Load nodes
            f.write("// Load Nodes\n")
            for node_file in node_files:
                # Labels come from the CSV "labels" column (supports multi-label nodes).
                f.write(f"""
// {os.path.basename(node_file)}
// Multi-label aware load: labels column is colon-separated (e.g. Person:Actor)
// Ensure indexes on :Label(id) exist before edges (see create_indexes_falkordb.cypher / loader)
LOAD CSV WITH HEADERS FROM 'file:///{os.path.basename(node_file)}' AS row
MERGE (n {{id: row.id}})
SET n += row
REMOVE n.labels;
// NOTE: FalkorDB/Cypher loaders that support dynamic labels should SET labels from row.labels.
// The Python falkordb_csv_loader.py applies multi-labels directly during import.

""")
            
            # Load relationships
            f.write("// Load Relationships\n")
            for edge_file in edge_files:
                rel_type = os.path.basename(edge_file).replace('edges_', '').replace('.csv', '')
                
                # Read the first row of the CSV to get label information
                try:
                    with open(edge_file, 'r', encoding='utf-8') as csvfile:
                        reader = csv.DictReader(csvfile)
                        first_row = next(reader, None)
                        if first_row and 'source_label' in first_row and 'target_label' in first_row:
                            source_label = first_row.get('source_label', '')
                            target_label = first_row.get('target_label', '')
                            
                            f.write(f"""
LOAD CSV WITH HEADERS FROM 'file:///{os.path.basename(edge_file)}' AS row
// Match endpoints by stable migrated id (multi-label safe)
MATCH (a {{id: row.source}})
MATCH (b {{id: row.target}})
MERGE (a)-[r:{rel_type}]->(b)
SET r += row
REMOVE r.source, r.source_label, r.target, r.target_label, r.type;

""")
                        else:
                            # Fallback to generic approach without labels
                            f.write(f"""
LOAD CSV WITH HEADERS FROM 'file:///{os.path.basename(edge_file)}' AS row
MATCH (a {{id: row.source}})
MATCH (b {{id: row.target}})
MERGE (a)-[r:{rel_type}]->(b)
SET r += row
REMOVE r.source, r.target, r.type;

""")
                except Exception as e:
                    print(f"Warning: Could not read labels from {edge_file}: {e}")
                    # Fallback to generic approach without labels
                    f.write(f"""
LOAD CSV WITH HEADERS FROM 'file:///{os.path.basename(edge_file)}' AS row
MATCH (a {{id: row.source}})
MATCH (b {{id: row.target}})
MERGE (a)-[r:{rel_type}]->(b)
SET r += row
REMOVE r.source, r.target, r.type;

""")
        
        print(f"Generated FalkorDB load script: {script_filename}")
        return script_filename
    
    def generate_template_config(self, output_file: str = "template_migrate_config.json") -> str:
        """Generate a template migration config file based on Neo4j topology"""
        print("🔍 Analyzing Neo4j database topology...")
        
        # Get all labels and relationship types without filtering
        with self.driver.session(database=self.database) as session:
            # Get all node labels
            result = session.run("CALL db.labels()")
            all_labels = [record["label"] for record in result]
            
            # Get all relationship types
            result = session.run("CALL db.relationshipTypes()")
            all_rel_types = [record["relationshipType"] for record in result]
        
        print(f"  Found {len(all_labels)} node labels: {all_labels}")
        print(f"  Found {len(all_rel_types)} relationship types: {all_rel_types}")
        
        # Build template configuration
        template_config = {
            "label_mappings": {label: label for label in sorted(all_labels)},
            "property_mappings": {},
            "relationship_mappings": {rel_type: rel_type for rel_type in sorted(all_rel_types)},
            "relationship_property_mappings": {},
            "excluded_labels": [],
            "excluded_relationships": [],
            "excluded_properties": {},
            "data_transformations": {},
            "node_id_property": "id"
        }
        
        # Analyze properties for each label
        print("\n📊 Analyzing node properties...")
        with self.driver.session(database=self.database) as session:
            for label in sorted(all_labels):
                print(f"  Analyzing {label} properties...")
                
                # Get properties for this label
                query = f"""
                MATCH (n:{label})
                WITH n LIMIT 1000
                UNWIND keys(n) AS key
                RETURN DISTINCT key
                ORDER BY key
                """
                result = session.run(query)
                properties = [record["key"] for record in result]
                
                if properties:
                    template_config["property_mappings"][label] = {
                        prop: prop for prop in properties
                    }
                    template_config["excluded_properties"][label] = []
                    template_config["data_transformations"][label] = {}
                    
                    # Add example transformations for common property patterns
                    for prop in properties:
                        if prop.lower() in ['born', 'birth_year', 'birthdate']:
                            template_config["data_transformations"][label][prop] = {
                                "type": "date_format",
                                "from_format": "yyyy",
                                "to_format": "yyyy-mm-dd",
                                "description": "Transform birth year to full date format"
                            }
                        elif prop.lower() in ['released', 'release_year', 'year']:
                            template_config["data_transformations"][label][prop] = {
                                "type": "date_format",
                                "from_format": "yyyy",
                                "to_format": "yyyy-mm-dd",
                                "description": "Transform release year to full date format"
                            }
                    
                    print(f"    Found {len(properties)} properties: {properties}")
                else:
                    print(f"    No properties found for {label}")
        
        # Analyze relationship properties
        print("\n🔗 Analyzing relationship properties...")
        with self.driver.session(database=self.database) as session:
            for rel_type in sorted(all_rel_types):
                print(f"  Analyzing {rel_type} properties...")
                
                # Get properties for this relationship type
                query = f"""
                MATCH ()-[r:{rel_type}]->()
                WITH r LIMIT 1000
                UNWIND keys(r) AS key
                RETURN DISTINCT key
                ORDER BY key
                """
                result = session.run(query)
                properties = [record["key"] for record in result]
                
                if properties:
                    template_config["relationship_property_mappings"][rel_type] = {
                        prop: prop for prop in properties
                    }
                    print(f"    Found {len(properties)} properties: {properties}")
                else:
                    print(f"    No properties found for {rel_type}")
        
        # Add database statistics
        print("\n📈 Gathering database statistics...")
        with self.driver.session(database=self.database) as session:
            # Count nodes by label
            node_counts = {}
            for label in all_labels:
                result = session.run(f"MATCH (n:{label}) RETURN count(n) as count")
                count = result.single()["count"]
                node_counts[label] = count
                print(f"  {label}: {count} nodes")
            
            # Count relationships by type
            rel_counts = {}
            for rel_type in all_rel_types:
                result = session.run(f"MATCH ()-[r:{rel_type}]->() RETURN count(r) as count")
                count = result.single()["count"]
                rel_counts[rel_type] = count
                print(f"  {rel_type}: {count} relationships")
        
        # Add metadata to the config
        template_config["_metadata"] = {
            "generated_from": "Neo4j topology analysis",
            "database_stats": {
                "node_counts": node_counts,
                "relationship_counts": rel_counts,
                "total_nodes": sum(node_counts.values()),
                "total_relationships": sum(rel_counts.values())
            },
            "instructions": [
                "This is a template configuration generated from your Neo4j database topology.",
                "Customize the mappings, exclusions, and transformations as needed.",
                "Remove the '_metadata' section before using this config for migration.",
                "See MIGRATION_CONFIG_README.md for detailed configuration options."
            ]
        }
        
        # Save template to file
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(template_config, f, indent=2, ensure_ascii=False)
        
        print(f"\n✅ Generated template migration config: {output_file}")
        print(f"📝 Total elements analyzed:")
        print(f"   - {len(all_labels)} node labels")
        print(f"   - {len(all_rel_types)} relationship types")
        print(f"   - {sum(len(props) for props in template_config['property_mappings'].values())} node properties")
        print(f"   - {sum(len(props) for props in template_config['relationship_property_mappings'].values())} relationship properties")
        
        return output_file
    
    def extract_all(self, batch_size: int = 1000) -> Dict[str, Any]:
        """Extract all nodes, relationships, indexes, and constraints"""
        print("Starting Neo4j to CSV extraction...")
        
        node_files = self.extract_all_nodes(batch_size)
        edge_files = self.extract_all_relationships(batch_size)
        
        # Extract indexes and constraints
        index_file = self.extract_indexes_to_csv()
        constraint_file = self.extract_constraints_to_csv()
        
        # Generate scripts
        data_script_file = self.generate_falkordb_load_script(node_files, edge_files)
        index_script_file = self.generate_falkordb_index_script(index_file, constraint_file)
        type_schema_file = self.write_type_schema()
        
        return {
            'nodes': node_files,
            'edges': edge_files,
            'indexes': index_file,
            'constraints': constraint_file,
            'data_script': data_script_file,
            'index_script': index_script_file,
            'type_schema': type_schema_file
        }


def main():
    parser = argparse.ArgumentParser(
        description='Extract Neo4j data to CSV for FalkorDB migration',
        epilog='''
Examples:
  # Analyze topology and generate template config
  python3 neo4j_to_csv_extractor.py --password mypass --analyze-only
  
  # Generate custom template config file
  python3 neo4j_to_csv_extractor.py --password mypass --generate-template my_template.json
  
  # Extract data using existing config
  python3 neo4j_to_csv_extractor.py --password mypass --config migrate_config.json
  
  # Extract only nodes
  python3 neo4j_to_csv_extractor.py --password mypass --nodes-only
''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--uri', default='bolt://localhost:7687', help='Neo4j URI')
    parser.add_argument('--username', default='neo4j', help='Neo4j username')
    parser.add_argument('--password', required=True, help='Neo4j password')
    parser.add_argument('--database', default='neo4j', help='Neo4j database name')
    parser.add_argument('--batch-size', type=int, default=1000, help='Batch size for extraction')
    parser.add_argument('--nodes-only', action='store_true', help='Extract only nodes')
    parser.add_argument('--edges-only', action='store_true', help='Extract only relationships')
    parser.add_argument('--indexes-only', action='store_true', help='Extract only indexes and constraints')
    parser.add_argument('--config', help='Path to migration configuration JSON file')
    parser.add_argument('--generate-template', help='Generate template migration config file from Neo4j topology (specify output filename)')
    parser.add_argument('--analyze-only', action='store_true', help='Only analyze topology and generate template config, do not extract data')
    
    # Multi-tenant options
    parser.add_argument('--tenant-mode', choices=['label', 'property'], help='Enable multi-tenant mode: "label" (filter by node label) or "property" (filter by property value)')
    parser.add_argument('--tenant-filter', help='The label name or property name to use for tenant segregation (required with --tenant-mode)')
    
    args = parser.parse_args()
    
    # Validate multi-tenant arguments
    if args.tenant_mode and not args.tenant_filter:
        parser.error("--tenant-filter is required when --tenant-mode is specified")
    if args.tenant_filter and not args.tenant_mode:
        parser.error("--tenant-mode is required when --tenant-filter is specified")
    
    extractor = Neo4jToCSVExtractor(
        args.uri, 
        args.username, 
        args.password, 
        args.database, 
        args.config,
        tenant_mode=args.tenant_mode,
        tenant_filter_key=args.tenant_filter
    )
    
    try:
        # Handle template generation
        if args.generate_template or args.analyze_only:
            template_file = args.generate_template if args.generate_template else "template_migrate_config.json"
            extractor.generate_template_config(template_file)
            
            if args.analyze_only:
                print("\n📋 Analysis complete! Use the generated template to customize your migration.")
                return
        
        # Handle data extraction
        if args.nodes_only:
            node_files = extractor.extract_all_nodes(args.batch_size)
            type_schema_file = extractor.write_type_schema()
            print("\nNode extraction complete!")
            print(f"Node files: {len(node_files)}")
            print(f"Type schema file: {type_schema_file}")
        elif args.edges_only:
            edge_files = extractor.extract_all_relationships(args.batch_size)
            type_schema_file = extractor.write_type_schema()
            print("\nRelationship extraction complete!")
            print(f"Edge files: {len(edge_files)}")
            print(f"Type schema file: {type_schema_file}")
        elif args.indexes_only:
            index_file = extractor.extract_indexes_to_csv()
            constraint_file = extractor.extract_constraints_to_csv()
            index_script_file = extractor.generate_falkordb_index_script(index_file, constraint_file)
            print("\nIndex and constraint extraction complete!")
            print(f"Index file: {index_file}")
            print(f"Constraint file: {constraint_file}")
            print(f"Index creation script: {index_script_file}")
        else:
            result = extractor.extract_all(args.batch_size)
            print("\nExtraction complete!")
            print(f"Node files: {len(result['nodes'])}")
            print(f"Edge files: {len(result['edges'])}")
            print(f"Index file: {result['indexes']}")
            print(f"Constraint file: {result['constraints']}")
            print(f"Data load script: {result['data_script']}")
            print(f"Index creation script: {result['index_script']}")
            print(f"Type schema file: {result['type_schema']}")
    
    finally:
        extractor.close()


if __name__ == "__main__":
    main()
