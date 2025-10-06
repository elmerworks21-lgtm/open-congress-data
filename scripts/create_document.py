#!/usr/bin/env python3
"""
Create a new document (HB or SB) with proper ULID and mapping.

Usage:
    python scripts/create_document.py --congress 15 --type SB --number 100
"""
import argparse
import sys
from pathlib import Path
from typing import Dict, Optional, List
import yaml
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime
import toml


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
        return f"{timestamp:010X}{randomness}"


def get_mapping_file(congress_dir: Path, doc_type: str) -> Path:
    """Get the mapping file path based on document type."""
    if doc_type == 'HB':
        return congress_dir / '.house-bill-number-mapping.yml'
    elif doc_type == 'SB':
        return congress_dir / '.senate-bill-number-mapping.yml'
    else:
        raise ValueError(f"Invalid document type: {doc_type}")


def load_mapping(mapping_file: Path) -> Dict:
    """Load the mapping file or return empty dict if it doesn't exist."""
    if mapping_file.exists():
        with open(mapping_file, 'r') as f:
            return yaml.safe_load(f) or {}
    return {}


def save_mapping(mapping_file: Path, mapping: Dict):
    """Save the mapping file."""
    # Sort the mapping by bill number (as integers)
    sorted_mapping = dict(sorted(mapping.items(), key=lambda x: int(x[0])))

    with open(mapping_file, 'w') as f:
        yaml.dump(sorted_mapping, f, default_flow_style=False, sort_keys=False)


def get_congress_ordinal(congress: int) -> str:
    """Convert congress number to ordinal (e.g., 15 -> '15th')."""
    if 10 <= congress % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(congress % 10, 'th')
    return f"{congress}{suffix}"


def fetch_ldr_data(bill_number: int, congress: int) -> Optional[Dict]:
    """Fetch bill data from the Senate LDR website."""
    congress_ordinal = get_congress_ordinal(congress)

    # Try primary URL first
    url_primary = f"https://ldr.senate.gov.ph/bills/senate-bill-no-{bill_number}-{congress_ordinal}-congress-republic"

    try:
        response = requests.get(url_primary, timeout=30)
        response.raise_for_status()
        return parse_ldr_html(response.text, bill_number, congress)
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            # Try fallback URL without "-republic"
            url_fallback = f"https://ldr.senate.gov.ph/bills/senate-bill-no-{bill_number}-{congress_ordinal}-congress"
            try:
                print(f"Primary URL not found, trying fallback URL...", file=sys.stderr)
                response = requests.get(url_fallback, timeout=30)
                response.raise_for_status()
                return parse_ldr_html(response.text, bill_number, congress)
            except requests.RequestException as e2:
                print(f"Warning: Failed to fetch data from LDR website: {e2}", file=sys.stderr)
                return None
        else:
            print(f"Warning: Failed to fetch data from LDR website: {e}", file=sys.stderr)
            return None
    except requests.RequestException as e:
        print(f"Warning: Failed to fetch data from LDR website: {e}", file=sys.stderr)
        return None


def extract_field_text(soup: BeautifulSoup, field_name: str) -> Optional[str]:
    """Extract text from a field with the given class name."""
    field = soup.find('div', class_=f'field--name-{field_name}')
    if field:
        item = field.find('div', class_='field__item')
        if item:
            # Get text and clean it up
            text = item.get_text(strip=True)
            # Remove link text if it's just a link
            link = item.find('a')
            if link:
                return link.get_text(strip=True)
            return text
    return None


def extract_field_items(soup: BeautifulSoup, field_name: str) -> List[str]:
    """Extract multiple items from a field with the given class name."""
    items = []
    field = soup.find('div', class_=f'field--name-{field_name}')
    if field:
        field_items = field.find_all('div', class_='field__item')
        for item in field_items:
            link = item.find('a')
            if link:
                items.append(link.get_text(strip=True))
            else:
                text = item.get_text(strip=True)
                if text:
                    items.append(text)
    return items


def parse_date(date_str: Optional[str]) -> Optional[str]:
    """Parse date string to YYYY-MM-DD format."""
    if not date_str:
        return None

    try:
        # Try parsing "February 2, 2011" format
        dt = datetime.strptime(date_str, "%B %d, %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    try:
        # Try parsing "2012-12-04" format
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    return None


def parse_ldr_html(html: str, bill_number: int, congress: int) -> Dict:
    """Parse the LDR HTML and extract bill data."""
    soup = BeautifulSoup(html, 'html.parser')

    data = {
        'bill_number': bill_number,
        'congress': congress,
    }

    # Extract long title
    long_title = extract_field_text(soup, 'field-long-title')
    if long_title:
        data['long_title'] = long_title

    # Extract short title (this becomes the main title)
    short_title = extract_field_text(soup, 'field-short-title')
    if short_title:
        data['title'] = short_title

    # Extract author
    authors = extract_field_items(soup, 'field-author')
    if authors:
        data['authors_raw'] = authors[0] if len(authors) == 1 else authors

    # Extract co-authors
    coauthors = extract_field_items(soup, 'field-co-authors')
    if coauthors:
        data['coauthors_raw'] = coauthors

    # Extract sponsor
    sponsor = extract_field_text(soup, 'field-sponsor')
    if sponsor:
        data['sponsor'] = sponsor

    # Extract date filed
    date_filed = extract_field_text(soup, 'field-bill-date-of-filing')
    parsed_date = parse_date(date_filed)
    if parsed_date:
        data['date_filed'] = parsed_date

    # Extract scope
    scope = extract_field_text(soup, 'field-scope')
    if scope:
        data['scope'] = scope

    # Extract legislative status
    leg_status = extract_field_text(soup, 'field-legislative-stat')
    if leg_status:
        data['legislative_status'] = leg_status

    # Extract legislative status date
    leg_status_date_field = soup.find('div', class_='field--name-field-legislative-status-date')
    if leg_status_date_field:
        time_elem = leg_status_date_field.find('time')
        if time_elem and time_elem.get('datetime'):
            datetime_str = time_elem['datetime']
            # Extract just the date part (YYYY-MM-DD)
            parsed_date = parse_date(datetime_str.split('T')[0])
            if parsed_date:
                data['legislative_status_date'] = parsed_date

    # Extract session sequence number
    session_seq = extract_field_text(soup, 'field-session-sequence-no-')
    if session_seq:
        data['session_sequence_no'] = session_seq

    # Extract session type
    session_type = extract_field_text(soup, 'field-session-type')
    if session_type:
        data['session_type'] = session_type

    # Extract subjects
    subjects = extract_field_items(soup, 'field-subjects')
    if subjects:
        data['subjects'] = subjects

    # Extract primary committee
    primary_committee = extract_field_text(soup, 'field-primary-committee-taxonomy')
    if primary_committee:
        data['primary_committee'] = primary_committee

    # Extract committee report numbers
    committee_reports = extract_field_items(soup, 'field-committee-report-no')
    if committee_reports:
        data['committee_report_nos'] = committee_reports

    # Add Senate website permalink
    data['senate_website_permalink'] = f"https://web.senate.gov.ph/lis/bill_res.aspx?congress={congress}&q=SBN-{bill_number}"

    return data


def read_existing_toml(file_path: Path) -> Optional[Dict]:
    """Read an existing TOML file and return its contents."""
    if not file_path.exists():
        return None

    try:
        with open(file_path, 'r') as f:
            return toml.load(f)
    except Exception as e:
        print(f"Warning: Failed to read existing TOML file: {e}", file=sys.stderr)
        return None


def format_toml_value(value) -> str:
    """Format a value for TOML output."""
    if isinstance(value, str):
        # Escape quotes and backslashes
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    elif isinstance(value, list):
        if not value:
            return "[]"
        formatted_items = [format_toml_value(item) for item in value]
        return "[ " + ", ".join(formatted_items) + ",]"
    elif isinstance(value, bool):
        return "true" if value else "false"
    elif value is None:
        return '""'
    else:
        return str(value)


def create_toml_file(file_path: Path, ulid: str, doc_type: str, bill_number: int, congress: int, ldr_data: Optional[Dict] = None, existing_data: Optional[Dict] = None):
    """Create a TOML file with the given parameters."""
    subtype_name = f"{doc_type}N-{bill_number:05d}"

    # Start with basic fields
    lines = [
        f'id = "{ulid}"',
        'type = "bill"',
        f'subtype = "{doc_type}"',
        f'name = "{subtype_name}"',
        '',
        '[meta]',
        f'bill_number = {bill_number}',
        f'congress = {congress}',
    ]

    # Fields to preserve from existing data (manually edited)
    preserved_fields = [
        'senate_website_author_codes',
        'senate_website_committee_codes',
        'download_url_sources',
    ]

    # Add optional fields from LDR data
    if ldr_data:
        if 'title' in ldr_data:
            lines.append(f'title = {format_toml_value(ldr_data["title"])}')

        if 'date_filed' in ldr_data:
            lines.append(f'date_filed = {format_toml_value(ldr_data["date_filed"])}')

        if 'long_title' in ldr_data:
            lines.append(f'long_title = {format_toml_value(ldr_data["long_title"])}')

        if 'scope' in ldr_data:
            lines.append(f'scope = {format_toml_value(ldr_data["scope"])}')

        if 'subjects' in ldr_data:
            lines.append(f'subjects = {format_toml_value(ldr_data["subjects"])}')

        if 'authors_raw' in ldr_data:
            lines.append(f'authors_raw = {format_toml_value(ldr_data["authors_raw"])}')

        if 'coauthors_raw' in ldr_data:
            lines.append(f'coauthors_raw = {format_toml_value(ldr_data["coauthors_raw"])}')

        if 'sponsor' in ldr_data:
            lines.append(f'sponsor = {format_toml_value(ldr_data["sponsor"])}')

        if 'legislative_status' in ldr_data:
            lines.append(f'legislative_status = {format_toml_value(ldr_data["legislative_status"])}')

        if 'legislative_status_date' in ldr_data:
            lines.append(f'legislative_status_date = {format_toml_value(ldr_data["legislative_status_date"])}')

        if 'session_sequence_no' in ldr_data:
            lines.append(f'session_sequence_no = {format_toml_value(ldr_data["session_sequence_no"])}')

        if 'session_type' in ldr_data:
            lines.append(f'session_type = {format_toml_value(ldr_data["session_type"])}')

        if 'primary_committee' in ldr_data:
            lines.append(f'primary_committee = {format_toml_value(ldr_data["primary_committee"])}')

        if 'committee_report_nos' in ldr_data:
            lines.append(f'committee_report_nos = {format_toml_value(ldr_data["committee_report_nos"])}')

        # Add preserved fields from existing data, or empty arrays if new
        for field in preserved_fields:
            if existing_data and 'meta' in existing_data and field in existing_data['meta']:
                lines.append(f'{field} = {format_toml_value(existing_data["meta"][field])}')
            else:
                lines.append(f'{field} = []')

        if 'senate_website_permalink' in ldr_data:
            lines.append(f'senate_website_permalink = {format_toml_value(ldr_data["senate_website_permalink"])}')

    content = '\n'.join(lines) + '\n'

    with open(file_path, 'w') as f:
        f.write(content)


def main():
    parser = argparse.ArgumentParser(description='Create a new document (HB or SB)')
    parser.add_argument('--congress', type=int, required=True, help='Congress number')
    parser.add_argument('--type', choices=['HB', 'SB'], required=True, help='Document type (HB or SB)')
    parser.add_argument('--number', type=int, required=True, help='Bill number')
    parser.add_argument('--no-fetch', action='store_true', help='Skip fetching data from LDR website')
    parser.add_argument('--update', action='store_true', help='Update existing document if it exists')

    args = parser.parse_args()

    # Determine directory structure
    doc_type_lower = args.type.lower()
    congress_dir = Path('data/document') / doc_type_lower / str(args.congress)

    # Create directory if it doesn't exist
    congress_dir.mkdir(parents=True, exist_ok=True)

    # Get mapping file
    mapping_file = get_mapping_file(congress_dir, args.type)

    # Load existing mapping
    mapping = load_mapping(mapping_file)

    # Check if bill number already exists
    bill_key = str(args.number)
    existing_ulid = mapping.get(bill_key)
    existing_data = None

    if existing_ulid:
        if not args.update:
            print(f"Error: Bill number {args.number} already exists with ID {existing_ulid}", file=sys.stderr)
            print(f"Use --update flag to update the existing document", file=sys.stderr)
            sys.exit(1)

        # Read existing data for update
        file_path = congress_dir / f"{existing_ulid}.toml"
        existing_data = read_existing_toml(file_path)
        if existing_data:
            print(f"Updating existing document with ID {existing_ulid}", file=sys.stderr)
        ulid = existing_ulid
    else:
        # Generate new ULID
        ulid = generate_ulid()
        file_path = congress_dir / f"{ulid}.toml"

    # Only fetch data for Senate Bills
    ldr_data = None
    if args.type == 'SB' and not args.no_fetch:
        print(f"Fetching data from Senate LDR website for SBN-{args.number}...", file=sys.stderr)
        ldr_data = fetch_ldr_data(args.number, args.congress)
        if ldr_data:
            print(f"Successfully fetched data from LDR website", file=sys.stderr)
        else:
            print(f"Failed to fetch data from LDR website", file=sys.stderr)
            if not existing_ulid:
                print(f"Creating minimal document", file=sys.stderr)

    # Create or update TOML file
    create_toml_file(file_path, ulid, args.type, args.number, args.congress, ldr_data, existing_data)

    # Update mapping if new
    if not existing_ulid:
        mapping[bill_key] = ulid
        save_mapping(mapping_file, mapping)

    action = "Updated" if existing_ulid else "Successfully created"
    print(f"{action} {args.type} {args.number} for Congress {args.congress}")
    print(f"File: {file_path}")
    print(f"ID: {ulid}")


if __name__ == '__main__':
    main()
