#!/usr/bin/env python3
"""
Scrape Senate bills from the Philippine Senate website.

This script extracts Senate bill numbers authored by specific legislators or referred to
specific committees, generating mapping files in the metadata directory structure.
"""

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
import os
import platform


class SenateBillScraper:
    """Scraper for Philippine Senate bills website."""

    BASE_URL = "https://web.senate.gov.ph/lis/leg_sys.aspx"
    # Use parent directory for metadata (go up from scripts folder)
    METADATA_DIR = Path(__file__).parent.parent / "metadata"
    METADATA_FILE = METADATA_DIR / "senate-website.json"

    def __init__(self, headless: bool = True, verbose: bool = False):
        """Initialize the scraper.

        Args:
            headless: Run Chrome in headless mode
            verbose: Print detailed progress messages
        """
        self.verbose = verbose
        self.driver = None
        self.metadata = self._load_metadata()
        self._setup_driver(headless)

    def _load_metadata(self) -> Dict:
        """Load metadata from senate-website.json."""
        if not self.METADATA_FILE.exists():
            self._log(f"Warning: {self.METADATA_FILE} not found")
            return {"congresses": {}}

        with open(self.METADATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _setup_driver(self, headless: bool):
        """Setup Chrome WebDriver with appropriate options."""
        chrome_options = Options()

        if headless:
            chrome_options.add_argument("--headless=new")  # Use new headless mode

        # Additional options for stability
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        # Fix for webdriver-manager on Mac ARM64
        driver_path = ChromeDriverManager().install()
        self._log(f"Initial driver path: {driver_path}")

        # On macOS, the actual driver might be in a subdirectory
        if platform.system() == "Darwin":
            # Check if this is a directory containing the actual driver
            if os.path.isdir(driver_path):
                # Look for the actual chromedriver executable
                for item in os.listdir(driver_path):
                    item_path = os.path.join(driver_path, item)
                    if os.path.isfile(item_path) and 'chromedriver' in item.lower() and not item.endswith('.txt'):
                        driver_path = item_path
                        break
            # If the path ends with a non-executable file, try to find the actual driver
            elif driver_path.endswith('THIRD_PARTY_NOTICES.chromedriver') or not os.access(driver_path, os.X_OK):
                # Get the directory and look for the actual chromedriver
                driver_dir = os.path.dirname(driver_path)
                for item in os.listdir(driver_dir):
                    item_path = os.path.join(driver_dir, item)
                    if 'chromedriver' in item and os.path.isfile(item_path) and not 'THIRD_PARTY' in item and not 'LICENSE' in item:
                        # Make sure it's executable
                        if not os.access(item_path, os.X_OK):
                            os.chmod(item_path, 0o755)
                        driver_path = item_path
                        break

        self._log(f"Final driver path: {driver_path}")

        # Ensure the driver is executable
        if not os.access(driver_path, os.X_OK):
            os.chmod(driver_path, 0o755)
            self._log(f"Made driver executable: {driver_path}")

        # Setup service with the correct driver path
        service = ChromeService(executable_path=driver_path)

        # Create driver
        self.driver = webdriver.Chrome(service=service, options=chrome_options)
        self.driver.implicitly_wait(10)

    def _log(self, message: str):
        """Print message if verbose mode is enabled."""
        if self.verbose:
            print(message)

    def _wait_for_element(self, by: By, value: str, timeout: int = 10) -> Optional:
        """Wait for an element to be present and return it."""
        try:
            element = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((by, value))
            )
            return element
        except TimeoutException:
            self._log(f"Timeout waiting for element: {value}")
            return None

    def _execute_postback(self, event_target: str, event_argument: str = ""):
        """Execute ASP.NET postback."""
        script = f"__doPostBack('{event_target}', '{event_argument}');"
        self.driver.execute_script(script)
        time.sleep(2)  # Wait for postback to complete


    def _select_author(self, author_code: str):
        """Select author from dropdown if specified."""
        try:
            # Wait for dropdown to be present
            dropdown = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.ID, "dlSenators"))
            )

            # Create Select object
            select = Select(dropdown)

            # Check if the author code exists in options
            available_values = [option.get_attribute('value') for option in select.options]
            if author_code not in available_values:
                self._log(f"Author code {author_code} not found in dropdown")
                self._log(f"Available codes: {', '.join([v for v in available_values if v][:5])}")
                return False

            # Get current selected value to check if we need to change
            current_value = select.first_selected_option.get_attribute('value')
            if current_value == author_code:
                self._log(f"Author {author_code} already selected")
                return True

            self._log(f"Selecting author: {author_code} (current: {current_value})")

            # Use JavaScript to change the value and trigger the postback
            # This mimics what happens when a user selects from the dropdown
            script = f"""
                var dropdown = document.getElementById('dlSenators');
                dropdown.value = '{author_code}';
                __doPostBack('dlSenators', '');
            """
            self.driver.execute_script(script)

            # Wait for page to reload after postback
            time.sleep(4)

            # Verify the selection was applied
            dropdown_after = self.driver.find_element(By.ID, "dlSenators")
            select_after = Select(dropdown_after)
            selected_value = select_after.first_selected_option.get_attribute('value')

            if selected_value == author_code:
                self._log(f"Successfully selected author: {author_code}")
                return True
            else:
                self._log(f"Failed to select author. Selected: {selected_value}, Expected: {author_code}")
                return False

        except TimeoutException:
            self._log(f"Timeout waiting for authors dropdown")
            return False
        except Exception as e:
            self._log(f"Error selecting author: {e}")
            return False

    def _select_committee(self, committee_code: str):
        """Select committee from dropdown if specified."""
        try:
            # Wait for dropdown to be present
            dropdown = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.ID, "dlCommittees"))
            )

            # Create Select object
            select = Select(dropdown)

            # Check if the committee code exists in options
            available_values = [option.get_attribute('value') for option in select.options]
            if committee_code not in available_values:
                self._log(f"Committee code {committee_code} not found in dropdown")
                self._log(f"Available codes: {', '.join([v for v in available_values if v][:5])}")
                return False

            # Get current selected value to check if we need to change
            current_value = select.first_selected_option.get_attribute('value')
            if current_value == committee_code:
                self._log(f"Committee {committee_code} already selected")
                return True

            self._log(f"Selecting committee: {committee_code} (current: {current_value})")

            # Use JavaScript to change the value and trigger the postback
            # This mimics what happens when a user selects from the dropdown
            script = f"""
                var dropdown = document.getElementById('dlCommittees');
                dropdown.value = '{committee_code}';
                __doPostBack('dlCommittees', '');
            """
            self.driver.execute_script(script)

            # Wait for page to reload after postback
            time.sleep(4)

            # Verify the selection was applied
            dropdown_after = self.driver.find_element(By.ID, "dlCommittees")
            select_after = Select(dropdown_after)
            selected_value = select_after.first_selected_option.get_attribute('value')

            if selected_value == committee_code:
                self._log(f"Successfully selected committee: {committee_code}")
                return True
            else:
                self._log(f"Failed to select committee. Selected: {selected_value}, Expected: {committee_code}")
                return False

        except TimeoutException:
            self._log(f"Timeout waiting for committees dropdown")
            return False
        except Exception as e:
            self._log(f"Error selecting committee: {e}")
            return False

    def _sort_by_bill_number(self):
        """Click on 'Bill No.' to sort by bill number."""
        # Try to find and click the sort link
        sort_link = self._wait_for_element(By.ID, "lbType")
        if sort_link:
            self._log("Sorting by bill number...")
            sort_link.click()
            time.sleep(3)  # Wait for sort to complete
        else:
            # Try using postback directly
            self._log("Using postback to sort by bill number...")
            self._execute_postback('lbType', '')

    def _extract_bills_from_page(self) -> Set[str]:
        """Extract bill numbers from the current page."""
        bills = set()

        # Parse the page
        soup = BeautifulSoup(self.driver.page_source, 'html.parser')

        # Check for "None found" message
        page_text = soup.get_text()
        if "None found" in page_text:
            self._log("No bills found on this page")
            return bills

        # Wait for content to load (shorter timeout)
        content_div = self._wait_for_element(By.CLASS_NAME, "alight", timeout=5)
        if not content_div:
            # If no content div, check again for "None found"
            if "None found" in self.driver.page_source:
                self._log("No bills found for this filter")
                return bills

        # Find all bill links
        bill_links = soup.find_all('a', href=re.compile(r'bill_res\.aspx\?'))

        for link in bill_links:
            # Extract bill number from the link text
            span = link.find('span')
            if span:
                text = span.get_text()
                # Extract SBN-XXXX or HBN-XXXX pattern
                match = re.search(r'(SBN|HBN)-(\d+)', text)
                if match:
                    bill_number = match.group(0)
                    bills.add(bill_number)
                    self._log(f"Found bill: {bill_number}")

        return bills

    def _has_next_page(self) -> bool:
        """Check if there's a next page available."""
        soup = BeautifulSoup(self.driver.page_source, 'html.parser')

        # Look for "Next" link in pagination
        next_links = soup.find_all('a', string=re.compile(r'Next\s*'))
        return len(next_links) > 0

    def _go_to_next_page(self) -> bool:
        """Navigate to the next page if available."""
        try:
            # Find and click the Next link
            next_link = self.driver.find_element(By.LINK_TEXT, "Next")
            if next_link:
                self._log("Going to next page...")
                next_link.click()
                time.sleep(3)  # Wait for page to load
                return True
        except NoSuchElementException:
            pass

        # Try with partial link text
        try:
            next_link = self.driver.find_element(By.PARTIAL_LINK_TEXT, "Next")
            if next_link:
                self._log("Going to next page...")
                next_link.click()
                time.sleep(3)
                return True
        except NoSuchElementException:
            pass

        return False

    def scrape_bills(self, congress: str,
                     legislator: Optional[str] = None,
                     committee: Optional[str] = None) -> Set[str]:
        """Scrape Senate bills for given parameters.

        Args:
            congress: Congress number
            legislator: Optional legislator code
            committee: Optional committee code

        Returns:
            Set of bill numbers (SBN-XXXXX)
        """
        all_bills = set()

        # Build URL with congress parameter only
        # Don't add filters to URL as they need to be applied via dropdowns
        url = f"{self.BASE_URL}?congress={congress}&type=bill"

        self._log(f"Navigating to: {url}")
        self.driver.get(url)

        # Wait for page to fully load - wait for the bill listing div
        try:
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CLASS_NAME, "alight"))
            )
        except:
            self._log("Warning: Could not verify page loaded properly")

        time.sleep(2)  # Additional wait for JavaScript to initialize

        # Apply filters via dropdowns (this triggers postback)
        if legislator:
            if not self._select_author(legislator):
                self._log(f"Failed to select author: {legislator}")
                return all_bills

        if committee:
            if not self._select_committee(committee):
                self._log(f"Failed to select committee: {committee}")
                return all_bills

        # Sort by bill number
        self._sort_by_bill_number()

        # Extract bills from all pages
        page_num = 1
        while True:
            self._log(f"Processing page {page_num}...")

            # Extract bills from current page
            page_bills = self._extract_bills_from_page()
            all_bills.update(page_bills)

            # Check if there's a next page and navigate to it
            if self._has_next_page():
                if self._go_to_next_page():
                    page_num += 1
                else:
                    break
            else:
                self._log("No more pages")
                break

        return all_bills

    def save_mapping_file(self, congress: str, mapping_type: str,
                          code: str, bills: Set[str]):
        """Save Senate bills to a mapping file.

        Args:
            congress: Congress number
            mapping_type: 'author' or 'committee'
            code: Author or committee code
            bills: Set of bill numbers
        """
        # Create directory structure: metadata/sb/<congress>/<mapping_type>/
        congress_num = int(congress) if congress.isdigit() else congress
        mapping_dir = self.METADATA_DIR / "sb" / f"{congress_num:02d}" / mapping_type
        mapping_dir.mkdir(parents=True, exist_ok=True)

        # Create filename with just the code
        filename = f"{code}.txt"
        filepath = mapping_dir / filename

        # Sort bills naturally (extract numbers for proper sorting)
        def extract_number(bill):
            match = re.search(r'(\d+)', bill)
            return int(match.group(1)) if match else 0

        sorted_bills = sorted(bills, key=extract_number)

        # Write to file
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"# bill_type: SB\n")
            f.write(f"# congress: {congress_num:02d}\n")
            f.write(f"# {mapping_type}: {code}\n")
            f.write(f"# generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# count: {len(bills)}\n")
            f.write("\n")
            for bill in sorted_bills:
                f.write(f"{bill}\n")

        self._log(f"Saved {len(bills)} Senate bills to {filepath}")

    def _process_single_legislator(self, congress: str, senator_code: str, senator_data: dict) -> Tuple[str, int]:
        """Process a single legislator in a separate thread.

        Returns:
            Tuple of (senator_code, bill_count)
        """
        # Create a new scraper instance for this thread
        scraper = SenateBillScraper(headless=True, verbose=self.verbose)
        try:
            print(f"\nProcessing author: {senator_data['name']} ({senator_code})")

            bills = scraper.scrape_bills(
                congress=congress,
                legislator=senator_code
            )

            if bills:
                scraper.save_mapping_file(
                    congress=congress,
                    mapping_type="author",
                    code=senator_code,
                    bills=bills
                )
                print(f"  {senator_code}: Found {len(bills)} bills")
                return (senator_code, len(bills))
            else:
                print(f"  {senator_code}: No bills found")
                return (senator_code, 0)
        finally:
            scraper.close()

    def _process_single_committee(self, congress: str, committee_code: str, committee_data: dict) -> Tuple[str, int]:
        """Process a single committee in a separate thread.

        Returns:
            Tuple of (committee_code, bill_count)
        """
        # Create a new scraper instance for this thread
        scraper = SenateBillScraper(headless=True, verbose=self.verbose)
        try:
            print(f"\nProcessing committee: {committee_data['name']} ({committee_code})")

            bills = scraper.scrape_bills(
                congress=congress,
                committee=committee_code
            )

            if bills:
                scraper.save_mapping_file(
                    congress=congress,
                    mapping_type="committee",
                    code=committee_code,
                    bills=bills
                )
                print(f"  {committee_code}: Found {len(bills)} bills")
                return (committee_code, len(bills))
            else:
                print(f"  {committee_code}: No bills found")
                return (committee_code, 0)
        finally:
            scraper.close()

    def scrape_all_legislators(self, congress: str, max_workers: int = 1):
        """Scrape all legislators for a specific congress.

        Args:
            congress: Congress number to process
            max_workers: Number of parallel workers to use
        """
        if congress not in self.metadata.get("congresses", {}):
            print(f"Congress {congress} not found in metadata")
            return

        congress_data = self.metadata["congresses"][congress]
        senators = congress_data.get("senators", {})

        print(f"\n{'='*60}")
        print(f"Processing all legislators for Congress {congress}")
        print(f"Using {max_workers} parallel worker(s)")
        print(f"{'='*60}")

        if max_workers == 1:
            # Single-threaded processing
            for senator_code, senator_data in senators.items():
                print(f"\nProcessing author: {senator_data['name']} ({senator_code})")

                bills = self.scrape_bills(
                    congress=congress,
                    legislator=senator_code
                )

                if bills:
                    self.save_mapping_file(
                        congress=congress,
                        mapping_type="author",
                        code=senator_code,
                        bills=bills
                    )
                    print(f"  Found {len(bills)} bills")
                else:
                    print(f"  No bills found")
        else:
            # Multi-threaded processing
            results = {}
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all tasks
                future_to_senator = {
                    executor.submit(self._process_single_legislator, congress, code, data): code
                    for code, data in senators.items()
                }

                # Process completed tasks
                for future in as_completed(future_to_senator):
                    senator_code = future_to_senator[future]
                    try:
                        code, bill_count = future.result()
                        results[code] = bill_count
                    except Exception as e:
                        print(f"Error processing {senator_code}: {e}")
                        results[senator_code] = 0

            # Print summary
            print(f"\n{'='*60}")
            print("Summary:")
            total_bills = sum(results.values())
            print(f"Processed {len(results)} legislators, found {total_bills} total bills")
            print(f"{'='*60}")

    def scrape_all_committees(self, congress: str, max_workers: int = 1):
        """Scrape all committees for a specific congress.

        Args:
            congress: Congress number to process
            max_workers: Number of parallel workers to use
        """
        if congress not in self.metadata.get("congresses", {}):
            print(f"Congress {congress} not found in metadata")
            return

        congress_data = self.metadata["congresses"][congress]
        committees = congress_data.get("committees", {})

        print(f"\n{'='*60}")
        print(f"Processing all committees for Congress {congress}")
        print(f"Using {max_workers} parallel worker(s)")
        print(f"{'='*60}")

        if max_workers == 1:
            # Single-threaded processing
            for committee_code, committee_data in committees.items():
                print(f"\nProcessing committee: {committee_data['name']} ({committee_code})")

                bills = self.scrape_bills(
                    congress=congress,
                    committee=committee_code
                )

                if bills:
                    self.save_mapping_file(
                        congress=congress,
                        mapping_type="committee",
                        code=committee_code,
                        bills=bills
                    )
                    print(f"  Found {len(bills)} bills")
                else:
                    print(f"  No bills found")
        else:
            # Multi-threaded processing
            results = {}
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all tasks
                future_to_committee = {
                    executor.submit(self._process_single_committee, congress, code, data): code
                    for code, data in committees.items()
                }

                # Process completed tasks
                for future in as_completed(future_to_committee):
                    committee_code = future_to_committee[future]
                    try:
                        code, bill_count = future.result()
                        results[code] = bill_count
                    except Exception as e:
                        print(f"Error processing {committee_code}: {e}")
                        results[committee_code] = 0

            # Print summary
            print(f"\n{'='*60}")
            print("Summary:")
            total_bills = sum(results.values())
            print(f"Processed {len(results)} committees, found {total_bills} total bills")
            print(f"{'='*60}")

    def scrape_all_from_metadata(self, congress: Optional[str] = None):
        """Scrape all Senate bills using metadata from senate-website.json.

        Args:
            congress: Optional specific congress to process (processes all if not specified)
        """
        congresses_to_process = []

        if congress:
            # Process specific congress
            if congress in self.metadata.get("congresses", {}):
                congresses_to_process.append(congress)
            else:
                self._log(f"Congress {congress} not found in metadata")
                return
        else:
            # Process all congresses
            congresses_to_process = list(self.metadata.get("congresses", {}).keys())

        for congress_num in congresses_to_process:
            congress_data = self.metadata["congresses"][congress_num]

            print(f"\n{'='*60}")
            print(f"Processing Congress {congress_num}")
            print(f"{'='*60}")

            # Process all authors
            senators = congress_data.get("senators", {})
            for senator_code, senator_data in senators.items():
                print(f"\nProcessing author: {senator_data['name']} ({senator_code})")

                bills = self.scrape_bills(
                    congress=congress_num,
                    legislator=senator_code
                )

                if bills:
                    self.save_mapping_file(
                        congress=congress_num,
                        mapping_type="author",
                        code=senator_code,
                        bills=bills
                    )
                    print(f"  Found {len(bills)} bills")
                else:
                    print(f"  No bills found")

            # Process all committees
            committees = congress_data.get("committees", {})
            for committee_code, committee_data in committees.items():
                print(f"\nProcessing committee: {committee_data['name']} ({committee_code})")

                bills = self.scrape_bills(
                    congress=congress_num,
                    committee=committee_code
                )

                if bills:
                    self.save_mapping_file(
                        congress=congress_num,
                        mapping_type="committee",
                        code=committee_code,
                        bills=bills
                    )
                    print(f"  Found {len(bills)} bills")
                else:
                    print(f"  No bills found")

    def close(self):
        """Close the browser driver."""
        if self.driver:
            self.driver.quit()


def main():
    """Main function to run the scraper."""
    parser = argparse.ArgumentParser(
        description='Scrape Philippine Senate bills from the Senate website'
    )

    parser.add_argument(
        '--congress',
        type=str,
        help='Congress number (e.g., 20)'
    )

    parser.add_argument(
        '--legislator',
        type=str,
        help='Specific legislator code (e.g., BAQUI) - cannot be used with other filters'
    )

    parser.add_argument(
        '--committee',
        type=str,
        help='Specific committee code (e.g., APOAI) - cannot be used with other filters'
    )

    parser.add_argument(
        '--legislators',
        action='store_true',
        help='Extract all legislators for the given congress - cannot be used with other filters'
    )

    parser.add_argument(
        '--committees',
        action='store_true',
        help='Extract all committees for the given congress - cannot be used with other filters'
    )

    parser.add_argument(
        '--workers',
        type=int,
        default=1,
        help='Number of parallel workers to use (default: 1, max recommended: 10)'
    )

    parser.add_argument(
        '--show-browser',
        action='store_true',
        help='Show browser window (default is headless)'
    )

    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Print detailed progress messages'
    )

    args = parser.parse_args()

    # Initialize scraper
    scraper = SenateBillScraper(
        headless=not args.show_browser,
        verbose=args.verbose
    )

    try:
        # Validate parameter combinations
        filters_count = sum([
            bool(args.legislator),
            bool(args.committee),
            bool(args.legislators),
            bool(args.committees)
        ])

        if filters_count > 1:
            print("Error: Cannot use multiple filter options together. Choose only one of:")
            print("  --legislator <code>  : for a specific legislator")
            print("  --committee <code>   : for a specific committee")
            print("  --legislators        : for all legislators")
            print("  --committees         : for all committees")
            return

        if args.legislator:
            # Specific legislator
            if not args.congress:
                print("Error: --congress is required when using --legislator")
                return

            bills = scraper.scrape_bills(
                congress=args.congress,
                legislator=args.legislator
            )

            if bills:
                scraper.save_mapping_file(
                    congress=args.congress,
                    mapping_type="author",
                    code=args.legislator,
                    bills=bills
                )
                print(f"\nFound {len(bills)} Senate bills for legislator {args.legislator}")
            else:
                print(f"No bills found for legislator {args.legislator}")

        elif args.committee:
            # Specific committee
            if not args.congress:
                print("Error: --congress is required when using --committee")
                return

            bills = scraper.scrape_bills(
                congress=args.congress,
                committee=args.committee
            )

            if bills:
                scraper.save_mapping_file(
                    congress=args.congress,
                    mapping_type="committee",
                    code=args.committee,
                    bills=bills
                )
                print(f"\nFound {len(bills)} Senate bills for committee {args.committee}")
            else:
                print(f"No bills found for committee {args.committee}")

        elif args.legislators:
            # All legislators for the congress
            if not args.congress:
                print("Error: --congress is required when using --legislators")
                return

            # Validate workers count
            max_workers = min(max(1, args.workers), 10)
            if args.workers > 10:
                print(f"Warning: Limiting workers to 10 (requested {args.workers})")

            scraper.scrape_all_legislators(congress=args.congress, max_workers=max_workers)

        elif args.committees:
            # All committees for the congress
            if not args.congress:
                print("Error: --congress is required when using --committees")
                return

            # Validate workers count
            max_workers = min(max(1, args.workers), 10)
            if args.workers > 10:
                print(f"Warning: Limiting workers to 10 (requested {args.workers})")

            scraper.scrape_all_committees(congress=args.congress, max_workers=max_workers)

        else:
            # No specific filter - scrape everything from metadata
            scraper.scrape_all_from_metadata(congress=args.congress)

    except KeyboardInterrupt:
        print("\nScraping interrupted by user")

    except Exception as e:
        print(f"Error during scraping: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()

    finally:
        scraper.close()
        print("\nScraping completed")


if __name__ == '__main__':
    main()