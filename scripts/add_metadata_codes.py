#!/usr/bin/env python3
"""
Parse metadata from metadata/sb folder and add senate_website_author_codes and senate_website_committee_codes
to the corresponding TOML files in data/document/sb.
"""

import tomlkit
from pathlib import Path
from collections import defaultdict

def parse_metadata_file(filepath):
    """Parse a metadata text file and return the list of bill names."""
    bills = []
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                bills.append(line)
    return bills

def build_metadata_index(metadata_path, congress_num):
    """Build an index of bills to their author and committee codes."""
    index = {
        'authors': defaultdict(list),
        'committees': defaultdict(list)
    }

    congress_path = metadata_path / str(congress_num)

    # Parse author files
    author_path = congress_path / 'author'
    if author_path.exists():
        for author_file in author_path.glob('*.txt'):
            author_code = author_file.stem
            bills = parse_metadata_file(author_file)
            for bill in bills:
                index['authors'][bill].append(author_code)

    # Parse committee files
    committee_path = congress_path / 'committee'
    if committee_path.exists():
        for committee_file in committee_path.glob('*.txt'):
            committee_code = committee_file.stem
            bills = parse_metadata_file(committee_file)
            for bill in bills:
                index['committees'][bill].append(committee_code)

    return index

def update_toml_file(toml_path, author_codes, committee_codes, verbose=False):
    """Update a TOML file with senate_website_author_codes and senate_website_committee_codes."""
    with open(toml_path, 'r') as f:
        doc = tomlkit.load(f)

    # Track if we made any changes
    changed = False

    # Add the codes to the meta section
    if 'meta' in doc:
        # Check if we need to update senate_website_author_codes
        existing_authors = doc['meta'].get('senate_website_author_codes', [])
        if author_codes and sorted(author_codes) != sorted(existing_authors):
            doc['meta']['senate_website_author_codes'] = sorted(author_codes)
            changed = True
            if verbose:
                print(f"    Updated senate_website_author_codes: {sorted(author_codes)}")

        # Check if we need to update senate_website_committee_codes
        existing_committees = doc['meta'].get('senate_website_committee_codes', [])
        if committee_codes and sorted(committee_codes) != sorted(existing_committees):
            doc['meta']['senate_website_committee_codes'] = sorted(committee_codes)
            changed = True
            if verbose:
                print(f"    Updated senate_website_committee_codes: {sorted(committee_codes)}")

    # Only write if we made changes
    if changed:
        with open(toml_path, 'w') as f:
            f.write(tomlkit.dumps(doc))

    return changed

def process_congress(data_path, metadata_path, congress_num, verbose=False):
    """Process all bills for a specific congress."""
    print(f"\nProcessing congress {congress_num}...")

    # Build metadata index
    metadata_index = build_metadata_index(metadata_path, congress_num)

    if verbose:
        print(f"  Found {len(metadata_index['authors'])} bills with authors")
        print(f"  Found {len(metadata_index['committees'])} bills with committees")

    # Get congress data folder
    congress_data_path = data_path / str(congress_num)
    if not congress_data_path.exists():
        print(f"  No data folder for congress {congress_num}")
        return

    # Process each TOML file
    updated_count = 0
    skipped_count = 0
    total_count = 0

    for toml_file in congress_data_path.glob('*.toml'):
        total_count += 1

        # Read the TOML file to get the bill name
        with open(toml_file, 'r') as f:
            data = tomlkit.load(f)

        if 'meta' in data and 'bill_number' in data['meta']:
            # Construct the bill name (e.g., SBN-1)
            bill_num = data['meta']['bill_number']
            bill_name = f"SBN-{bill_num}"

            # Get the codes from the index
            author_codes = metadata_index['authors'].get(bill_name, [])
            committee_codes = metadata_index['committees'].get(bill_name, [])

            if author_codes or committee_codes:
                if verbose:
                    print(f"  Processing {toml_file.name} ({bill_name})")

                was_updated = update_toml_file(toml_file, author_codes, committee_codes, verbose)
                if was_updated:
                    updated_count += 1
                else:
                    skipped_count += 1
                    if verbose:
                        print(f"    No changes needed (already up to date)")

    print(f"  Summary: {updated_count} updated, {skipped_count} skipped (already up to date), {total_count} total files")

def main():
    import argparse

    # Set up argument parser
    parser = argparse.ArgumentParser(description='Add author and committee codes to TOML files from metadata')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose output')
    parser.add_argument('-c', '--congress', type=int, help='Process only a specific congress number')
    args = parser.parse_args()

    # Define paths - use script's parent directory as base
    base_path = Path(__file__).parent.parent
    data_path = base_path / 'data' / 'document' / 'sb'
    metadata_path = base_path / 'metadata' / 'sb'

    # Get list of congress numbers
    if args.congress:
        # Process only the specified congress
        congress_nums = [args.congress]
        print(f"Processing only congress {args.congress}")
    else:
        # Get all congress numbers
        congress_nums = []
        for folder in data_path.iterdir():
            if folder.is_dir() and folder.name.isdigit():
                congress_nums.append(int(folder.name))
        congress_nums.sort()
        print(f"Found {len(congress_nums)} congress folders: {congress_nums}")

    # Process each congress
    for congress_num in congress_nums:
        process_congress(data_path, metadata_path, congress_num, args.verbose)

    print("\n" + "="*60)
    print("Complete! The script is idempotent - you can run it multiple times safely.")
    print("Files are only rewritten if their author/committee codes have changed.")

if __name__ == '__main__':
    main()