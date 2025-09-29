#!/usr/bin/env python3
"""
Optimized sync script for Philippine Congress data to Neo4j database.

This version uses batch operations and transactions for much faster syncing.
Performance improvements:
- Batch UNWIND operations for multiple nodes at once
- Single transaction per batch instead of per node
- Reduced network round trips

Usage:
    python sync_to_neo4j.py                # Sync data without clearing
    python sync_to_neo4j.py --clear        # Clear database first (will prompt for confirmation)
    python sync_to_neo4j.py --clear --yes  # Clear database first (skip confirmation - for CI/CD)
"""

import os
import sys
import logging
import time
import yaml
from pathlib import Path
from typing import Dict, List
import tomlkit
from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class Neo4jSyncerOptimized:
    """Optimized handler for syncing data to Neo4j database using batch operations."""

    def __init__(self, uri: str, username: str, password: str):
        """Initialize Neo4j connection."""
        try:
            self.driver = GraphDatabase.driver(uri, auth=(username, password))
            self.driver.verify_connectivity()
            logger.info("Successfully connected to Neo4j")
        except Exception as e:
            logger.error(f"Failed to connect to Neo4j: {e}")
            raise

    def close(self):
        """Close the Neo4j driver."""
        if self.driver:
            self.driver.close()

    def clear_database(self, skip_confirmation=False):
        """Clear specific node types and their relationships from the database."""
        node_labels_to_clear = ["Congress", "Committee", "Person", "Group", "Document"]

        with self.driver.session() as session:
            try:
                # Count nodes that will be deleted
                label_conditions = " OR ".join(
                    [f"n:{label}" for label in node_labels_to_clear]
                )
                count_query = (
                    f"MATCH (n) WHERE {label_conditions} RETURN count(n) as count"
                )
                result = session.run(count_query)
                node_count = result.single()["count"]

                if node_count > 0:
                    logger.info(
                        f"Will delete nodes with labels: {', '.join(node_labels_to_clear)}"
                    )

                    if skip_confirmation:
                        logger.info(f"Auto-confirming deletion of {node_count} nodes (--yes flag provided)")
                        response = "yes"
                    else:
                        response = input(
                            f"This will delete {node_count} nodes and their relationships. Continue? (yes/no): "
                        )

                    if response.lower() == "yes":
                        delete_query = (
                            f"MATCH (n) WHERE {label_conditions} DETACH DELETE n"
                        )
                        session.run(delete_query)
                        logger.info(
                            f"Cleared {node_count} nodes of types: {', '.join(node_labels_to_clear)}"
                        )
                    else:
                        logger.info("Clear operation cancelled")
                else:
                    logger.info(
                        f"No nodes found with labels: {', '.join(node_labels_to_clear)}"
                    )
            except Neo4jError as e:
                logger.error(f"Failed to clear database: {e}")
                raise

    def sync_congresses_batch(self, congresses_dir: Path) -> Dict[int, str]:
        """Sync congress data to Neo4j using batch operations."""
        congress_mapping = {}
        congress_batch = []

        congress_files = sorted(congresses_dir.glob("*.toml"))
        logger.info(f"Found {len(congress_files)} congress files")

        # Load all congress data
        for file_path in congress_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = tomlkit.load(f)
                    congress_mapping[data["congress_number"]] = data["id"]
                    congress_batch.append(dict(data))
            except Exception as e:
                logger.error(f"Failed to load {file_path}: {e}")

        # Batch insert all congresses in a single transaction
        if congress_batch:
            with self.driver.session() as session:
                query = """
                UNWIND $batch AS congress
                MERGE (c:Congress {id: congress.id})
                SET c = congress
                """
                session.run(query, batch=congress_batch)
                logger.info(f"Successfully synced {len(congress_batch)} congresses in batch")

        return congress_mapping

    def sync_chambers_batch(self, chambers_dir: Path, congress_mapping: Dict[int, str]):
        """Sync chamber (Group) data to Neo4j using batch operations."""
        chamber_files = list(chambers_dir.glob("*.toml"))
        # Filter out the mapping files
        chamber_files = [f for f in chamber_files if not f.name.startswith('.')]
        total_files = len(chamber_files)
        logger.info(f"Found {total_files} chamber files")

        chambers_batch = []
        relationships_batch = []

        for file_path in sorted(chamber_files):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = tomlkit.load(f)

                    # Add Group label to chamber data
                    chamber_data = dict(data)
                    chambers_batch.append(chamber_data)

                    # Create relationship to congress
                    if data.get("congress") and data["congress"] in congress_mapping:
                        relationships_batch.append({
                            "chamber_id": data["id"],
                            "congress_id": congress_mapping[data["congress"]]
                        })

            except Exception as e:
                logger.error(f"Failed to load {file_path}: {e}")

        # Batch insert all chambers in a single transaction
        if chambers_batch:
            with self.driver.session() as session:
                # Create Group nodes with chamber data
                chamber_query = """
                UNWIND $batch AS chamber
                MERGE (g:Group {id: chamber.id})
                SET g = chamber
                """
                session.run(chamber_query, batch=chambers_batch)
                logger.info(f"Successfully synced {len(chambers_batch)} chambers")

                # Create relationships to Congress
                if relationships_batch:
                    relationship_query = """
                    UNWIND $batch AS rel
                    MATCH (g:Group {id: rel.chamber_id})
                    MATCH (c:Congress {id: rel.congress_id})
                    MERGE (g)-[:BELONGS_TO]->(c)
                    """
                    session.run(relationship_query, batch=relationships_batch)
                    logger.info(f"Created {len(relationships_batch)} chamber-congress relationships")

    def sync_committees_batch(self, committees_dir: Path, congress_mapping: Dict[int, str]):
        """Sync committee data to Neo4j using batch operations."""
        committee_files = list(committees_dir.glob("*.toml"))
        total_files = len(committee_files)
        logger.info(f"Found {total_files} committee files")

        batch_size = 50  # Process 50 committees at a time
        committees_batch = []
        relationships_batch = []

        for idx, file_path in enumerate(sorted(committee_files), 1):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = tomlkit.load(f)

                    # Prepare committee data (exclude congresses field)
                    committee_data = {k: v for k, v in data.items() if k != "congresses"}
                    committees_batch.append(committee_data)

                    # Prepare relationship data
                    for congress_num in data.get("congresses", []):
                        if congress_num in congress_mapping:
                            relationships_batch.append({
                                "committee_id": data["id"],
                                "congress_id": congress_mapping[congress_num]
                            })

                # Process batch when it reaches the size limit or at the end
                if len(committees_batch) >= batch_size or idx == total_files:
                    self._process_committee_batch(committees_batch, relationships_batch)

                    logger.info(f"Progress: {idx}/{total_files} committees synced")
                    committees_batch = []
                    relationships_batch = []

            except Exception as e:
                logger.error(f"Failed to process {file_path.name}: {e}")

    def _process_committee_batch(self, committees_batch: List[dict], relationships_batch: List[dict]):
        """Process a batch of committees and their relationships."""
        if not committees_batch:
            return

        with self.driver.session() as session:
            # Batch create/update committees
            committee_query = """
            UNWIND $batch AS committee
            MERGE (c:Committee {id: committee.id})
            SET c = committee
            """
            session.run(committee_query, batch=committees_batch)

            # Batch create relationships
            if relationships_batch:
                relationship_query = """
                UNWIND $batch AS rel
                MATCH (com:Committee {id: rel.committee_id})
                MATCH (con:Congress {id: rel.congress_id})
                MERGE (com)-[:BELONGS_TO]->(con)
                """
                session.run(relationship_query, batch=relationships_batch)

    def sync_people_batch(self, people_dir: Path, congress_mapping: Dict[int, str]):
        """Sync person data to Neo4j using batch operations."""
        people_files = list(people_dir.glob("*.toml"))
        total_files = len(people_files)
        logger.info(f"Found {total_files} people files")

        batch_size = 50  # Process 50 people at a time
        people_batch = []
        relationships_batch = []
        start_time = time.time()

        for idx, file_path in enumerate(sorted(people_files), 1):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = tomlkit.load(f)

                    # Prepare person data (exclude memberships and congresses)
                    person_data = {k: v for k, v in data.items() if k not in ["memberships", "congresses"]}
                    people_batch.append(person_data)

                    # Handle memberships structure - only create chamber relationships
                    memberships = data.get("memberships", [])
                    for membership in memberships:
                        # For chamber memberships, create relationship to the chamber Group node
                        if membership.get("type") == "chamber":
                            congress_num = membership.get("congress")
                            subtype = membership.get("subtype")  # 'house' or 'senate'

                            if congress_num and subtype:
                                relationships_batch.append({
                                    "person_id": data["id"],
                                    "congress": congress_num,
                                    "subtype": subtype,
                                    "type": "chamber",
                                    "position": membership.get("position", "")
                                })

                # Process batch when it reaches the size limit or at the end
                if len(people_batch) >= batch_size or idx == total_files:
                    self._process_people_batch(people_batch, relationships_batch)

                    elapsed = time.time() - start_time
                    rate = idx / elapsed
                    eta = (total_files - idx) / rate if rate > 0 else 0

                    logger.info(f"Progress: {idx}/{total_files} people synced ({rate:.1f} people/sec, ETA: {eta:.0f}s)")
                    people_batch = []
                    relationships_batch = []

            except Exception as e:
                logger.error(f"Failed to process {file_path.name}: {e}")

        total_time = time.time() - start_time
        logger.info(f"Successfully synced people in {total_time:.1f} seconds")

    def _process_people_batch(self, people_batch: List[dict], relationships_batch: List[dict]):
        """Process a batch of people and their relationships."""
        if not people_batch:
            return

        with self.driver.session() as session:
            # Batch create/update people
            people_query = """
            UNWIND $batch AS person
            MERGE (p:Person {id: person.id})
            SET p = person
            """
            session.run(people_query, batch=people_batch)

            # Create chamber relationships (Person -> Group)
            if relationships_batch:
                chamber_query = """
                UNWIND $batch AS rel
                MATCH (p:Person {id: rel.person_id})
                MATCH (g:Group {type: 'chamber', congress: rel.congress, subtype: rel.subtype})
                MERGE (p)-[r:MEMBER_OF]->(g)
                SET r.position = rel.position
                """
                session.run(chamber_query, batch=relationships_batch)

    def create_indexes(self):
        """Create indexes for better query performance."""
        with self.driver.session() as session:
            indexes = [
                "CREATE INDEX IF NOT EXISTS FOR (c:Congress) ON (c.id)",
                "CREATE INDEX IF NOT EXISTS FOR (c:Congress) ON (c.congress_number)",
                "CREATE INDEX IF NOT EXISTS FOR (com:Committee) ON (com.id)",
                "CREATE INDEX IF NOT EXISTS FOR (com:Committee) ON (com.name)",
                "CREATE INDEX IF NOT EXISTS FOR (p:Person) ON (p.id)",
                "CREATE INDEX IF NOT EXISTS FOR (p:Person) ON (p.full_name)",
                "CREATE INDEX IF NOT EXISTS FOR (p:Person) ON (p.last_name)",
                "CREATE INDEX IF NOT EXISTS FOR (g:Group) ON (g.id)",
                "CREATE INDEX IF NOT EXISTS FOR (g:Group) ON (g.type)",
                "CREATE INDEX IF NOT EXISTS FOR (g:Group) ON (g.congress)",
                "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.id)",
                "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.name)",
                "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.congress)",
                "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.bill_number)",
            ]

            for index_query in indexes:
                try:
                    session.run(index_query)
                except Exception as e:
                    logger.warning(f"Index creation warning: {e}")

            logger.info("Database indexes created/verified")

    def get_statistics(self):
        """Get statistics about the synced data."""
        with self.driver.session() as session:
            stats = {}

            # Count nodes
            for label in ["Congress", "Committee", "Person", "Group", "Document"]:
                result = session.run(f"MATCH (n:{label}) RETURN count(n) as count")
                stats[label] = result.single()["count"]

            # Count chamber nodes specifically
            result = session.run("MATCH (g:Group {type: 'chamber'}) RETURN count(g) as count")
            stats["Chamber"] = result.single()["count"]

            # Count relationships
            for rel_type in ["BELONGS_TO", "MEMBER_OF", "AUTHORED", "FILED_IN"]:
                result = session.run(
                    f"MATCH ()-[r:{rel_type}]->() RETURN count(r) as count"
                )
                stats[rel_type] = result.single()["count"]

            # Count chamber memberships by type
            result = session.run("""
                MATCH (p:Person)-[r:MEMBER_OF]->(g:Group {type: 'chamber', subtype: 'senate'})
                RETURN count(DISTINCT r) as count
            """)
            stats["senate_memberships"] = result.single()["count"]

            result = session.run("""
                MATCH (p:Person)-[r:MEMBER_OF]->(g:Group {type: 'chamber', subtype: 'house'})
                RETURN count(DISTINCT r) as count
            """)
            stats["house_memberships"] = result.single()["count"]

            return stats

    def load_senate_website_key_mapping(self, people_dir: Path) -> Dict[str, str]:
        """Load the Senate website key to person ID mapping."""
        mapping_file = people_dir / ".senate-website-key-mapping.yml"
        if not mapping_file.exists():
            logger.warning(f"Senate website key mapping file not found: {mapping_file}")
            return {}

        with open(mapping_file, 'r', encoding='utf-8') as f:
            mapping = yaml.safe_load(f)

        logger.info(f"Loaded {len(mapping)} Senate website key mappings")
        return mapping

    def sync_documents_batch(self, document_dir: Path, congress_mapping: Dict[int, str], senate_key_mapping: Dict[str, str]):
        """Sync document data to Neo4j using batch operations."""
        # Collect all files from both HB and SB first
        all_bill_files = []

        for bill_type in ['hb', 'sb']:
            bill_dir = document_dir / bill_type
            if not bill_dir.exists():
                logger.warning(f"Bill directory not found: {bill_dir}")
                continue

            # Collect all document files across all congresses
            for congress_dir in sorted(bill_dir.iterdir()):
                if congress_dir.is_dir() and congress_dir.name.isdigit():
                    toml_files = list(congress_dir.glob('*.toml'))
                    # Add bill type to each file for tracking
                    all_bill_files.extend([(bill_type, f) for f in toml_files])

        total_files = len(all_bill_files)
        logger.info(f"Found {total_files} total document files (HB + SB)")

        if not all_bill_files:
            logger.warning("No document files found to process")
            return

        # Increased batch size for faster processing
        batch_size = 200  # Increased from 50 to 200
        documents_batch = []
        author_relationships_batch = []
        congress_relationships_batch = []
        start_time = time.time()

        # Process all files in a single loop
        for idx, (bill_type, file_path) in enumerate(all_bill_files, 1):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = tomlkit.load(f)

                # Extract document data
                doc_data = {
                    'id': data.get('id'),
                    'type': data.get('type'),
                    'subtype': data.get('subtype'),
                    'name': data.get('name')
                }

                # Add meta fields if present
                if 'meta' in data:
                    meta = data['meta']
                    doc_data.update({
                        'bill_number': meta.get('bill_number'),
                        'congress': meta.get('congress'),
                        'title': meta.get('title'),
                        'date_filed': meta.get('date_filed'),
                        'long_title': meta.get('long_title'),
                        'scope': meta.get('scope'),
                        'subjects': meta.get('subjects', []),
                        'authors_raw': meta.get('authors_raw'),
                        'senate_website_permalink': meta.get('senate_website_permalink'),
                        'download_url_sources': meta.get('download_url_sources', [])
                    })

                    # Create congress relationship
                    if meta.get('congress') and meta['congress'] in congress_mapping:
                        congress_relationships_batch.append({
                            'document_id': data['id'],
                            'congress_id': congress_mapping[meta['congress']]
                        })

                    # Create author relationships using senate_website_author_codes
                    if meta.get('senate_website_author_codes'):
                        for author_code in meta['senate_website_author_codes']:
                            if author_code in senate_key_mapping:
                                author_relationships_batch.append({
                                    'document_id': data['id'],
                                    'person_id': senate_key_mapping[author_code]
                                })

                documents_batch.append(doc_data)

                # Process batch when it reaches the size limit or at the end
                if len(documents_batch) >= batch_size or idx == total_files:
                    self._process_document_batch(documents_batch, author_relationships_batch, congress_relationships_batch)

                    # Calculate and display progress with ETA
                    elapsed = time.time() - start_time
                    rate = idx / elapsed if elapsed > 0 else 0
                    eta = (total_files - idx) / rate if rate > 0 else 0

                    logger.info(f"Progress: {idx}/{total_files} documents synced ({rate:.1f} docs/sec, ETA: {eta:.0f}s)")
                    documents_batch = []
                    author_relationships_batch = []
                    congress_relationships_batch = []

            except Exception as e:
                logger.error(f"Failed to process {file_path.name}: {e}")

        total_time = time.time() - start_time
        logger.info(f"Document sync completed in {total_time:.1f} seconds ({total_files / total_time:.1f} docs/sec)")

    def _process_document_batch(self, documents_batch: List[dict], author_relationships_batch: List[dict], congress_relationships_batch: List[dict]):
        """Process a batch of documents and their relationships."""
        if not documents_batch:
            return

        with self.driver.session() as session:
            # Use a single transaction for all operations
            with session.begin_transaction() as tx:
                # Batch create/update documents
                document_query = """
                UNWIND $batch AS document
                MERGE (d:Document {id: document.id})
                SET d = document
                """
                tx.run(document_query, batch=documents_batch)

                # Create congress relationships with optimized matching
                if congress_relationships_batch:
                    # Group by congress_id for more efficient matching
                    congress_groups = {}
                    for rel in congress_relationships_batch:
                        if rel['congress_id'] not in congress_groups:
                            congress_groups[rel['congress_id']] = []
                        congress_groups[rel['congress_id']].append(rel['document_id'])

                    # Process each congress group
                    for congress_id, doc_ids in congress_groups.items():
                        congress_query = """
                        MATCH (c:Congress {id: $congress_id})
                        UNWIND $doc_ids AS doc_id
                        MATCH (d:Document {id: doc_id})
                        MERGE (d)-[:FILED_IN]->(c)
                        """
                        tx.run(congress_query, congress_id=congress_id, doc_ids=doc_ids)

                # Create author relationships with optimized matching
                if author_relationships_batch:
                    # Group by person_id for more efficient matching
                    person_groups = {}
                    for rel in author_relationships_batch:
                        if rel['person_id'] not in person_groups:
                            person_groups[rel['person_id']] = []
                        person_groups[rel['person_id']].append(rel['document_id'])

                    # Process each person group
                    for person_id, doc_ids in person_groups.items():
                        author_query = """
                        MATCH (p:Person {id: $person_id})
                        UNWIND $doc_ids AS doc_id
                        MATCH (d:Document {id: doc_id})
                        MERGE (p)-[:AUTHORED]->(d)
                        """
                        tx.run(author_query, person_id=person_id, doc_ids=doc_ids)

                # Commit the transaction
                tx.commit()


def main():
    """Main execution function."""
    load_dotenv()

    # Get configuration
    neo4j_uri = os.getenv("NEO4J_URI")
    neo4j_username = os.getenv("NEO4J_USERNAME")
    neo4j_password = os.getenv("NEO4J_PASSWORD")

    if not all([neo4j_uri, neo4j_username, neo4j_password]):
        logger.error(
            "Missing required environment variables. Please check your .env file."
        )
        logger.error("Required: NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD")
        sys.exit(1)

    # Get project root directory
    project_root = Path(__file__).parent.parent
    congresses_dir = project_root / "data" / "congress"
    committees_dir = project_root / "data" / "committee"
    people_dir = project_root / "data" / "person"
    chambers_dir = project_root / "data" / "group" / "chamber"
    document_dir = project_root / "data" / "document"

    # Verify directories exist
    for dir_path in [congresses_dir, committees_dir, people_dir]:
        if not dir_path.exists():
            logger.error(f"Directory not found: {dir_path}")
            sys.exit(1)

    # Chambers directory is optional for now (during migration)
    if not chambers_dir.exists():
        logger.warning(f"Chambers directory not found: {chambers_dir}. Skipping chamber sync.")
        chambers_dir = None

    # Initialize syncer
    syncer = None
    try:
        syncer = Neo4jSyncerOptimized(neo4j_uri, neo4j_username, neo4j_password)

        # Parse command line arguments
        clear_db = "--clear" in sys.argv
        skip_confirmation = "--yes" in sys.argv

        # Optional: Clear database
        if clear_db:
            syncer.clear_database(skip_confirmation=skip_confirmation)

        # Create indexes first for better performance
        logger.info("Creating database indexes...")
        syncer.create_indexes()

        # Track total time
        total_start = time.time()

        # Sync data in order using batch operations
        logger.info("Starting optimized data sync...")

        # 1. Sync Congresses first (they're referenced by committees and people)
        logger.info("Syncing congresses...")
        congress_start = time.time()
        congress_mapping = syncer.sync_congresses_batch(congresses_dir)
        logger.info(f"Congress sync completed in {time.time() - congress_start:.1f}s")

        # 2. Sync Chambers (Group nodes) if directory exists
        if chambers_dir:
            logger.info("Syncing chambers...")
            chamber_start = time.time()
            syncer.sync_chambers_batch(chambers_dir, congress_mapping)
            logger.info(f"Chamber sync completed in {time.time() - chamber_start:.1f}s")

        # 3. Sync Committees
        logger.info("Syncing committees...")
        committee_start = time.time()
        syncer.sync_committees_batch(committees_dir, congress_mapping)
        logger.info(f"Committee sync completed in {time.time() - committee_start:.1f}s")

        # 4. Sync People
        logger.info("Syncing people...")
        people_start = time.time()
        syncer.sync_people_batch(people_dir, congress_mapping)
        logger.info(f"People sync completed in {time.time() - people_start:.1f}s")

        # 5. Sync Documents (after Congress, Group, and Person nodes)
        if document_dir.exists():
            logger.info("Loading Senate website key mapping...")
            senate_key_mapping = syncer.load_senate_website_key_mapping(people_dir)

            logger.info("Syncing documents...")
            document_start = time.time()
            syncer.sync_documents_batch(document_dir, congress_mapping, senate_key_mapping)
            logger.info(f"Document sync completed in {time.time() - document_start:.1f}s")
        else:
            logger.warning(f"Document directory not found: {document_dir}. Skipping document sync.")

        # Display statistics
        stats = syncer.get_statistics()
        total_time = time.time() - total_start

        logger.info("\n=== Sync Complete ===")
        logger.info(f"Total sync time: {total_time:.1f} seconds")
        logger.info(f"Congresses: {stats['Congress']}")
        logger.info(f"Chambers (Group): {stats.get('Chamber', 0)}")
        logger.info(f"Committees: {stats['Committee']}")
        logger.info(f"People: {stats['Person']}")
        logger.info(f"Documents: {stats.get('Document', 0)}")
        logger.info(f"BELONGS_TO relationships: {stats['BELONGS_TO']}")
        logger.info(f"MEMBER_OF relationships: {stats.get('MEMBER_OF', 0)}")
        logger.info(f"  - Senate memberships: {stats.get('senate_memberships', 0)}")
        logger.info(f"  - House memberships: {stats.get('house_memberships', 0)}")
        logger.info(f"AUTHORED relationships: {stats.get('AUTHORED', 0)}")
        logger.info(f"FILED_IN relationships: {stats.get('FILED_IN', 0)}")

    except Exception as e:
        logger.error(f"Sync failed: {e}")
        sys.exit(1)
    finally:
        if syncer:
            syncer.close()


if __name__ == "__main__":
    main()