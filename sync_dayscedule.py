import requests
import json
import time
import asyncio
import aiohttp
import argparse
import os
import datetime
import schedule
import sys
import re
from tqdm import tqdm

# === CONFIGURATION ===
API_KEY = "ejRxL4P2ftssWogeMD31tagAYvJMQVfM"  # Replace with your API key
BASE_URL = "https://api.dayschedule.com/v1"
OUTPUT_FILE = "day_schedule_bookings.json"  # Save in the current directory
DELAY_BETWEEN_REQUESTS = 0.3  # 300ms to stay under Enterprise rate limit
MAX_CONCURRENT_REQUESTS = 5  # Limit concurrent requests to avoid rate limiting

class DayScheduleClient:
    """Client for interacting with the DaySchedule API"""
    
    def __init__(self, api_key, base_url):
        self.api_key = api_key
        self.base_url = base_url
        self.session = None
    
    async def __aenter__(self):
        """Set up the aiohttp session for async requests"""
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Close the aiohttp session"""
        if self.session:
            await self.session.close()
    
    async def fetch_all_bookings(self):
        """Fetch all booking summaries"""
        print("🔄 Fetching all booking summaries...")
        url = f"{self.base_url}/bookings?apiKey={self.api_key}"
        
        async with self.session.get(url) as response:
            if response.status != 200:
                raise Exception(f"Failed to fetch bookings: {response.status}")
            
            data = await response.json()
            return data.get("result", [])
    
    async def fetch_booking_detail(self, booking_id):
        """Fetch detailed information for a specific booking"""
        url = f"{self.base_url}/bookings/{booking_id}?apiKey={self.api_key}"
        
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    print(f"⚠️ Failed to fetch booking {booking_id} — status {response.status}")
                    return None
        except Exception as e:
            print(f"⚠️ Error fetching booking {booking_id}: {str(e)}")
            return None

class BookingProcessor:
    """Process and transform booking data"""
    
    @staticmethod
    def extract_store_name(booking_url):
        """Extract store name from booking URL
        
        URL patterns:
        - https://section21.dayschedule.com/{store name}-dr-m-tupy-consultation
        - https://section21bookings.dayschedule.com/{store name}-dr-consultation-1
        """
        if not booking_url:
            return None
        
        # Extract the store name using regex - handle multiple URL patterns
        patterns = [
            # Pattern 1: https://section21.dayschedule.com/{store name}-dr-m-tupy-consultation
            r"https://section21\.dayschedule\.com/([^/]+?)(?:-dr-m-tupy-consultation|$)",
            # Pattern 2: https://section21bookings.dayschedule.com/{store name}-dr-consultation-1
            r"https://section21bookings\.dayschedule\.com/([^/]+?)(?:-dr-consultation-1|$)"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, booking_url)
            if match:
                store_name = match.group(1)
                # Replace hyphens with spaces and capitalize words for better readability
                store_name = store_name.replace('-', ' ').title()
                return store_name
        
        return None
    
    @staticmethod
    def clean_booking_data(raw):
        """Clean and format booking data, extracting patient information"""
        invitee = raw.get("invitees", [{}])[0]
        questions = {q["label"]: q["value"] for q in invitee.get("questions", [])}
        
        # Extract store name from booking URL
        booking_url = raw.get("booking_url")
        store_name = BookingProcessor.extract_store_name(booking_url)
        
        # Create the booking data with the requested field order
        booking_data = {
            "booking_id": raw.get("booking_id"),
        }
        
        # Add store name if available, otherwise use booking URL
        if store_name:
            booking_data["store_name"] = store_name
        else:
            booking_data["booking_url"] = booking_url
        
        # Add remaining fields
        booking_data["status"] = raw.get("status")
        booking_data["patient"] = {
            "full_name": f"{questions.get('Name', '')} {questions.get('Surname', '')}".strip(),
            "email": questions.get("Your email address", invitee.get("email")),
            "phone": questions.get("Mobile number", invitee.get("phone")),
            "date_of_birth": questions.get("Date of Birth"),
            "gender": questions.get("Gender"),
            "height": questions.get("Height"),
            "weight": questions.get("Weight")
        }
            
        return booking_data

def load_existing_data():
    """Load existing booking data from the JSON file"""
    if not os.path.exists(OUTPUT_FILE):
        print(f"ℹ️ No existing data file found at {OUTPUT_FILE}")
        return {"last_updated": None, "confirmed": [], "pending": [], "canceled": [], "other": []}
    
    try:
        with open(OUTPUT_FILE, 'r') as f:
            data = json.load(f)
            print(f"✅ Loaded existing data from {OUTPUT_FILE}")
            
            # Add last_updated field if it doesn't exist
            if "last_updated" not in data:
                data["last_updated"] = None
                
            return data
    except json.JSONDecodeError:
        print(f"⚠️ Error parsing JSON from {OUTPUT_FILE}. Starting with empty data.")
        return {"last_updated": None, "confirmed": [], "pending": [], "canceled": [], "other": []}
    except Exception as e:
        print(f"⚠️ Error loading data from {OUTPUT_FILE}: {str(e)}. Starting with empty data.")
        return {"last_updated": None, "confirmed": [], "pending": [], "canceled": [], "other": []}

def create_booking_map(data):
    """Create a map of booking_id to booking data for quick lookup"""
    booking_map = {}
    
    for status in ["confirmed", "pending", "canceled", "other"]:
        for booking in data.get(status, []):
            if "booking_id" in booking:
                booking_map[booking["booking_id"]] = {
                    "status": status,
                    "data": booking
                }
    
    return booking_map

async def process_booking_batch(client, bookings, progress_bar, existing_booking_map=None, force_process=False):
    """Process a batch of bookings concurrently"""
    tasks = []
    results = []
    changes = {"new": 0, "updated": 0, "unchanged": 0}
    
    # Create a semaphore to limit concurrent requests
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    
    async def fetch_with_semaphore(booking):
        """Fetch booking details with semaphore to limit concurrency"""
        booking_id = booking.get("booking_id")
        booking_status = booking.get("status", "").lower()
        
        # Check if booking exists and has the same status
        if not force_process and existing_booking_map and booking_id in existing_booking_map:
            existing = existing_booking_map[booking_id]
            if existing["status"] == booking_status:
                # Status hasn't changed, but we need to process it to extract store name
                # Check if it already has store_name
                if "store_name" in existing["data"]:
                    # Already has store_name, use existing data
                    progress_bar.update(1)
                    changes["unchanged"] += 1
                    return {"type": "existing", "data": existing["data"]}
                else:
                    # Doesn't have store_name, need to process it
                    pass
        
        # Booking is new or status has changed, fetch details
        async with semaphore:
            detail = await client.fetch_booking_detail(booking_id)
            progress_bar.update(1)
            
            if not detail:
                return None
            
            # Add a small delay to avoid overwhelming the API
            await asyncio.sleep(DELAY_BETWEEN_REQUESTS)
            
            if existing_booking_map and booking_id in existing_booking_map:
                changes["updated"] += 1
                return {"type": "updated", "data": detail}
            else:
                changes["new"] += 1
                return {"type": "new", "data": detail}
    
    # Create tasks for all bookings in the batch
    for booking in bookings:
        task = asyncio.create_task(fetch_with_semaphore(booking))
        tasks.append(task)
    
    # Wait for all tasks to complete
    details = await asyncio.gather(*tasks)
    
    # Process the results
    for detail in details:
        if detail:
            if detail["type"] == "existing":
                # Use existing data
                results.append(detail["data"])
            else:
                # Process new or updated data
                cleaned = BookingProcessor.clean_booking_data(detail["data"])
                results.append(cleaned)
    
    return results, changes

async def process_bookings(sync_mode="incremental", test_mode=False, limit=10, force_process=False):
    """Process all bookings, with options for sync mode and test mode"""
    # Load existing data
    existing_data = load_existing_data()
    existing_booking_map = None
    
    if sync_mode == "incremental":
        # Create a map of existing bookings for quick lookup
        existing_booking_map = create_booking_map(existing_data)
        print(f"📋 Found {len(existing_booking_map)} existing bookings")
    
    # Initialize the grouped data structure
    grouped = {
        "last_updated": datetime.datetime.now().isoformat(),
        "confirmed": [],
        "pending": [],
        "canceled": [],
        "other": []
    }
    
    # Track changes for reporting
    total_changes = {"new": 0, "updated": 0, "unchanged": 0}
    
    async with DayScheduleClient(API_KEY, BASE_URL) as client:
        # Fetch all booking summaries
        raw_bookings = await client.fetch_all_bookings()
        
        # If in test mode, limit the number of bookings to process
        if test_mode:
            print(f"🧪 TEST MODE: Processing only {limit} bookings")
            raw_bookings = raw_bookings[:limit]
        
        print(f"📦 {len(raw_bookings)} bookings found.\n")
        
        # Create a visually appealing progress bar
        progress_bar = tqdm(
            total=len(raw_bookings),
            desc="Processing bookings",
            unit="booking",
            bar_format="{desc}: |{bar}| {percentage:3.0f}% [{n_fmt}/{total_fmt}] {elapsed}<{remaining}",
            ncols=80,
            ascii="░▒▓█"  # Use these characters for the progress bar
        )
        
        # Process bookings in batches for better performance
        batch_size = min(50, len(raw_bookings))  # Process up to 50 bookings at a time
        for i in range(0, len(raw_bookings), batch_size):
            batch = raw_bookings[i:i+batch_size]
            results, changes = await process_booking_batch(
                client, 
                batch, 
                progress_bar, 
                existing_booking_map if sync_mode == "incremental" else None,
                force_process
            )
            
            # Update total changes
            for key in changes:
                total_changes[key] += changes[key]
            
            # Group results by status
            for result in results:
                status = result.get("status", "").lower()
                if status in grouped:
                    grouped[status].append(result)
                else:
                    grouped["other"].append(result)
        
        # Close progress bar
        progress_bar.close()
        
        print("\n📊 Summary:")
        for state, entries in grouped.items():
            if state != "last_updated":
                print(f"  - {state.capitalize()}: {len(entries)}")
        
        print("\n🔄 Changes:")
        print(f"  - New bookings: {total_changes['new']}")
        print(f"  - Updated bookings: {total_changes['updated']}")
        print(f"  - Unchanged bookings: {total_changes['unchanged']}")
        
        return grouped

def ask_run_mode():
    """Ask the user for the run mode (sync mode and test mode)"""
    # Ask for sync mode
    while True:
        print("\n🔄 Sync Mode Options:")
        print("  1. Incremental (only fetch details for new or changed bookings)")
        print("  2. Full (fetch details for all bookings)")
        
        response = input("Choose sync mode (1/2): ").strip()
        if response in ['1', 'incremental']:
            sync_mode = "incremental"
            break
        elif response in ['2', 'full']:
            sync_mode = "full"
            break
        else:
            print("Please enter '1' or '2'.")
    
    # Ask for test mode
    while True:
        response = input("\nRun in test mode with limited bookings? (y/n): ").lower()
        if response in ['y', 'yes']:
            try:
                limit = int(input("How many bookings to process? (default: 10): ") or 10)
                return sync_mode, True, limit
            except ValueError:
                print("Please enter a valid number. Using default of 10.")
                return sync_mode, True, 10
        elif response in ['n', 'no']:
            return sync_mode, False, None
        else:
            print("Please enter 'y' or 'n'.")

def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description='Sync bookings from DaySchedule API')
    parser.add_argument('--sync-mode', choices=['incremental', 'full'], default='incremental',
                        help='Sync mode: incremental (default) or full')
    parser.add_argument('--test', action='store_true', help='Run in test mode with limited bookings')
    parser.add_argument('--limit', type=int, default=10, help='Number of bookings to process in test mode')
    parser.add_argument('--schedule', action='store_true', help='Run in scheduled mode, syncing at regular intervals')
    parser.add_argument('--interval', type=int, default=60, help='Interval in minutes for scheduled sync (default: 60)')
    parser.add_argument('--force-process', action='store_true', help='Force processing of all bookings, even unchanged ones')
    
    return parser.parse_args()

def run_sync(sync_mode, test_mode, limit, force_process=False):
    """Run the sync process"""
    try:
        print(f"\n🚀 Starting DaySchedule sync at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}...\n")
        
        print(f"ℹ️ Running in {sync_mode.upper()} sync mode")
        
        # Run the async process_bookings function
        grouped_bookings = asyncio.run(process_bookings(sync_mode, test_mode, limit, force_process))
        
        # Save the results to a JSON file
        with open(OUTPUT_FILE, "w") as f:
            json.dump(grouped_bookings, f, indent=2)
        
        print(f"\n✅ Done! Synced and saved to {OUTPUT_FILE}")
        print(f"📅 Last updated: {grouped_bookings['last_updated']}")
        
        return True
    except requests.exceptions.HTTPError as err:
        print(f"❌ HTTP Error: {err}")
    except aiohttp.ClientError as err:
        print(f"❌ HTTP Client Error: {err}")
    except Exception as e:
        print(f"❌ Unexpected Error: {e}")
    
    return False

def scheduled_sync(sync_mode, test_mode, limit, interval, force_process=False):
    """Run the sync process on a schedule"""
    print(f"🕒 Scheduled sync every {interval} minutes")
    print("Press Ctrl+C to stop")
    
    # Run once immediately
    run_sync(sync_mode, test_mode, limit, force_process)
    
    # Schedule to run at the specified interval
    schedule.every(interval).minutes.do(run_sync, sync_mode=sync_mode, test_mode=test_mode, limit=limit, force_process=force_process)
    
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n⏹️ Scheduled sync stopped by user")
        sys.exit(0)

# === MAIN EXECUTION ===
if __name__ == "__main__":
    # Parse command line arguments
    args = parse_arguments()
    
    # If no command line arguments specified, ask interactively
    if not args.test and not args.limit and args.sync_mode == 'incremental' and not args.schedule:
        sync_mode, test_mode, limit = ask_run_mode()
        scheduled_mode = False
        interval = args.interval
        force_process = args.force_process
    else:
        sync_mode, test_mode, limit = args.sync_mode, args.test, args.limit
        scheduled_mode = args.schedule
        interval = args.interval
        force_process = args.force_process
    
    if scheduled_mode:
        # Run in scheduled mode
        scheduled_sync(sync_mode, test_mode, limit, interval, force_process)
    else:
        # Run once
        run_sync(sync_mode, test_mode, limit, force_process)
