#!/usr/bin/env python3
"""
Upload PDFs from congressional bill data to Cloudflare R2 bucket.

This script reads bill metadata from TOML files, downloads PDFs from specified URLs,
and uploads them to an R2 bucket using the S3-compatible API.
"""

import argparse
import os
import sys
import time
import re
from pathlib import Path
from typing import Optional, List, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import boto3
import requests
import tomlkit
from dotenv import load_dotenv


@dataclass
class BillUploadTask:
    """Represents a bill PDF upload task."""
    bill_number: int
    document_id: str
    bill_name: str
    download_url: Optional[str]


class UploadTracker:
    """Manages tracking files for upload status."""

    def __init__(self, base_path: Path):
        self.base_path = base_path
        self.finished_file = base_path / ".r2-finished-uploading-pdfs"
        self.missing_file = base_path / ".r2-missing-pdfs"
        self.erroring_file = base_path / ".r2-erroring-pdfs"

        # Ensure tracking files exist
        for file in [self.finished_file, self.missing_file, self.erroring_file]:
            file.touch(exist_ok=True)

    def load_finished(self) -> Set[str]:
        """Load set of finished bill numbers."""
        if self.finished_file.exists():
            return set(self.finished_file.read_text().strip().split('\n'))
        return set()

    def mark_finished(self, bill_number: int):
        """Mark a bill number as finished."""
        with open(self.finished_file, 'a') as f:
            f.write(f"{bill_number:05d}\n")

    def mark_missing(self, bill_number: int):
        """Mark a bill number as missing."""
        with open(self.missing_file, 'a') as f:
            f.write(f"{bill_number:05d}\n")

    def mark_erroring(self, bill_number: int):
        """Mark a bill number as erroring."""
        with open(self.erroring_file, 'a') as f:
            f.write(f"{bill_number:05d}\n")


class PDFUploader:
    """Handles PDF downloading and uploading to R2."""

    def __init__(self, endpoint_url: str, access_key: str, secret_key: str, bucket_name: str):
        self.bucket_name = bucket_name
        self.s3_client = boto3.client(
            's3',
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name='auto',  # R2 uses 'auto' for automatic region
        )

    def download_pdf_with_retry(self, url: str, max_retries: int = 5) -> Optional[bytes]:
        """Download PDF with exponential backoff retry logic."""
        delay = 5  # Start with 5 seconds

        for attempt in range(max_retries):
            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                return response.content
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    print(f"  Download failed (attempt {attempt + 1}/{max_retries}): {e}")
                    print(f"  Retrying in {delay} seconds...")
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff
                else:
                    print(f"  Download failed after {max_retries} attempts: {e}")
                    return None

        return None

    def file_exists_in_r2(self, key: str) -> bool:
        """Check if a file already exists in R2 bucket."""
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except self.s3_client.exceptions.ClientError as e:
            if e.response['Error']['Code'] == '404':
                return False
            # Other errors, re-raise
            raise
        except Exception:
            return False

    def upload_to_r2(self, pdf_content: bytes, key: str) -> bool:
        """Upload PDF content to R2 bucket."""
        try:
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=pdf_content,
                ContentType='application/pdf'
            )
            return True
        except Exception as e:
            print(f"  Upload to R2 failed: {e}")
            return False


def parse_yaml_line_by_line(yaml_file: Path, limit: Optional[int] = None) -> List[tuple]:
    """Parse YAML mapping file line by line without loading into memory."""
    mappings = []

    with open(yaml_file, 'r') as f:
        for line in f:
            # Match pattern like: '123': DOCUMENT_ID
            match = re.match(r"'(\d+)':\s+(\w+)", line.strip())
            if match:
                bill_number = int(match.group(1))
                document_id = match.group(2)
                mappings.append((bill_number, document_id))

                if limit and len(mappings) >= limit:
                    break

    return mappings


def get_bill_metadata(bill_type: str, congress: int, document_id: str) -> Optional[dict]:
    """Read and parse TOML file for a bill."""
    toml_path = Path(f"data/document/{bill_type}/{congress}/{document_id}.toml")

    if not toml_path.exists():
        return None

    try:
        with open(toml_path, 'r') as f:
            data = tomlkit.load(f)
        return data
    except Exception as e:
        print(f"  Error reading TOML file {toml_path}: {e}")
        return None


def filter_download_url(urls: List[str], bill_type: str) -> Optional[str]:
    """Filter download URLs based on bill type."""
    if bill_type == 'hb':
        # House bills: only use docs.congress.hrep.online
        for url in urls:
            if 'docs.congress.hrep.online' in url:
                return url
    elif bill_type == 'sb':
        # Senate bills: only use web.senate.gov.ph/lisdata/
        for url in urls:
            if 'web.senate.gov.ph/lisdata/' in url:
                return url

    return None


def create_upload_task(bill_type: str, congress: int, bill_number: int, document_id: str) -> BillUploadTask:
    """Create an upload task by reading bill metadata."""
    metadata = get_bill_metadata(bill_type, congress, document_id)

    if not metadata:
        return BillUploadTask(bill_number, document_id, "", None)

    # Extract bill name
    bill_name = metadata.get('name', '')

    # Extract and filter download URLs
    download_urls = metadata.get('meta', {}).get('download_url_sources', [])
    download_url = filter_download_url(download_urls, bill_type)

    return BillUploadTask(bill_number, document_id, bill_name, download_url)


def process_bill(task: BillUploadTask, bill_type: str, congress: int,
                uploader: PDFUploader, tracker: UploadTracker, force: bool = False) -> bool:
    """Process a single bill: download PDF and upload to R2."""
    bill_num_str = f"{task.bill_number:05d}"

    # Skip if already finished (unless forced)
    if not force and bill_num_str in tracker.load_finished():
        print(f"Skipping {task.bill_name or f'Bill #{task.bill_number}'} (already uploaded)")
        return True

    print(f"Processing {task.bill_name or f'Bill #{task.bill_number}'} (bill #{task.bill_number})...")

    # Check if bill name is valid
    if not task.bill_name:
        print(f"  No bill name found for bill #{task.bill_number}")
        tracker.mark_erroring(task.bill_number)
        return False

    # Check if download URL is available
    if not task.download_url:
        print(f"  No download URL found for {task.bill_name}")
        tracker.mark_missing(task.bill_number)
        return False

    # Check if file already exists in R2 (unless forced)
    r2_key = f"{bill_type}/{congress:02d}/{task.bill_name}.pdf"

    try:
        if not force and uploader.file_exists_in_r2(r2_key):
            print(f"  File already exists in R2: {r2_key}")
            tracker.mark_finished(task.bill_number)
            return True
    except Exception as e:
        print(f"  Error checking R2 existence for {r2_key}: {e}")
        # Continue with download/upload anyway

    # Download PDF
    print(f"  Downloading from {task.download_url}...")
    pdf_content = uploader.download_pdf_with_retry(task.download_url)

    if not pdf_content:
        print(f"  Failed to download PDF for {task.bill_name}")
        tracker.mark_erroring(task.bill_number)
        return False

    # Upload to R2
    print(f"  Uploading to R2: {r2_key}...")

    if uploader.upload_to_r2(pdf_content, r2_key):
        print(f"  Successfully uploaded {task.bill_name}")
        tracker.mark_finished(task.bill_number)
        return True
    else:
        print(f"  Failed to upload {task.bill_name}")
        tracker.mark_erroring(task.bill_number)
        return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Upload congressional bill PDFs to Cloudflare R2'
    )
    parser.add_argument(
        '--type',
        required=True,
        choices=['sb', 'hb'],
        help='Bill type: sb (Senate Bill) or hb (House Bill)'
    )
    parser.add_argument(
        '--congress',
        required=True,
        type=int,
        help='Congress number'
    )

    # Mutually exclusive group for single or multiple documents
    doc_group = parser.add_mutually_exclusive_group()
    doc_group.add_argument(
        '--document',
        type=int,
        help='Single document number to upload (will overwrite if exists)'
    )
    doc_group.add_argument(
        '--documents',
        type=str,
        help='Range of document numbers to upload (format: lowest,highest)'
    )

    parser.add_argument(
        '--workers',
        type=int,
        default=10,
        help='Number of concurrent workers (default: 10)'
    )

    args = parser.parse_args()

    # Load environment variables
    load_dotenv()

    # Get R2 credentials
    endpoint_url = os.getenv('R2_ENDPOINT_URL')
    access_key = os.getenv('R2_ACCESS_KEY_ID')
    secret_key = os.getenv('R2_SECRET_ACCESS_KEY')
    bucket_name = os.getenv('R2_BUCKET_NAME')

    if not all([endpoint_url, access_key, secret_key, bucket_name]):
        print("Error: Missing R2 credentials in .env file")
        print("Required: R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME")
        sys.exit(1)

    # Initialize uploader and tracker
    uploader = PDFUploader(endpoint_url, access_key, secret_key, bucket_name)
    base_path = Path(f"data/document/{args.type}/{args.congress}")

    if not base_path.exists():
        print(f"Error: Path {base_path} does not exist")
        sys.exit(1)

    tracker = UploadTracker(base_path)

    # Determine which bills to process
    mapping_file_name = f".{'senate' if args.type == 'sb' else 'house'}-bill-number-mapping.yml"
    mapping_file = base_path / mapping_file_name

    if not mapping_file.exists():
        print(f"Error: Mapping file {mapping_file} does not exist")
        sys.exit(1)

    # Parse mappings
    if args.document:
        # Single document mode
        print(f"Processing single document: {args.document}")
        mappings = [(args.document, None)]  # Will look up document_id from mapping
        force = True
    elif args.documents:
        # Range mode: lowest,highest
        doc_range = [int(d.strip()) for d in args.documents.split(',')]
        if len(doc_range) != 2:
            print("Error: --documents requires exactly 2 numbers (lowest,highest)")
            sys.exit(1)

        lowest, highest = min(doc_range), max(doc_range)
        doc_numbers = list(range(lowest, highest + 1))
        print(f"Processing document range {lowest} to {highest} ({len(doc_numbers)} documents)")
        mappings = [(num, None) for num in doc_numbers]
        force = False
    else:
        # Process all bills
        print(f"Processing all bills from {mapping_file}")
        mappings = parse_yaml_line_by_line(mapping_file)
        force = False

    # If document_id is None, look it up from mapping
    full_mapping_dict = {}
    with open(mapping_file, 'r') as f:
        for line in f:
            match = re.match(r"'(\d+)':\s+(\w+)", line.strip())
            if match:
                full_mapping_dict[int(match.group(1))] = match.group(2)

    # Create tasks
    tasks = []
    for bill_number, document_id in mappings:
        if document_id is None:
            document_id = full_mapping_dict.get(bill_number)
            if not document_id:
                print(f"Warning: No document ID found for bill number {bill_number}")
                continue

        task = create_upload_task(args.type, args.congress, bill_number, document_id)
        tasks.append(task)

    print(f"\nTotal tasks: {len(tasks)}")
    print(f"Using {args.workers} workers\n")

    # Process tasks with thread pool
    success_count = 0
    failed_count = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                process_bill, task, args.type, args.congress, uploader, tracker, force
            ): task for task in tasks
        }

        for future in as_completed(futures):
            task = futures[future]
            try:
                result = future.result()
                if result:
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                print(f"Exception processing {task.bill_name}: {e}")
                failed_count += 1

    print(f"\n{'='*60}")
    print(f"Upload Summary")
    print(f"{'='*60}")
    print(f"Total tasks:     {len(tasks)}")
    print(f"Successful:      {success_count}")
    print(f"Failed:          {failed_count}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
