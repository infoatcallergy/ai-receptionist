from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse
import os
from dotenv import load_dotenv
from openai import OpenAI  # ✅ moved up here

# Load environment variables (API keys)
load_dotenv()

# Initialize FastAPI app
app = FastAPI()

# Set up OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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

    # 🧾 Log the conversation to a text file
    os.makedirs("logs", exist_ok=True)
    with open("logs/conversation_log.txt", "a", encoding="utf-8") as log:
        log.write(f"User: {Body}\n")
        log.write(f"AI: {reply}\n\n")

    # Build Twilio reply
    resp = MessagingResponse()
    resp.message(reply)
    return PlainTextResponse(str(resp), media_type="application/xml")


