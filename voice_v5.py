# voice.py
# ------------------------------------------------------------
# Callergy AI realtime voice bot
# Twilio Media Streams → OpenAI GPT-4o Realtime
# ------------------------------------------------------------
import os, json, base64, asyncio, numpy as np, io, soundfile as sf
from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
import aiohttp
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from scipy.signal import resample_poly

load_dotenv()
app = FastAPI()

OPENAI_KEY = os.getenv("OPENAI_API_KEY")

# ------------------------------------------------------------
# HELPER: convert GPT audio → Twilio format
# ------------------------------------------------------------
def wav_to_pcm16_8khz(wav_bytes: bytes) -> bytes:
    audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
    if len(audio.shape) > 1:
        audio = np.mean(audio, axis=1)
    if sr != 8000:
        audio = resample_poly(audio, 8000, sr)
    audio_i16 = np.clip(audio * 32768, -32768, 32767).astype(np.int16)
    return audio_i16.tobytes()

# ------------------------------------------------------------
# TWILIO ENTRY
# ------------------------------------------------------------
@app.post("/voice")
async def voice_entry():
    """Webhook Twilio hits first."""
    vr = VoiceResponse()
    connect = Connect()
    # ✅ SAFE: only inbound audio; fully supported
    connect.append(Stream(
        url="wss://scablike-cole-fuzzily.ngrok-free.dev/media-stream",
        audioTrack="inbound_track"
    ))
    vr.append(connect)
    return PlainTextResponse(str(vr), media_type="text/xml")

# ------------------------------------------------------------
# MEDIA STREAM
# ------------------------------------------------------------
@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("🔗 Twilio stream connected")

    headers = {
        "Authorization": f"Bearer {OPENAI_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    session = aiohttp.ClientSession(headers=headers)
    openai_ws = await session.ws_connect(
        "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"
    )

    # ---- Configure GPT ----
    await openai_ws.send_json({
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "instructions": (
                "You are Callergy AI, Dr. Smith’s friendly dental receptionist. "
                "Greet the caller warmly, ask how you can help, and reply with short, natural answers."
            ),
            "voice": "alloy",
        },
    })
    await asyncio.sleep(1)

    # ---- GPT greets immediately ----
    await openai_ws.send_json({
        "type": "response.create",
        "response": {
            "modalities": ["audio", "text"],
            "instructions": "Say hello and introduce yourself.",
        },
    })
    print("✅ GPT instructed to greet the caller")

    # ---- Audio handling ----
    async def forward_audio_to_openai():
        async for msg in ws.iter_text():
            data = json.loads(msg)
            if data.get("event") == "media":
                pcm = base64.b64decode(data["media"]["payload"])
                print(f"🎙 Received {len(pcm)} bytes from Twilio")
                float_audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
                await openai_ws.send_json({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(float_audio.tobytes()).decode(),
                })
            elif data.get("event") == "stop":
                await openai_ws.send_json({"type": "input_audio_buffer.commit"})

    async def handle_openai_responses():
        async for msg in openai_ws:
            event = json.loads(msg.data)
            if event.get("type") == "response.output_text.delta":
                print("🧠 GPT:", event["delta"])
            elif event.get("type") == "response.output_audio.delta":
                wav_chunk = base64.b64decode(event["delta"])
                pcm16 = wav_to_pcm16_8khz(wav_chunk)
                payload = base64.b64encode(pcm16).decode()
                await ws.send_json({"event": "media", "media": {"payload": payload}})
                print("🎧 Sent PCM16 chunk to Twilio")
            elif event.get("type") == "response.completed":
                print("✅ GPT finished speaking")
            elif event.get("type") == "error":
                print("❌ OpenAI Error:", event)

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(forward_audio_to_openai())
            tg.create_task(handle_openai_responses())
    except Exception as e:
        print("⚠️ Stream error:", e)
    finally:
        await openai_ws.close()
        await session.close()
        print("❌ Stream closed")
