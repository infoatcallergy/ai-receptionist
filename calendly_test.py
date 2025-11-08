import os
import requests
from dotenv import load_dotenv

# Load keys from .env
load_dotenv()
CALENDLY_API_KEY = os.getenv("CALENDLY_API_KEY")

headers = {"Authorization": f"Bearer {CALENDLY_API_KEY}"}

print("🔑 Using this Calendly key:", CALENDLY_API_KEY)

# Try to get user info (will fail until you have a real key)
response = requests.get("https://api.calendly.com/users/me", headers=headers)

if response.status_code == 200:
    print("✅ Calendly connection successful!")
    print(response.json())
else:
    print("⚠️ Calendly connection failed:", response.status_code)
    print(response.text)
