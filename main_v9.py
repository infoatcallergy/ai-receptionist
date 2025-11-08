from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse
import os
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables (API keys)
load_dotenv()

# Initialize FastAPI app
app = FastAPI()

# Set up OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# -------------------------------------------------
# 🔹 Fetch next few available time slots from Calendly
# -------------------------------------------------
def get_available_times():
    CALENDLY_API_KEY = os.getenv("CALENDLY_API_KEY")
    EVENT_TYPE_URI = "https://api.calendly.com/event_types/a8bf7e72-ea28-4b09-9d92-53c6e98714ce"
    headers = {"Authorization": f"Bearer {CALENDLY_API_KEY}"}

    now = datetime.now(timezone.utc)
    start_time = (now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    end_time = (now + timedelta(days=7)).isoformat().replace("+00:00", "Z")

    url = (
        f"https://api.calendly.com/event_type_available_times?"
        f"event_type={EVENT_TYPE_URI}&start_time={start_time}&end_time={end_time}"
    )

    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print("⚠️ Calendly API error:", response.status_code)
        return []

    data = response.json().get("collection", [])
    slots = [s["start_time"] for s in data][:5]
    formatted = []
    for s in slots:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        local_time = dt.astimezone()
        formatted.append(local_time.strftime("%A %I:%M %p"))
    return formatted

# -------------------------------------------------
# 🔹 Book appointment automatically in Calendly
# -------------------------------------------------
def book_appointment(start_time, name="New Patient", email="info@callergy.ai"):
    """
    Creates a Calendly scheduling link for a given start time.
    Returns the booking URL if successful.
    """
    CALENDLY_API_KEY = os.getenv("CALENDLY_API_KEY")
    EVENT_TYPE_URI = "https://api.calendly.com/event_types/a8bf7e72-ea28-4b09-9d92-53c6e98714ce"

    headers = {
        "Authorization": f"Bearer {CALENDLY_API_KEY}",
        "Content-Type": "application/json"
    }

    # Calendly requires both `owner` and `owner_type`
    payload = {
        "owner": EVENT_TYPE_URI,
        "owner_type": "EventType",
        "max_event_count": 1,
        "start_time": start_time,
        "timezone": "America/Toronto"  # your clinic's timezone
    }

    response = requests.post(
        "https://api.calendly.com/scheduling_links",
        headers=headers,
        json=payload
    )

    print("📅 Booking attempt status:", response.status_code, response.text)

    if response.status_code in (200, 201):
        data = response.json()
        booking_url = data.get("resource", {}).get("booking_url")
        if booking_url:
            print("✅ Created booking link:", booking_url)
            return booking_url
    return None

# -------------------------------------------------
# 🔹 Twilio endpoint – handles incoming messages
# -------------------------------------------------
@app.post("/sms")
async def sms_reply(Body: str = Form(...)):
    """Handles incoming text messages from Twilio and replies using AI."""
    print("📩 Incoming message:", Body)

    # Generate AI reply using OpenAI
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are Callergy AI, a warm and professional dental receptionist."},
            {"role": "user", "content": Body}
        ]
    )

    reply = response.choices[0].message.content.strip()
    print("🤖 AI reply:", reply)

    # 🧠 Step 1: When the user asks to book
    if "book" in Body.lower() or "appointment" in Body.lower():
        times = get_available_times()
        if times:
            time_list = ", ".join(times)
            reply = f"We have openings at {time_list}. Which works best for you?"
        else:
            reply = (
                "Sure! What day and time work best for you? "
                "You can also use our online booking link: https://calendly.com/callergy-info"
            )

    # 🧠 Step 2: When the user confirms a time (like “Wednesday 9:30”)
    elif any(day in Body.lower() for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]):
        utc_now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        booking_link = book_appointment(start_time=utc_now)

        if booking_link:
            reply = f"✅ Perfect — here’s your confirmation link: {booking_link}"
        else:
            reply = (
                "Something went wrong while booking — please try again or use the online link: "
                "https://calendly.com/callergy-info"
            )

    # 🧾 Step 3: Log the conversation
    os.makedirs("logs", exist_ok=True)
    with open("logs/conversation_log.txt", "a", encoding="utf-8") as log:
        log.write(f"User: {Body}\n")
        log.write(f"AI: {reply}\n\n")

    # Step 4: Build and return the Twilio response
    resp = MessagingResponse()
    resp.message(reply)
    return PlainTextResponse(str(resp), media_type="application/xml")



