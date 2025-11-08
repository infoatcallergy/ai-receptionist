from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse
import os
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from openai import OpenAI  # ✅ moved up here

# Load environment variables (API keys)
load_dotenv()

# Initialize FastAPI app
app = FastAPI()

# Set up OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# -------------------------------
# 🧩 Helper function – Fetch real Calendly slots
# -------------------------------
def get_available_times():
    """Pulls the next few available times from Calendly."""
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
    slots = [s["start_time"] for s in data][:3]  # get first 3 slots
    # Convert UTC → readable local format (Eastern Time for clinic)
    formatted = []
    for s in slots:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        local_time = dt.astimezone()  # auto-convert to your system timezone
        formatted.append(local_time.strftime("%A %I:%M %p"))
    return formatted

# -------------------------------
# 📨 Twilio endpoint
# -------------------------------
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

    # 🧠 Detect if the message is about booking
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

    # 🧾 Log the conversation
    os.makedirs("logs", exist_ok=True)
    with open("logs/conversation_log.txt", "a", encoding="utf-8") as log:
        log.write(f"User: {Body}\n")
        log.write(f"AI: {reply}\n\n")

    # Build Twilio reply
    resp = MessagingResponse()
    resp.message(reply)
    return PlainTextResponse(str(resp), media_type="application/xml")




