
# ------------------------------------------------------------
# voice.py — Realtime-first, Twilio <-> OpenAI WS proxy
# Env keys expected (exactly as in your .env):
#   XI_API_KEY, XI_VOICE_ID, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
#   TWILIO_NUMBER, OPENAI_API_KEY, CALENDLY_API_KEY
# Only OPENAI_API_KEY is required for this Realtime version.
# ------------------------------------------------------------
import os, json, base64, asyncio, pathlib
from typing import Optional

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from twilio.twiml.voice_response import VoiceResponse, Connect

import numpy as np

load_dotenv()

# --- Read envs exactly as you named them ---
XI_API_KEY          = os.getenv("XI_API_KEY")           # not used in realtime-only path
XI_VOICE_ID         = os.getenv("XI_VOICE_ID")          # not used in realtime-only path
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")   # not required for media streams
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")    # not required for media streams
TWILIO_NUMBER       = os.getenv("TWILIO_NUMBER")        # not used here
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")       # REQUIRED
CALENDLY_API_KEY    = os.getenv("CALENDLY_API_KEY")     # not used here

REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
OPENAI_VOICE   = os.getenv("OPENAI_VOICE", "verse")
SYSTEM_PROMPT  = os.getenv("SYSTEM_PROMPT",
    "You are a warm, efficient dental receptionist named Sophie. Never say you are AI. Keep responses concise and helpful."
)

# --- Startup sanity check ---
print("🧪 SANITY CHECK — cwd:", pathlib.Path().resolve())
print("🧪 SANITY CHECK — .env OPENAI_API_KEY present:", bool(OPENAI_API_KEY))
print("🧪 SANITY CHECK — model:", REALTIME_MODEL)
print("🧪 SANITY CHECK — voice:", OPENAI_VOICE)

app = FastAPI()

# ---- TwiML webhook: ask for bidirectional media stream --------------------
@app.post("/voice")
async def voice_webhook(request: Request):
    host = request.headers.get("host", "")
    vr = VoiceResponse()
    with Connect() as connect:
        connect.stream(url=f"wss://{host}/media-stream", track="both_tracks")
    vr.append(connect)
    return PlainTextResponse(str(vr), media_type="application/xml")

# ---- Helpers: μ-law + resampling -----------------------------------------
def pcm16_to_mulaw(samples: np.ndarray) -> bytes:
    if samples.size == 0:
        return b""
    x = np.clip(samples.astype(np.float32) / 32768.0, -1.0, 1.0)
    mu = 255.0
    y = np.sign(x) * np.log1p(mu * np.abs(x)) / np.log1p(mu)
    mu_law = ((y + 1) / 2 * 255).astype(np.uint8) ^ 0xFF
    return mu_law.tobytes()

def mulaw_to_pcm16(mu: np.ndarray) -> np.ndarray:
    if mu.size == 0:
        return np.array([], dtype=np.int16)
    y = (mu ^ 0xFF).astype(np.float32)
    y = (y / 255.0) * 2.0 - 1.0
    mu_c = 255.0
    x = np.sign(y) * ((1.0 + mu_c) ** np.abs(y) - 1.0) / mu_c
    return np.clip(x * 32768.0, -32768, 32767).astype(np.int16)

def upsample_8k_to_24k(pcm16_8k: np.ndarray) -> np.ndarray:
    if pcm16_8k.size == 0:
        return pcm16_8k
    return np.repeat(pcm16_8k, 3).astype(np.int16)

def downsample_24k_to_8k(pcm16_24k: np.ndarray) -> np.ndarray:
    n = pcm16_24k.size
    if n == 0:
        return pcm16_24k
    trim = n - (n // 3) * 3
    if trim:
        pcm16_24k = pcm16_24k[:-trim]
    if pcm16_24k.size == 0:
        return pcm16_24k
    return pcm16_24k.reshape(-1, 3).mean(axis=1).astype(np.int16)

# ---- WS proxy: Twilio <-> OpenAI Realtime -------------------------------
@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("🔗 Twilio stream connected")

    if not OPENAI_API_KEY:
        print("❌ OPENAI_API_KEY missing in environment. Check your .env file.")
        await ws.close()
        return

    import websockets
    oa_url = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"

    try:
        async with websockets.connect(
            oa_url,
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1",
            },
            ping_interval=10,
            ping_timeout=20,
            max_size=8_000_000,
        ) as oa:
            print("✅ Connected to GPT session")

            await oa.send(json.dumps({
                "type":"session.update",
                "session":{
                    "instructions": SYSTEM_PROMPT,
                    "input_audio_format":{"type":"wav","sample_rate_hz":8000,"channels":1},
                    "output_audio_format":{"type":"pcm16","sample_rate_hz":24000,"channels":1},
                    "voice": OPENAI_VOICE,
                }
            }))

            await oa.send(json.dumps({
                "type":"response.create",
                "response":{
                    "instructions":"Hi, thanks for calling the dental office. This is Sophie. How can I help you today?",
                    "modalities":["audio"],
                    "conversation":"none",
                    "audio":{"voice":OPENAI_VOICE},
                }
            }))
            print("🚀 Forced greeting sent immediately")

            TWILIO_FRAME_MS = 20
            MIN_COMMIT_MS   = 120
            frames_since_commit = 0
            stream_sid: Optional[str] = None
            cancelled_once = False

            async def twilio_to_openai():
                nonlocal frames_since_commit, stream_sid, cancelled_once
                while True:
                    raw = await ws.receive_text()
                    data = json.loads(raw)
                    event = data.get("event")

                    if event == "connected":
                        continue
                    if event == "start":
                        stream_sid = data["start"]["streamSid"]
                        continue
                    if event == "stop":
                        break
                    if event != "media":
                        continue

                    payload_b64 = data["media"].get("payload")
                    if not payload_b64:
                        continue

                    ulaw = np.frombuffer(base64.b64decode(payload_b64), dtype=np.uint8)
                    pcm8 = mulaw_to_pcm16(ulaw)
                    if pcm8.size == 0:
                        continue

                    if not cancelled_once:
                        await oa.send(json.dumps({"type":"response.cancel"}))
                        cancelled_once = True

                    pcm24 = upsample_8k_to_24k(pcm8)
                    await oa.send(json.dumps({
                        "type":"input_audio_buffer.append",
                        "audio": base64.b64encode(pcm24.tobytes()).decode()
                    }))
                    frames_since_commit += 1

                    if frames_since_commit * TWILIO_FRAME_MS >= MIN_COMMIT_MS:
                        await oa.send(json.dumps({"type":"input_audio_buffer.commit"}))
                        print(f"🟢 Committed audio to GPT (~{frames_since_commit*TWILIO_FRAME_MS}ms)")
                        frames_since_commit = 0

            async def openai_to_twilio():
                nonlocal stream_sid
                async for msg in oa:
                    evt = json.loads(msg)

                    if evt.get("type") == "response.audio.delta":
                        pcm24 = np.frombuffer(base64.b64decode(evt["audio"]), dtype=np.int16)
                        pcm8  = downsample_24k_to_8k(pcm24)
                        ulaw  = pcm16_to_mulaw(pcm8)
                        if not stream_sid or not ulaw:
                            continue
                        await ws.send_text(json.dumps({
                            "event":"media",
                            "streamSid": stream_sid,
                            "media":{"payload": base64.b64encode(ulaw).decode()}
                        }))

                    elif evt.get("type") == "error":
                        print("❌ OpenAI Error:", evt)

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    except Exception as e:
        print("❌ WS proxy error:", repr(e))

# ---- Healthcheck ---------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")
