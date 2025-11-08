from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse
import os, json, requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
app = FastAPI()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

BOOKINGS_FILE = "logs/bookings.json"

# ------------------ Helpers ------------------
def load_bookings():
    if os.path.exists(BOOKINGS_FILE):
        with open(BOOKINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_bookings(data):
    os.makedirs("logs", exist_ok=True)
    with open(BOOKINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# ------------------ Intent Detection ------------------
def detect_intent(message: str):
    msg = message.lower().strip()
    if any(x in msg for x in ["emergency","bleeding","pain","tooth broke"]): return "emergency"
    if any(x in msg for x in ["cancel","can't make it","remove appointment"]): return "cancel"
    if any(x in msg for x in ["resched","change time","move appointment"]): return "reschedule"
    if any(x in msg for x in ["hour","open","close","business hours"]): return "hours"
    if any(x in msg for x in ["thank","thanks","appreciate","grateful"]): return "gratitude"
    if any(x in msg for x in ["book","appointment","schedule","cleaning","checkup","visit"]): return "book"
    return "unknown"

def extract_reason(msg: str):
    m = msg.lower()
    if "clean" in m: return "Cleaning"
    if "pain" in m or "hurt" in m: return "Pain / Emergency"
    if "check" in m: return "Checkup"
    if "consult" in m: return "Consultation"
    return "General dental visit"

# ------------------ Calendly ------------------
def get_available_times():
    key = os.getenv("CALENDLY_API_KEY")
    event = "https://api.calendly.com/event_types/a8bf7e72-ea28-4b09-9d92-53c6e98714ce"
    hdr = {"Authorization": f"Bearer {key}"}
    now = datetime.now(timezone.utc)
    start = (now+timedelta(minutes=1)).isoformat().replace("+00:00","Z")
    end = (now+timedelta(days=7)).isoformat().replace("+00:00","Z")
    url = f"https://api.calendly.com/event_type_available_times?event_type={event}&start_time={start}&end_time={end}"
    r = requests.get(url, headers=hdr)
    if r.status_code!=200: return []
    data = r.json().get("collection", [])
    out=[]
    for s in [d["start_time"] for d in data][:5]:
        dt=datetime.fromisoformat(s.replace("Z","+00:00")).astimezone()
        out.append(dt.strftime("%A %I:%M %p"))
    return out

def book_appointment(start_time, reason="General Visit"):
    key = os.getenv("CALENDLY_API_KEY")
    event = "https://api.calendly.com/event_types/a8bf7e72-ea28-4b09-9d92-53c6e98714ce"
    hdr={"Authorization":f"Bearer {key}","Content-Type":"application/json"}
    payload={
        "owner":event,
        "owner_type":"EventType",
        "max_event_count":1,
        "start_time":start_time,
        "timezone":"America/Toronto",
        "metadata":{"reason_for_visit":reason}
    }
    r=requests.post("https://api.calendly.com/scheduling_links",headers=hdr,json=payload)
    if r.status_code in (200,201):
        data=r.json().get("resource",{})
        return data.get("booking_url")
    return None

# ------------------ Twilio SMS Endpoint ------------------
@app.post("/sms")
async def sms_reply(Body: str = Form(...), From: str = Form("+000")):
    print("📩",Body)
    settings=json.load(open("settings.json"))
    intent=detect_intent(Body)
    user=From.replace("whatsapp:","").replace("+","")
    bookings=load_bookings()
    reply=""

    # --- Conversational Flow ---
    if user in bookings and intent=="book" and bookings[user].get("status")=="booked":
        reply=f"You already have an appointment on {bookings[user]['date']} for {bookings[user]['reason']}. Want to reschedule or cancel?"
    elif intent=="book":
        reply="Sure! What’s the reason for your visit — cleaning, pain, or check-up?"
        bookings[user]={"status":"awaiting_reason"}
        save_bookings(bookings)
    elif user in bookings and bookings[user].get("status")=="awaiting_reason":
        reason=extract_reason(Body)
        times=get_available_times()
        msg=", ".join(times[:3]) if times else "next few days"
        reply=f"Got it — {reason}. What day and time work best for you? (Available: {msg})"
        bookings[user]={"status":"awaiting_time","reason":reason}
        save_bookings(bookings)
    elif user in bookings and bookings[user].get("status")=="awaiting_time":
        reason=bookings[user]["reason"]
        utc_now=datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
        link=book_appointment(start_time=utc_now,reason=reason)
        if link:
            reply=f"✅ Great! You’re booked for {reason}. Here’s your confirmation link: {link}"
            bookings[user]={"status":"booked","date":str(datetime.now().date()),"reason":reason}
            save_bookings(bookings)
        else:
            reply="Something went wrong while booking — please try again."
    elif intent=="cancel":
        if user in bookings:
            reply="Your appointment has been cancelled. Hope to see you soon!"
            bookings.pop(user); save_bookings(bookings)
        else:
            reply="I don’t see an appointment under your number. Want me to book one?"
    elif intent=="hours":
        reply=f"Our hours are {settings['hours']}. Would you like me to help you book a time?"
    elif intent=="emergency":
        reply=f"This sounds urgent. Please call our emergency line at {settings['emergency_number']} immediately."
    elif intent=="gratitude":
        reply="You’re very welcome! Happy to help."
    else:
        r=client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":"You are Callergy AI, a warm and professional dental receptionist."},
                {"role":"user","content":Body}
            ])
        reply=r.choices[0].message.content.strip()

    print("🤖",reply)
    os.makedirs("logs",exist_ok=True)
    with open("logs/conversation_log.txt","a",encoding="utf-8") as log:
        log.write(f"{user}: {Body}\nAI: {reply}\nIntent: {intent}\n\n")

    resp=MessagingResponse(); resp.message(reply)
    return PlainTextResponse(str(resp),media_type="application/xml")


