import os
import requests
from dotenv import load_dotenv

# Load your environment variables
load_dotenv()
CALENDLY_API_KEY = os.getenv("CALENDLY_API_KEY")

headers = {"Authorization": f"Bearer {CALENDLY_API_KEY}"}

# Replace this with your actual User URI from your test
USER_URI = "https://api.calendly.com/users/9bd40462-f7d1-40dd-80be-f7e922c6d8ba"

print("🔍 Getting your event types from Calendly...")

# ✅ Correct endpoint (use ?user= instead of /event_types)
event_types_url = f"https://api.calendly.com/event_types?user={USER_URI}"
response = requests.get(event_types_url, headers=headers)

if response.status_code == 200:
    data = response.json()
    print("✅ Calendly connection successful!\n")
    print("Event types found:")
    for event in data.get("collection", []):
        print(f"• {event['name']} → {event['uri']}")
else:
    print("⚠️ Could not fetch event types.")
    print("Status Code:", response.status_code)
    print(response.text)

print("\n🌐 You can share your public booking link:", "https://calendly.com/callergy-info")
