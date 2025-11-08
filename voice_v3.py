from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, FileResponse
import os, json, requests
from twilio.twiml.voice_response import VoiceResponse
from openai import OpenAI
from dotenv import load_dotenv

# -------------------------------------------------
# Environment / Clients
# -------------------------------------------------
load_dotenv()
app = FastAPI()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# 🧠 Corrected variable names for ElevenLabs
ELEVEN_KEY = os.getenv("XI_API_KEY")
ELEVEN_VOICE = os.getenv("XI_VOICE_ID")

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

if not TWILIO_SID or not TWILIO_TOKEN:
    print("⚠️ Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN in .env — recording download will fail (401).")
if not ELEVEN_KEY or not ELEVEN_VOICE:
    print("⚠️ Missing XI_API_KEY or XI_VOICE_ID — ElevenLabs TTS will fail.")

# -------------------------------------------------
# Helpers
# -------------------------------------------------
def public_base_url(request: Request) -> str:
    scheme = request.headers.get("x-forwarded-proto") or "http"
    host = request.headers.get("host")
    return f"{scheme}://{host}"

def speak_voice() -> str:
    return "Polly.Joanna"

@app.get("/reply.mp3")
def get_reply_mp3():
    path = "reply.mp3"
    if not os.path.exists(path):
        return PlainTextResponse("Audio not found", status_code=404)
    return FileResponse(path, media_type="audio/mpeg")

# -------------------------------------------------
# 1) Greet once
# -------------------------------------------------
@app.post("/voice")
async def voice_greet(request: Request):
    form = await request.form()
    from_number = form.get("From", "")
    print(f"📞 Incoming call from {from_number}")

    vr = VoiceResponse()
    vr.say("Hi, you've reached the dental office. One moment please while I listen.", voice=speak_voice())
    vr.pause(length=1)
    vr.record(play_beep=True, timeout=5, transcribe=False, recording_status_callback="/process_audio")
    return PlainTextResponse(str(vr), media_type="application/xml")

# -------------------------------------------------
# 2) Listen again (loop)
# -------------------------------------------------
@app.post("/voice_listen")
async def voice_listen(request: Request):
    print("👂 Listening again…")
    vr = VoiceResponse()
    vr.record(play_beep=True, timeout=5, transcribe=False, recording_status_callback="/process_audio")
    return PlainTextResponse(str(vr), media_type="application/xml")

# -------------------------------------------------
# 3) Process recording
# -------------------------------------------------
@app.post("/process_audio")
async def process_audio(request: Request):
    data = await request.form()
    recording_url = data.get("RecordingUrl")
    print("🎙 Recording URL:", recording_url)

    raw_url = recording_url + ".mp3?Download=true"
    print("⬇️ Downloading:", raw_url)

    try:
        r = requests.get(raw_url, stream=True, auth=(TWILIO_SID, TWILIO_TOKEN))
        if r.status_code != 200:
            print("⚠️ Couldn't download audio:", r.status_code, r.text)
            vr_err = VoiceResponse()
            vr_err.say("I couldn't hear that clearly. Could you say that again?", voice=speak_voice())
            vr_err.redirect("/voice_listen")
            return PlainTextResponse(str(vr_err), media_type="application/xml")
        with open("call.mp3", "wb") as f:
            for chunk in r.iter_content(1024):
                f.write(chunk)
        print("✅ Audio saved to call.mp3")
    except Exception as e:
        print("❌ Error downloading recording:", e)
        vr_err = VoiceResponse()
        vr_err.say("There was a problem receiving your audio. Please try again.", voice=speak_voice())
        vr_err.redirect("/voice_listen")
        return PlainTextResponse(str(vr_err), media_type="application/xml")

    # Whisper STT
    try:
        with open("call.mp3", "rb") as audio:
            transcription = client.audio.transcriptions.create(model="whisper-1", file=audio)
        text = transcription.text or ""
        print("🗣️ Transcribed:", text)
    except Exception as e:
        print("❌ Whisper error:", e)
        vr_err = VoiceResponse()
        vr_err.say("Sorry, I didn't catch that. Could you repeat?", voice=speak_voice())
        vr_err.redirect("/voice_listen")
        return PlainTextResponse(str(vr_err), media_type="application/xml")

    # ChatGPT reply
    try:
        settings = json.load(open("settings.json"))
    except Exception:
        settings = {
            "doctor_name": "Dr. Smith",
            "hours": "Mon-Fri 9AM-5PM",
            "emergency_number": "555-911-0000",
            "tone_style": "warm and professional"
        }

    system_prompt = (
        f"You are Callergy AI, a {settings['tone_style']} dental receptionist for "
        f"{settings['doctor_name']}. "
        "Be concise, helpful, and natural. If the caller wants to book, ask for preferred day/time and reason. "
        f"For hours, answer using: {settings['hours']}. "
        f"For emergencies, direct to: {settings['emergency_number']}."
    )

    try:
        chat = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ]
        )
        reply = (chat.choices[0].message.content or "").strip()
        print("🤖 Reply:", reply)
    except Exception as e:
        print("❌ ChatGPT error:", e)
        reply = "I'm sorry, I had trouble processing that."

    # ElevenLabs TTS (using your XI_ variables)
    try:
        tts_payload = {"text": reply, "voice": ELEVEN_VOICE, "model_id": "eleven_multilingual_v2"}
        headers = {"xi-api-key": ELEVEN_KEY}
        tts = requests.post("https://api.elevenlabs.io/v1/text-to-speech/" + ELEVEN_VOICE, headers=headers, json=tts_payload)
        if tts.status_code != 200:
            print("❌ ElevenLabs TTS error:", tts.status_code, tts.text)
            raise RuntimeError("TTS failed")
        with open("reply.mp3", "wb") as f:
            f.write(tts.content)
        print("🔊 Created reply.mp3")
    except Exception as e:
        print("❌ ElevenLabs error:", e)
        vr_fallback = VoiceResponse()
        vr_fallback.say(reply, voice=speak_voice())
        vr_fallback.say("Anything else I can help you with?", voice=speak_voice())
        vr_fallback.redirect("/voice_listen")
        return PlainTextResponse(str(vr_fallback), media_type="application/xml")

    # Play + loop
    base = public_base_url(request)
    audio_url = f"{base}/reply.mp3"
    vr = VoiceResponse()
    vr.play(audio_url)
    vr.pause(length=1)
    vr.say("Anything else I can help you with?", voice=speak_voice())
    vr.redirect("/voice_listen")
    print("✅ Sent reply + looping back to listen.")
    return PlainTextResponse(str(vr), media_type="application/xml")
