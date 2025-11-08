
# ------------------------------------------------------------
# voice.py — Twilio <-> OpenAI Realtime proxy (production-ready)
# - TwiML is returned explicitly (Connect+Stream)
# - PUBLIC_URL controls the WSS base (no guessing)
# - Realtime audio in both directions (via aiohttp -> OpenAI)
# ------------------------------------------------------------
import os, json, base64, asyncio, pathlib
from typing import Optional

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

# Twilio TwiML helpers (we build XML manually; import kept for clarity)
from twilio.twiml.voice_response import VoiceResponse, Connect  # noqa: F401

import numpy as np

load_dotenv()

# --- Env vars (read exactly as you named them) ------------------------------
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")       # REQUIRED
PUBLIC_URL          = os.getenv("PUBLIC_URL")           # REQUIRED in prod
REALTIME_MODEL      = os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
OPENAI_VOICE        = os.getenv("OPENAI_VOICE", "verse")
SYSTEM_PROMPT       = os.getenv("SYSTEM_PROMPT",
    "You are a warm, efficient dental receptionist named Sophie. Never say you are AI. Keep responses concise and helpful."
)

# Others (for future features)
XI_API_KEY          = os.getenv("XI_API_KEY")
XI_VOICE_ID         = os.getenv("XI_VOICE_ID")
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER       = os.getenv("TWILIO_NUMBER")
CALENDLY_API_KEY    = os.getenv("CALENDLY_API_KEY")

# --- Startup sanity prints ---------------------------------------------------
print("🧪 SANITY CHECK — cwd:", pathlib.Path().resolve())
print("🧪 SANITY CHECK — OPENAI key present:", bool(OPENAI_API_KEY))
print("🧪 SANITY CHECK — model:", REALTIME_MODEL)
print("🧪 SANITY CHECK — voice:", OPENAI_VOICE)
print("🧪 SANITY CHECK — PUBLIC_URL:", PUBLIC_URL or "(not set — falling back to Host header)")

app = FastAPI()


def to_wss(url: str) -> str:
    """Convert https:// to wss:// (or http:// to ws://)."""
    return url.replace("https://", "wss://").replace("http://", "ws://")


def stream_url_from_request(request: Request) -> str:
    """Build the wss:// URL Twilio should stream to."""
    if PUBLIC_URL:
        return to_wss(f"{PUBLIC_URL.rstrip('/')}/media-stream")
    host = request.headers.get("host", "")
    base = f"https://{host}" if not host.startswith("http") else host
    return to_wss(f"{base}/media-stream")


# ---------------------------------------------------------------------------
# 1) /voice — TwiML endpoint (explicit XML for easy curl/browser testing)
# ---------------------------------------------------------------------------
@app.post("/voice")
async def voice_webhook_post(request: Request):
    stream_url = stream_url_from_request(request)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Connect><Stream url="{stream_url}" track="both_tracks"/></Connect>'
        "</Response>"
    )
    return PlainTextResponse(xml, media_type="application/xml")


# Same TwiML for browser GET (so you don’t get 405 in Safari/Chrome)
@app.get("/voice")
async def voice_webhook_get(request: Request):
    stream_url = stream_url_from_request(request)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Connect><Stream url="{stream_url}" track="both_tracks"/></Connect>'
        "</Response>"
    )
    return PlainTextResponse(xml, media_type="application/xml")


# ---------------------------------------------------------------------------
# 2) /media-stream — WebSocket bridge Twilio <-> OpenAI Realtime (aiohttp)
# ---------------------------------------------------------------------------
def pcm16_to_mulaw(samples: np.ndarray) -> bytes:
    if samples.size == 0:
        return b""
    x = np.clip(samples.astype(np.float32) / 32768.0, -1.0, 1.0)
    mu = 255.0
    y = np.sign(x) * np.log1p(mu * np.abs(x)) / np.log1p(mu
    )
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
    return np.repeat(pcm16_8k, 3).astype(np.int16)  # simple 3x repeat


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


@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("🔗 Twilio stream connected")

    if not OPENAI_API_KEY:
        print("❌ Missing OPENAI_API_KEY")
        await ws.close()
        return

    import aiohttp
    oa_url = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                oa_url,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "OpenAI-Beta": "realtime=v1",
                },
                heartbeat=10,           # keepalive pings
                max_msg_size=8_000_000, # generous frame size
            ) as oa:
                print("✅ Connected to GPT session")

                # Configure session: 8k input (Twilio μ-law -> PCM) → 24k output PCM16
                await oa.send_str(json.dumps({
                    "type": "session.update",
                    "session": {
                        "instructions": SYSTEM_PROMPT,
                        "input_audio_format":  {"type": "wav", "sample_rate_hz": 8000, "channels": 1},
                        "output_audio_format": {"type": "pcm16", "sample_rate_hz": 24000, "channels": 1},
                        "voice": OPENAI_VOICE,
                    }
                }))

                # Immediate greeting
                await oa.send_str(json.dumps({
                    "type": "response.create",
                    "response": {
                        "instructions": "Hi, thanks for calling the dental office. This is Sophie. How can I help you today?",
                        "modalities": ["audio"],
                        "conversation": "none",
                        "audio": {"voice": OPENAI_VOICE},
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

                        # Twilio sends 8k μ-law frames (20ms)
                        ulaw = np.frombuffer(base64.b64decode(payload_b64), dtype=np.uint8)
                        pcm8 = mulaw_to_pcm16(ulaw)
                        if pcm8.size == 0:
                            continue

                        # Barge-in: cancel greeting on first caller audio
                        if not cancelled_once:
                            await oa.send_str(json.dumps({"type": "response.cancel"}))
                            cancelled_once = True

                        # Send 24k PCM16 frames to OpenAI
                        pcm24 = upsample_8k_to_24k(pcm8)
                        await oa.send_str(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm24.tobytes()).decode()
                        }))
                        frames_since_commit += 1

                        # Commit after ≥120ms to avoid “buffer too small” errors
                        if frames_since_commit * TWILIO_FRAME_MS >= MIN_COMMIT_MS:
                            await oa.send_str(json.dumps({"type": "input_audio_buffer.commit"}))
                            print(f"🟢 Committed audio to GPT (~{frames_since_commit * TWILIO_FRAME_MS}ms)")
                            frames_since_commit = 0

                async def openai_to_twilio():
                    nonlocal stream_sid
                    async for msg in oa:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            evt = json.loads(msg.data)

                            if evt.get("type") == "response.audio.delta":
                                # GPT → 24k PCM16 → 8k PCM16 → μ-law → Twilio
                                pcm24 = np.frombuffer(base64.b64decode(evt["audio"]), dtype=np.int16)
                                pcm8  = downsample_24k_to_8k(pcm24)
                                ulaw  = pcm16_to_mulaw(pcm8)
                                if not stream_sid or not ulaw:
                                    continue
                                await ws.send_text(json.dumps({
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": base64.b64encode(ulaw).decode()}
                                }))

                            elif evt.get("type") == "error":
                                print("❌ OpenAI Error:", evt)

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            print("❌ OpenAI WS error frame:", msg)

                await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    except Exception as e:
        print("❌ WS proxy error:", repr(e))


# ---------------------------------------------------------------------------
# 3) Health check
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")
