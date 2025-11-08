from twilio.rest import Client
import os
from dotenv import load_dotenv

# Load your secret keys from the .env file
load_dotenv()

# Get Twilio info from environment variables
account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
twilio_number = os.getenv("TWILIO_NUMBER")

# Connect to Twilio
client = Client(account_sid, auth_token)

# Send a test message
message = client.messages.create(
    from_=twilio_number,
    to="+13435756312",  # 👈 Replace this with your real phone number!
    body="Hello from Callergy AI! 🎉 Your AI receptionist is alive."
)

print("✅ Message sent successfully!")
print("Message SID:", message.sid)
