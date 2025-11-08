from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse
import os
import requests
import json
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables
load_dotenv()

app = FastAPI()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# -------------------------------------------------
# 🔹 Detect what the user wants (smarter intent)
# -------------------------------------------------
def detect_intent(message: str):
    msg = message.lower().strip()

    # Priority order to avoid misfires
    if any(x in msg for x in ["emergency", "bleeding", "pain", "tooth broke"]):
        return "emergency"
    elif any(x in msg for x in ["cancel", "can't make it", "call off", "remove appointment"]):
        return "cancel"
    elif any(x in msg for x in ["resched", "change time", "move appointment", "different time"]):
        return "reschedule"
    elif any(x in msg for x in ["hour", "open", "close", "business hours", "time you’re open"]):
        return "hours"
    elif any(x in msg for x in ["thank", "thanks", "appreciate", "grateful"]):
        return "gratitude"
    elif any(x in msg for x in ["book", "appointment", "schedule", "cleaning", "checkup", "visit"]):
        return "book"
    else:
        return "unknown"

# -------------------------------------------------
# 🔹 Fetch available times from Calendly
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
    CALENDLY_API_KEY = os.getenv("CALENDLY_API_KEY")
    EVENT_TYPE_URI = "https://api.calendly.com/event_types/a8bf7e72-ea28-4b09-9d92-53c6e98714ce"

    headers = {
        "Authorization": f"Bearer {CALENDLY_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "owner": EVENT_TYPE_URI,
        "owner_type": "EventType",
        "max_event_count": 1,
        "start_time": start_time,
        "timezone": "America/Toronto"
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
# 🔹 Twilio endpoint — handles incoming texts
# -------------------------------------------------
@app.post("/sms")
async def sms_reply(Body: str = Form(...)):
    print("📩 Incoming message:", Body)

    # Load clinic settings
    settings = json.load(open("settings.json"))
    intent = detect_intent(Body)

    reply = ""

    # Route by detected intent
    if intent == "book":
        if settings.get("auto_book"):
            utc_now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            booking_link = book_appointment(start_time=utc_now)
            if booking_link:
                reply = f"✅ You're booked! Here's your confirmation link: {booking_link}"
            else:
                reply = "Something went wrong while booking—please try again or use the online link."
        else:
            times = get_available_times()
            time_list = ", ".join(times)
            reply = f"We have openings at {time_list}. Which works best for you?"

    elif intent == "reschedule":
        reply = "No problem! Please tell me your current appointment date so I can reschedule it."

    elif intent == "cancel":
        reply = "Got it. Please confirm your name and the date you'd like to cancel."

    elif intent == "hours":
        reply = f"Our hours are {settings['hours']}. Would you like me to help you book a time?"

    elif intent == "emergency":
        reply = f"This sounds urgent. Please call our emergency line at {settings['emergency_number']} immediately."

    elif intent == "gratitude":
        reply = "You're very welcome! Happy to help."

    else:
        # Default fallback via OpenAI
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are Callergy AI, a warm and professional dental receptionist."},
                {"role": "user", "content": Body}
            ]
        )
        reply = response.choices[0].message.content.strip()

    print("🤖 AI reply:", reply)

    # 🧾 Log conversation
    os.makedirs("logs", exist_ok=True)
    with open("logs/conversation_log.txt", "a", encoding="utf-8") as log:
        log.write(f"User: {Body}\n")
        log.write(f"AI: {reply}\n")
        log.write(f"Detected Intent: {intent}\n\n")

    # Return Twilio response
    resp = MessagingResponse()
    resp.message(reply)
    return PlainTextResponse(str(resp), media_type="application/xml")






