import requests
import json
import time
from tqdm import tqdm

# === CONFIGURATION ===
API_KEY = "ejRxL4P2ftssWogeMD31tagAYvJMQVfM"  # Replace with your API key
BASE_URL = "https://api.dayschedule.com/v1"
OUTPUT_FILE = "Sync Dayscedule API/day_schedule_bookings.json"
DELAY_BETWEEN_REQUESTS = 0.3  # 300ms to stay under Enterprise rate limit

# === FETCH ALL BOOKINGS ===
def fetch_all_bookings():
    print("🔄 Fetching all booking summaries...")
    url = f"{BASE_URL}/bookings?apiKey={API_KEY}"
    response = requests.get(url)
    response.raise_for_status()
    return response.json().get("result", [])

# === FETCH FULL BOOKING DETAILS ===
def fetch_booking_detail(booking_id):
    url = f"{BASE_URL}/bookings/{booking_id}?apiKey={API_KEY}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    print(f"⚠️ Failed to fetch booking {booking_id} — status {response.status_code}")
    return None

# === CLEAN AND FORMAT DATA ===
def clean_booking_data(raw):
    invitee = raw.get("invitees", [{}])[0]
    questions = {q["label"]: q["value"] for q in invitee.get("questions", [])}

    return {
        "booking_id": raw.get("booking_id"),
        "status": raw.get("status"),
        "start_at": raw.get("start_at"),
        "end_at": raw.get("end_at"),
        "booking_url": raw.get("booking_url"),
        "host": {
            "name": raw.get("host", {}).get("name"),
            "email": raw.get("host", {}).get("email")
        },
        "patient": {
            "full_name": f"{questions.get('Name', '')} {questions.get('Surname', '')}".strip(),
            "email": questions.get("Your email address", invitee.get("email")),
            "phone": questions.get("Mobile number", invitee.get("phone")),
            "date_of_birth": questions.get("Date of Birth"),
            "gender": questions.get("Gender"),
            "height": questions.get("Height"),
            "weight": questions.get("Weight")
        }
    }

# === PROCESS ALL BOOKINGS ===
def process_bookings():
    raw_bookings = fetch_all_bookings()
    grouped = {
        "confirmed": [],
        "pending": [],
        "canceled": [],
        "other": []
    }

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

    for booking in raw_bookings:
        booking_id = booking.get("booking_id")
        detail = fetch_booking_detail(booking_id)

        if not detail:
            progress_bar.update(1)
            continue

        cleaned = clean_booking_data(detail)
        status = detail.get("status", "").lower()

        # Group by status
        if status in grouped:
            grouped[status].append(cleaned)
        else:
            grouped["other"].append(cleaned)

        # Update progress bar
        progress_bar.update(1)
        time.sleep(DELAY_BETWEEN_REQUESTS)
    
    # Close progress bar
    progress_bar.close()

    print("\n📊 Summary:")
    for state, entries in grouped.items():
        print(f"  - {state.capitalize()}: {len(entries)}")

    return grouped

# === MAIN EXECUTION ===
if __name__ == "__main__":
    try:
        print("🚀 Starting DaySchedule sync...\n")
        grouped_bookings = process_bookings()

        with open(OUTPUT_FILE, "w") as f:
            json.dump(grouped_bookings, f, indent=2)

        print(f"\n✅ Done! Synced and saved to {OUTPUT_FILE}")

    except requests.exceptions.HTTPError as err:
        print(f"❌ HTTP Error: {err}")
    except Exception as e:
        print(f"❌ Unexpected Error: {e}")
