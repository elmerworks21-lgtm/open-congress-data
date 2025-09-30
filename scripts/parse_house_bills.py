#!/usr/bin/env python3
"""
Parse House Bills data from raw JSON files and generate TOML files.

Usage:
    python parse_house_bills.py --congress=19
"""

import argparse
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional
import yaml


def generate_ulid() -> str:
    """Generate a ULID using the ulid-py library or fallback to a simple method."""
    try:
        from ulid import ULID
        return str(ULID())
    except ImportError:
        # Fallback: use a timestamp-based approach
        import time
        import random
        timestamp = int(time.time() * 1000)
        randomness = ''.join(random.choices('0123456789ABCDEFGHJKMNPQRSTVWXYZ', k=16))
        return f"{timestamp:013X}{randomness}"[:26]


def load_mapping_file(mapping_path: Path) -> Dict[str, str]:
    """Load the house bill number mapping file."""
    if not mapping_path.exists():
        return {}

    with open(mapping_path, 'r') as f:
        data = yaml.safe_load(f)
        if data is None:
            return {}
        # Convert keys to strings to ensure consistency
        return {str(k): v for k, v in data.items()}


def save_mapping_file(mapping_path: Path, mapping: Dict[str, str]):
    """Save the house bill number mapping file, sorted by bill number."""
    # Sort by numeric value of bill number
    sorted_mapping = dict(sorted(mapping.items(), key=lambda x: int(x[0])))

    with open(mapping_path, 'w') as f:
        yaml.dump(sorted_mapping, f, default_flow_style=False, sort_keys=False)


def extract_bill_number(bill_no: str) -> Optional[int]:
    """Extract numeric bill number from bill_no string (e.g., 'HB00001' -> 1)."""
    match = re.search(r'(\d+)', bill_no)
    if match:
        return int(match.group(1))
    return None


def parse_author_codes(author_string: str) -> List[str]:
    """Parse author codes from semicolon-separated string."""
    if not author_string:
        return []
    codes = [code.strip() for code in author_string.split(';') if code.strip()]
    return codes


def parse_coauthors(coauthors_list: Optional[List[Dict]]) -> List[str]:
    """Extract coauthor names from coauthors list."""
    if not coauthors_list:
        return []
    return [coauthor['name'] for coauthor in coauthors_list if 'name' in coauthor]


def parse_committee_codes(referrals: Optional[List[Dict]]) -> List[str]:
    """Extract committee referral codes from referrals list, removing duplicates while preserving order."""
    if not referrals:
        return []

    # Extract codes and deduplicate while preserving order
    codes = []
    seen = set()
    for ref in referrals:
        if 'referral' in ref:
            code = ref['referral']
            if code not in seen:
                codes.append(code)
                seen.add(code)

    return codes


def parse_status(status_string: str) -> Optional[tuple]:
    """Parse status string to extract status and date.

    Example: "Pending with the Committee on BASIC EDUCATION AND CULTURE since 2025-08-04"
    Returns: ("Pending in the Committee", "2025-08-04")
    """
    if not status_string:
        return None

    # Extract date from "since YYYY-MM-DD" pattern
    date_match = re.search(r'since (\d{4}-\d{2}-\d{2})', status_string)
    if not date_match:
        return None

    date = date_match.group(1)

    # Normalize status text
    if 'Pending with the Committee' in status_string or 'Pending in the Committee' in status_string:
        status = "Pending in the Committee"
    elif 'Approved' in status_string:
        status = "Approved"
    else:
        # Keep original status text up to "since"
        status = status_string.split(' since ')[0].strip()

    return (status, date)


def determine_bill_subtype(bill_type: str) -> str:
    """Determine bill subtype from bill_type string."""
    if 'Resolution' in bill_type:
        return 'HR'
    return 'HB'


def escape_toml_string(s: str) -> str:
    """Escape special characters in TOML strings."""
    if not s:
        return '""'
    # Replace backslashes first
    s = s.replace('\\', '\\\\')
    # Replace quotes
    s = s.replace('"', '\\"')
    # Remove problematic control characters
    s = s.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
    # Normalize whitespace
    s = ' '.join(s.split())
    return f'"{s}"'


def format_toml_array(items: List[str], quote_strings: bool = True) -> str:
    """Format a list as a TOML array."""
    if not items:
        return '[]'

    if quote_strings:
        formatted_items = [escape_toml_string(item)[1:-1] for item in items]  # Remove outer quotes
        return '[' + ','.join([f'"{item}"' for item in formatted_items]) + ']'
    else:
        return '[' + ','.join([str(item) for item in items]) + ']'


def parse_existing_toml(file_path: Path) -> Dict:
    """Parse existing TOML file to preserve its structure."""
    if not file_path.exists():
        return {}

    with open(file_path, 'r') as f:
        content = f.read()

    # Parse the structure to preserve history, status, committees sections
    result = {
        'id': None,
        'type': None,
        'subtype': None,
        'name': None,
        'meta': {},
        'history': [],
        'status': [],
        'committees': [],
        'raw_content': content
    }

    # Extract ID
    id_match = re.search(r'^id = "([^"]+)"', content, re.MULTILINE)
    if id_match:
        result['id'] = id_match.group(1)

    # Extract history sections
    history_pattern = r'\[\[history\]\](.*?)(?=\[\[|$)'
    for match in re.finditer(history_pattern, content, re.DOTALL):
        result['history'].append(match.group(0))

    # Extract status sections
    status_pattern = r'\[\[status\]\](.*?)(?=\[\[|$)'
    for match in re.finditer(status_pattern, content, re.DOTALL):
        result['status'].append(match.group(0))

    # Extract committees sections
    committees_pattern = r'\[\[committees\]\](.*?)(?=\[\[|$)'
    for match in re.finditer(committees_pattern, content, re.DOTALL):
        result['committees'].append(match.group(0))

    # Extract existing meta fields
    meta_section = re.search(r'\[meta\](.*?)(?=\[\[|\Z)', content, re.DOTALL)
    if meta_section:
        meta_content = meta_section.group(1)

        # Parse key fields from meta
        for field in ['senate_website_permalink', 'scope', 'subjects', 'authors_raw',
                      'date_filed', 'title', 'long_title', 'bill_number', 'congress',
                      'download_url_sources', 'abstract']:
            pattern = rf'^{field}\s*=\s*(.+)$'
            match = re.search(pattern, meta_content, re.MULTILINE)
            if match:
                result['meta'][field] = match.group(1).strip()

    return result


def generate_toml_content(bill_data: Dict, ulid: str, congress: int, existing: Optional[Dict] = None) -> str:
    """Generate TOML content from bill data, preserving existing data."""
    if existing is None:
        existing = {}

    bill_number = extract_bill_number(bill_data['bill_no'])
    subtype = determine_bill_subtype(bill_data.get('bill_type', 'House Bill'))

    # Extract data from JSON - handle None values
    title_full = (bill_data.get('title_full') or '').strip()
    abstract = (bill_data.get('abstract') or '').strip()
    date_filed = bill_data.get('date_filed') or ''
    author_codes = parse_author_codes(bill_data.get('author') or '')
    coauthors = parse_coauthors(bill_data.get('coauthors'))
    committee_codes = parse_committee_codes(bill_data.get('referrals'))
    primary_key = bill_data.get('id')
    text_as_filed = bill_data.get('text_as_filed') or ''
    status_info = parse_status(bill_data.get('status') or '')

    # Build TOML content
    lines = []
    lines.append(f'id = "{ulid}"')
    lines.append('type = "bill"')
    lines.append(f'subtype = "{subtype}"')
    lines.append(f'name = "{subtype}N-{bill_number:05d}"')
    lines.append('')
    lines.append('[meta]')
    lines.append(f'bill_number = {bill_number}')
    lines.append(f'congress = {congress}')

    # Preserve existing title/long_title/abstract if they exist
    existing_meta = existing.get('meta', {})
    if 'title' in existing_meta:
        lines.append(f'title = {existing_meta["title"]}')

    # Add date_filed from JSON if available, or preserve existing
    if date_filed:
        lines.append(f'date_filed = "{date_filed}"')
    elif 'date_filed' in existing_meta:
        lines.append(f'date_filed = {existing_meta["date_filed"]}')

    if 'long_title' in existing_meta:
        lines.append(f'long_title = {existing_meta["long_title"]}')

    if 'abstract' in existing_meta:
        lines.append(f'abstract = {existing_meta["abstract"]}')

    # Preserve scope or use default
    if 'scope' in existing_meta:
        lines.append(f'scope = {existing_meta["scope"]}')
    else:
        lines.append('scope = "National"')

    # Preserve subjects if they exist
    if 'subjects' in existing_meta:
        lines.append(f'subjects = {existing_meta["subjects"]}')
    else:
        lines.append('subjects = []')

    # Preserve authors_raw if it exists
    if 'authors_raw' in existing_meta:
        lines.append(f'authors_raw = {existing_meta["authors_raw"]}')

    # Add new congress_website fields
    if author_codes:
        lines.append(f'congress_website_author_codes = {format_toml_array(author_codes)}')

    if coauthors:
        lines.append(f'congress_website_coauthors_raw = {format_toml_array(coauthors)}')

    if committee_codes:
        lines.append(f'congress_website_committee_codes = {format_toml_array(committee_codes)}')

    if primary_key:
        lines.append(f'congress_website_primary_keys = [{primary_key}]')

    # Add congress_website_title from raw title_full
    if title_full:
        lines.append(f'congress_website_title = {escape_toml_string(title_full)}')

    # Add congress_website_abstract from raw abstract
    if abstract:
        lines.append(f'congress_website_abstract = {escape_toml_string(abstract)}')

    # Preserve senate_website_permalink if it exists
    if 'senate_website_permalink' in existing_meta:
        lines.append(f'senate_website_permalink = {existing_meta["senate_website_permalink"]}')

    # Handle download_url_sources - append if exists, create if not
    if text_as_filed:
        if 'download_url_sources' in existing_meta:
            # Parse existing download_url_sources array
            existing_sources_str = existing_meta['download_url_sources']
            # Extract URLs from the array string
            existing_urls = re.findall(r'"([^"]+)"', existing_sources_str)
            # Add new URL if it's not already there
            if text_as_filed not in existing_urls:
                existing_urls.append(text_as_filed)
            # Format as TOML array with proper quotes
            formatted_urls = ','.join([f'"{url}"' for url in existing_urls])
            lines.append(f'download_url_sources = [{formatted_urls}]')
        else:
            lines.append(f'download_url_sources = ["{text_as_filed}"]')

    lines.append('')

    # Preserve existing history sections
    if existing.get('history'):
        for history_block in existing['history']:
            lines.append(history_block.strip())
            lines.append('')

    # Add status section from JSON data (or preserve existing if no new status)
    if status_info:
        status_text, status_date = status_info
        lines.append('[[status]]')
        lines.append(f'status = "{status_text}"')
        lines.append(f'date = "{status_date}"')
        lines.append('')
    elif existing.get('status'):
        for status_block in existing['status']:
            lines.append(status_block.strip())
            lines.append('')

    # Preserve existing committees sections
    if existing.get('committees'):
        for committee_block in existing['committees']:
            lines.append(committee_block.strip())
            lines.append('')

    return '\n'.join(lines)


def update_toml_file(file_path: Path, bill_data: Dict, congress: int):
    """Update an existing TOML file with new data, preserving existing content."""
    # Parse existing file
    existing = parse_existing_toml(file_path)

    # Extract ULID from existing or generate new
    ulid = existing.get('id') or generate_ulid()

    # Generate new content with existing data preserved
    new_content = generate_toml_content(bill_data, ulid, congress, existing)

    # Write back
    with open(file_path, 'w') as f:
        f.write(new_content)


def process_bill(bill_data: Dict, congress_dir: Path, mapping: Dict[str, str], congress: int) -> str:
    """Process a single bill and create/update its TOML file.

    Returns: 'new', 'updated', or 'skipped'
    """
    bill_no = bill_data.get('bill_no', '')

    # Only process bills that start with "HB"
    if not bill_no.startswith('HB'):
        return 'skipped'

    bill_number = extract_bill_number(bill_no)
    if bill_number is None:
        return 'skipped'

    bill_key = str(bill_number)
    is_new = bill_key not in mapping

    # Check if bill already exists in mapping
    if bill_key in mapping:
        # Update existing file
        ulid = mapping[bill_key]
        file_path = congress_dir / f"{ulid}.toml"
        if file_path.exists():
            update_toml_file(file_path, bill_data, congress)
            return 'updated'
        else:
            # File missing, create new one
            ulid = generate_ulid()
            mapping[bill_key] = ulid
    else:
        # Create new file
        ulid = generate_ulid()
        mapping[bill_key] = ulid

    # Generate and write TOML file
    file_path = congress_dir / f"{ulid}.toml"

    # Check if file exists to preserve existing data
    existing = parse_existing_toml(file_path) if file_path.exists() else None
    content = generate_toml_content(bill_data, ulid, congress, existing)

    with open(file_path, 'w') as f:
        f.write(content)

    return 'new'


def main():
    parser = argparse.ArgumentParser(description='Parse House Bills data and generate TOML files')
    parser.add_argument('--congress', type=int, required=True, help='Congress number to process')
    args = parser.parse_args()

    congress = args.congress

    # Setup paths
    script_dir = Path(__file__).parent
    project_dir = script_dir.parent
    raw_dir = project_dir / 'raw'
    hb_dir = project_dir / 'data' / 'document' / 'hb'
    congress_dir = hb_dir / str(congress)
    mapping_file = congress_dir / '.house-bill-number-mapping.yml'

    # Create congress directory if it doesn't exist
    congress_dir.mkdir(parents=True, exist_ok=True)

    # Load existing mapping
    mapping = load_mapping_file(mapping_file)
    print(f"Loaded {len(mapping)} existing bills from mapping")

    # Find all batch files for this congress
    # Note: File names use congress number (e.g., 20), but inside the JSON
    # the congress field may differ (e.g., 103 for congress 20)
    batch_files = sorted(raw_dir.glob(f'house_congress_{congress}_bills_batch_*.json'))

    if not batch_files:
        print(f"No batch files found for congress {congress}")
        return

    print(f"Found {len(batch_files)} batch files")

    new_count = 0
    updated_count = 0
    skipped_count = 0

    # Process each batch file
    for batch_file in batch_files:
        print(f"Processing {batch_file.name}...")

        with open(batch_file, 'r') as f:
            data = json.load(f)

        # Extract rows from the JSON structure
        rows = data.get('data', {}).get('rows', [])

        for bill_data in rows:
            result = process_bill(bill_data, congress_dir, mapping, congress)
            if result == 'new':
                new_count += 1
            elif result == 'updated':
                updated_count += 1
            elif result == 'skipped':
                skipped_count += 1

    # Save updated mapping
    save_mapping_file(mapping_file, mapping)

    print(f"\nProcessing complete:")
    print(f"  New bills created: {new_count}")
    print(f"  Existing bills updated: {updated_count}")
    print(f"  Skipped (non-HB bills): {skipped_count}")
    print(f"  Total bills in mapping: {len(mapping)}")


if __name__ == '__main__':
    main()