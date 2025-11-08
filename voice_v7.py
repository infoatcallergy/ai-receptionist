
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, FileResponse
import os, json, requests, time
from twilio.twiml.voice_response import VoiceResponse
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone as dt_tz

# ---------------------------------------------------
# ENV & CLIENTS
# ---------------------------------------------------
load_dotenv()
app = FastAPI()

OPENAI_KEY   = os.getenv("OPENAI_API_KEY")
client       = OpenAI(api_key=OPENAI_KEY)

XI_API_KEY   = os.getenv("XI_API_KEY")
XI_VOICE_ID  = os.getenv("XI_VOICE_ID")

TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

CALENDLY_API_KEY = os.getenv("CALENDLY_API_KEY")
CALENDLY_EVENT_TYPE_URI = os.getenv("CALENDLY_EVENT_TYPE_URI", "")  # set this in .env

# ---------------------------------------------------
# IN-MEMORY SESSIONS (per CallSid)
# ---------------------------------------------------
# SESSIONS[CallSid] = {
#   "history": [{"role":"user"/"assistant","content": "..."}],
#   "service": "cleaning"/"checkup"/"pain"/None,
#   "stage": "idle" | "checking_availability" | "offered_times",
# }
SESSIONS = {}

# ---------------------------------------------------
# HELPERS
# ---------------------------------------------------
def base_url(request: Request) -> str:
    scheme = request.headers.get("x-forwarded-proto", "http")
    host   = request.headers.get("host", "localhost:8000")
    return f"{scheme}://{host}"

def ensure_ready(path="reply.mp3", wait=1, retries=10):
    for _ in range(retries):
        if os.path.exists(path) and os.path.getsize(path) > 1000:
            return True
        time.sleep(wait)
    return False

def serve_mp3_file(name: str):
    path = f"{name}.mp3"
    if not os.path.exists(path):
        return PlainTextResponse("missing", status_code=404)
    return FileResponse(path, media_type="audio/mpeg")

@app.get("/{filename}.mp3")
def serve_mp3(filename: str):
    return serve_mp3_file(filename)

def tts_stream_to(file_path: str, text: str):
    headers = {"xi-api-key": XI_API_KEY}
    payload = {"text": text, "voice": XI_VOICE_ID, "model_id": "eleven_multilingual_v2"}
    with requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{XI_VOICE_ID}/stream",
        headers=headers,
        json=payload,
        stream=True
    ) as r:
        r.raise_for_status()
        with open(file_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=4096):
                if chunk:
                    f.write(chunk)

def detect_intent(text: str) -> str:
    msg = text.lower()
    if any(k in msg for k in ["emergency", "bleeding", "severe pain", "911"]):
        return "emergency"
    if any(k in msg for k in ["cancel", "can't make it", "cant make it"]):
        return "cancel"
    if any(k in msg for k in ["resched", "reschedule", "change time", "move appointment"]):
        return "reschedule"
    if any(k in msg for k in ["hour", "open", "close", "time are you open"]):
        return "hours"
    if any(k in msg for k in ["availability", "available", "today", "tomorrow", "slot", "time work"]):
        return "availability"
    if any(k in msg for k in ["clean", "cleaning", "hygiene"]):
        return "book_cleaning"
    if any(k in msg for k in ["checkup", "exam", "consult"]):
        return "book_checkup"
    if any(k in msg for k in ["pain", "toothache", "cavity", "hurts"]):
        return "book_pain"
    if any(k in msg for k in ["book", "appointment", "schedule"]):
        return "book"
    return "unknown"

def update_session_from_intent(sess: dict, intent: str):
    if intent.startswith("book_"):
        # lock service so we don't re-ask
        if intent == "book_cleaning":
            sess["service"] = "cleaning"
        elif intent == "book_checkup":
            sess["service"] = "checkup"
        elif intent == "book_pain":
            sess["service"] = "pain"
    elif intent == "book":
        # leave service None until clarified
        pass
    # availability stage
    if intent == "availability":
        sess["stage"] = "checking_availability"

def summarize_context(sess: dict) -> str:
    service = sess.get("service")
    stage   = sess.get("stage", "idle")
    bits = []
    if service:
        bits.append(f"Service requested: {service}.")
    bits.append(f"Conversation stage: {stage}.")
    return " ".join(bits)

def get_available_times_today():
    """Return a short list of friendly local time strings from Calendly if configured; else a couple of dummy slots."""
    if not (CALENDLY_API_KEY and CALENDLY_EVENT_TYPE_URI):
        # Fallback demo times
        local = datetime.now().astimezone()
        t1 = (local + timedelta(hours=1)).strftime("%-I:%M %p")
        t2 = (local + timedelta(hours=2)).strftime("%-I:%M %p")
        return [t1, t2]

    headers = {"Authorization": f"Bearer {CALENDLY_API_KEY}"}
    now = datetime.now(dt_tz.utc)
    start_time = (now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    end_time   = (now + timedelta(days=1)).isoformat().replace("+00:00", "Z")

    url = (
        "https://api.calendly.com/event_type_available_times"
        f"?event_type={CALENDLY_EVENT_TYPE_URI}&start_time={start_time}&end_time={end_time}"
    )
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        resp.raise_for_status()
        slots = resp.json().get("collection", [])
        out = []
        for s in slots[:5]:
            dt = datetime.fromisoformat(s["start_time"].replace("Z", "+00:00")).astimezone()
            out.append(dt.strftime("%-I:%M %p"))
        return out or []
    except Exception as e:
        print("⚠️ Calendly fetch failed:", e)
        return []

def system_prompt_for(sess: dict, settings: dict) -> str:
    ctx = summarize_context(sess)
    return (
        f"You are Callergy AI, a {settings.get('tone_style','warm and professional')} dental receptionist for "
        f"{settings.get('doctor_name','Dr. Smith')}. Keep answers concise, caring, and natural. "
        "Never re-ask for a service if the caller already provided it. "
        "If service is cleaning, move toward picking an exact time. "
        f"For hours, use: {settings.get('hours','Mon-Fri 9AM-5PM')}. "
        f"For emergencies, direct to: {settings.get('emergency_number','555-911-0000')}. "
        f"Conversation state: {ctx}"
    )

def settings_or_default():
    try:
        return json.load(open("settings.json"))
    except Exception:
        return {"doctor_name":"Dr. Smith","hours":"Mon-Fri 9AM-5PM","emergency_number":"555-911-0000","tone_style":"warm and professional"}

# ---------------------------------------------------
# 0) PRE-GENERATE a tiny filler (once) for perceived speed
# ---------------------------------------------------
def ensure_filler():
    if os.path.exists("filler.mp3") and os.path.getsize("filler.mp3") > 1000:
        return
    text = "Just a moment while I check that."
    try:
        tts_stream_to("filler.mp3", text)
        print("🎛️ Pre-generated filler.mp3")
    except Exception as e:
        print("⚠️ Filler TTS failed:", e)

ensure_filler()

# ---------------------------------------------------
# 1️⃣ GREETING – ElevenLabs only
# ---------------------------------------------------
@app.post("/voice")
async def greet(request: Request):
    t_start = time.time()
    form = await request.form()
    call_sid = form.get("CallSid", "")
    caller = form.get("From", "")
    print(f"📞 Incoming call from {caller}  (CallSid={call_sid})")

    # init session
    SESSIONS[call_sid] = {"history": [], "service": None, "stage": "idle"}

    greeting_text = "Hi, you've reached Dr. Smith's dental office. How can I help you today?"
    
    t_tts_start = time.time()
    try:
        tts_stream_to("greeting.mp3", greeting_text)
        t_tts_end = time.time()
        print(f"🔊 Created greeting.mp3 | ⏱️ TTS: {t_tts_end - t_tts_start:.2f}s")
    except Exception as e:
        print("⚠️ Greeting TTS failed:", e)

    vr = VoiceResponse()
    vr.play(f"{base_url(request)}/greeting.mp3")
    vr.record(
        play_beep=False,
        timeout=2,
        max_length=20,
        trim="do-not-trim",
        method="POST",
        action="/process_audio"
    )
    
    t_end = time.time()
    print(f"⏱️ GREETING TOTAL: {t_end - t_start:.2f}s")
    return PlainTextResponse(str(vr), media_type="application/xml")

# ---------------------------------------------------
# 2️⃣ MAIN CONVERSATION LOOP (no speech, just listen)
# ---------------------------------------------------
@app.post("/conversation")
async def conversation(request: Request):
    vr = VoiceResponse()
    vr.record(
        play_beep=False,
        timeout=2,
        max_length=20,
        trim="do-not-trim",
        method="POST",
        action="/process_audio"
    )
    return PlainTextResponse(str(vr), media_type="application/xml")

# ---------------------------------------------------
# 3️⃣ PROCESS AUDIO → memory/intent → GPT → ElevenLabs → PLAY
#     Also supports proactive follow-up when "checking_availability"
# ---------------------------------------------------
@app.post("/process_audio")
async def process_audio(request: Request):
    t_request_start = time.time()
    print(f"\n⏱️ [REQUEST START] {time.strftime('%H:%M:%S')}")
    
    data = await request.form()
    call_sid = data.get("CallSid", "")
    rec_url  = data.get("RecordingUrl")
    dur = float(data.get("RecordingDuration", "0") or 0)
    print(f"🎙 CallSid={call_sid}  Recording: {rec_url}  ⏱ {dur}s")

    sess = SESSIONS.setdefault(call_sid, {"history": [], "service": None, "stage": "idle"})

    if dur < 0.6:
        print("⚠️ too short; listen again")
        vr = VoiceResponse()
        vr.redirect("/conversation", method="POST")
        return PlainTextResponse(str(vr), media_type="application/xml")

    # download user audio
    t_download_start = time.time()
    mp3_url = rec_url + ".mp3?Download=true"
    r = requests.get(mp3_url, auth=(TWILIO_SID, TWILIO_TOKEN))
    if r.status_code != 200:
        print("❌ download fail", r.status_code)
        vr = VoiceResponse()
        vr.redirect("/conversation", method="POST")
        return PlainTextResponse(str(vr), media_type="application/xml")
    with open("call.mp3", "wb") as f:
        f.write(r.content)
    t_download_end = time.time()
    print(f"✅ call.mp3 saved | ⏱️ Download: {t_download_end - t_download_start:.2f}s")

    # STT
    t_stt_start = time.time()
    with open("call.mp3", "rb") as a:
        tr = client.audio.transcriptions.create(model="whisper-1", file=a)
    user_text = (tr.text or "").strip()
    t_stt_end = time.time()
    print(f"🗣️ {user_text} | ⏱️ Whisper STT: {t_stt_end - t_stt_start:.2f}s")
    
    if not user_text:
        vr = VoiceResponse()
        vr.redirect("/conversation", method="POST")
        return PlainTextResponse(str(vr), media_type="application/xml")

    # memory + intent
    sess["history"].append({"role":"user","content": user_text})
    intent = detect_intent(user_text)
    update_session_from_intent(sess, intent)
    print("🧠 intent:", intent, "| service:", sess.get("service"), "| stage:", sess.get("stage"))

    settings = settings_or_default()

    # SPECIAL CASE: proactive follow-up (no new user audio)
    if sess.get("stage") == "checking_availability":
        # Step 1: Immediately play filler so caller hears something fast
        vr = VoiceResponse()
        if os.path.exists("filler.mp3"):
            vr.play(f"{base_url(request)}/filler.mp3")
        # Step 2: Redirect to agent_followup (AI will speak next without recording)
        vr.redirect("/agent_followup", method="POST")
        
        t_request_end = time.time()
        print(f"⏱️ [TOTAL RESPONSE TIME]: {t_request_end - t_request_start:.2f}s\n")
        return PlainTextResponse(str(vr), media_type="application/xml")

    # Otherwise: normal reply turn
    t_gpt_start = time.time()
    sys_prompt = system_prompt_for(sess, settings)
    chat = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role":"system","content":sys_prompt}] + sess["history"]
    )
    reply = (chat.choices[0].message.content or "").strip()
    t_gpt_end = time.time()
    print(f"🤖 {reply} | ⏱️ GPT: {t_gpt_end - t_gpt_start:.2f}s")
    sess["history"].append({"role":"assistant","content": reply})

    # TTS
    t_tts_start = time.time()
    tts_stream_to("reply.mp3", reply)
    ensure_ready("reply.mp3", 1, 6)
    t_tts_end = time.time()
    print(f"⏱️ ElevenLabs TTS: {t_tts_end - t_tts_start:.2f}s")

    vr = VoiceResponse()
    vr.play(f"{base_url(request)}/reply.mp3")
    vr.redirect("/conversation", method="POST")
    
    t_request_end = time.time()
    print(f"⏱️ [TOTAL RESPONSE TIME]: {t_request_end - t_request_start:.2f}s")
    print(f"   └─ Download: {t_download_end - t_download_start:.2f}s")
    print(f"   └─ Whisper: {t_stt_end - t_stt_start:.2f}s")
    print(f"   └─ GPT: {t_gpt_end - t_gpt_start:.2f}s")
    print(f"   └─ TTS: {t_tts_end - t_tts_start:.2f}s\n")
    
    return PlainTextResponse(str(vr), media_type="application/xml")

# ---------------------------------------------------
# 4️⃣ PROACTIVE FOLLOW-UP: the AI speaks next (no recording first)
#     Used after "Please hold on while I check..."
# ---------------------------------------------------
@app.post("/agent_followup")
async def agent_followup(request: Request):
    t_start = time.time()
    print(f"\n⏱️ [FOLLOWUP START] {time.strftime('%H:%M:%S')}")
    
    form = await request.form()
    call_sid = form.get("CallSid", "")
    sess = SESSIONS.setdefault(call_sid, {"history": [], "service": None, "stage": "idle"})

    # Pull availability (Calendly if configured; fallback demo times)
    t_calendly_start = time.time()
    times = get_available_times_today()
    t_calendly_end = time.time()
    print(f"⏱️ Calendly fetch: {t_calendly_end - t_calendly_start:.2f}s")
    
    if times:
        times_str = " and ".join(times[:2]) if len(times) >= 2 else times[0]
        service_phrase = f" for your {sess.get('service','appointment')}" if sess.get("service") else ""
        followup_text = f"I can offer {times_str} today{service_phrase}. Which time works best for you?"
    else:
        followup_text = "I wasn't able to load the schedule just now. Do you prefer earlier in the afternoon or later?"

    print("📅 Follow-up:", followup_text)
    sess["history"].append({"role":"assistant","content": followup_text})
    sess["stage"] = "offered_times"

    # TTS + play
    t_tts_start = time.time()
    tts_stream_to("reply.mp3", followup_text)
    ensure_ready("reply.mp3", 1, 6)
    t_tts_end = time.time()
    print(f"⏱️ ElevenLabs TTS: {t_tts_end - t_tts_start:.2f}s")

    vr = VoiceResponse()
    vr.play(f"{base_url(request)}/reply.mp3")
    vr.redirect("/conversation", method="POST")
    
    t_end = time.time()
    print(f"⏱️ [FOLLOWUP TOTAL]: {t_end - t_start:.2f}s\n")
    
    return PlainTextResponse(str(vr), media_type="application/xml")
