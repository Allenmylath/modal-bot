import os
import json
import modal
from loguru import logger

app = modal.App("twilio-voice-bot")

# Create Modal image with all dependencies
image = modal.Image.debian_slim(python_version="3.12").pip_install_from_requirements(
    "requirements.txt"
)

@app.function(
    image=image,
    cpu=1.0,
    memory=1024,
    secrets=[modal.Secret.from_dotenv()],
    keep_warm=1,
)
@modal.asgi_app()
def endpoint():
    from fastapi import FastAPI, WebSocket
    from fastapi.responses import HTMLResponse
    from bot import run_bot
    
    web_app = FastAPI()
    
    @web_app.post("/")
    async def start_call():
        logger.info("POST TwiML received")
        # Read the TwiML template
        with open("templates/streams.xml") as f:
            twiml_content = f.read()
        return HTMLResponse(content=twiml_content, media_type="application/xml")
    
    @web_app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        logger.info("WebSocket connection accepted")
        
        # Get initial data from Twilio
        start_data = websocket.iter_text()
        await start_data.__anext__()  # Skip first message
        call_data = json.loads(await start_data.__anext__())
        
        stream_sid = call_data["start"]["streamSid"]
        logger.info(f"Stream SID: {stream_sid}")
        
        # Run the bot
        await run_bot(websocket, stream_sid)
    
    return web_app
