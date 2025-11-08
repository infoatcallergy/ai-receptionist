import os
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

# Load environment variables
load_dotenv()
CALENDLY_API_KEY = os.getenv("CALENDLY_API_KEY")

headers = {"Authorization": f"Bearer {CALENDLY_API_KEY}"}

# 🔹 Replace with your real Event Type URI from Day 6
EVENT_TYPE_URI = "https://api.calendly.com/event_types/a8bf7e72-ea28-4b09-9d92-53c6e98714ce"

# ✅ Always use timezone-aware UTC datetimes and start slightly in the future
now = datetime.now(timezone.utc)
start_time = (now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
end_time = (now + timedelta(days=7)).isoformat().replace("+00:00", "Z")

print("🔍 Fetching available slots from Calendly...")

# Build the Calendly API URL
url = (
    f"https://api.calendly.com/event_type_available_times?"
    f"event_type={EVENT_TYPE_URI}&start_time={start_time}&end_time={end_time}"
)

# Make the request
response = requests.get(url, headers=headers)

if response.status_code == 200:
    data = response.json()
    slots = data.get("collection", [])
    print("✅ Connection successful!\n")
    if slots:
        print("Available time slots (next 7 days):")
        for s in slots[:5]:  # Show the first 5 slots
            start = s["start_time"]
            print("•", start)
    else:
        print("⚠️ No available time slots found — check your Calendly availability hours.")
else:
    print("❌ Failed to fetch available times.")
    print("Status code:", response.status_code)
    print(response.text)

print("\n🌐 You can share your public booking link:", "https://calendly.com/callergy-info")
