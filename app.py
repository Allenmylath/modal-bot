import json
import modal
from loguru import logger

MAX_SESSION_TIME = 15 * 60
app = modal.App("twilio-voice-bot")

# Create Modal image with all dependencies and include bot.py
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_requirements("requirements.txt")
    .add_local_file("requirements.txt", "/root/requirements.txt")
    .add_local_file("bot.py", "/root/bot.py")
    .add_local_file("templates/streams.xml", "/root/templates/streams.xml")
    .add_local_file("audio_s3.py", "/root/audio_s3.py")
)


# First function for handling TwiML - this is a lightweight function
@app.function(
    image=image,
    cpu=0.5,  # Lower CPU for this simple function
    memory=256,  # Less memory needed
    secrets=[modal.Secret.from_dotenv()],
    min_containers=0,
    enable_memory_snapshot=False,
)
@modal.asgi_app()
def twiml_endpoint():
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from fastapi.middleware.cors import CORSMiddleware

    web_app = FastAPI()
    web_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @web_app.post("/twiml")
    async def start_call():
        logger.info("POST TwiML received")
        # Read the TwiML template
        with open("/root/templates/streams.xml") as f:
            twiml_content = f.read()
        return HTMLResponse(content=twiml_content, media_type="application/xml")

    return web_app


# Second function for handling WebSocket connections - more resource-intensive
@app.function(
    image=image,
    cpu=0.5,
    memory=512,
    secrets=[modal.Secret.from_dotenv()],
    min_containers=1,
    max_inputs=1,  # Don't reuse instances across requests
    buffer_containers=1,
    enable_memory_snapshot=False,
)
@modal.asgi_app()
def websocket_endpoint():
    from fastapi import FastAPI, WebSocket
    from fastapi.middleware.cors import CORSMiddleware
    from bot import run_bot

    web_app = FastAPI()
    web_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @web_app.websocket("/ws-handler")
    async def websocket_handler(websocket: WebSocket):
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


# You'll need to update your streams.xml to point to the new WebSocket endpoint
# The URL in the <Stream> tag should be: wss://your-modal-app-url.modal.run/websocket-endpoint/ws-handler
