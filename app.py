import json
import modal
from loguru import logger


MAX_SESSION_TIME = 15 * 60
app = modal.App("twilio-voice-bot-dev")


# Create Modal image with all dependencies and include bot.py
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_requirements("requirements.txt")
    .add_local_file("requirements.txt", "/root/requirements.txt")
    .add_local_file("bot.py", "/root/bot.py")
    .add_local_file("templates/streams.xml", "/root/templates/streams.xml")
    .add_local_file("audio_s3.py", "/root/audio_s3.py")
)


@app.function(
    image=image,
    cpu=1.0,
    memory=512,
    secrets=[modal.Secret.from_dotenv()],
    min_containers=1,  # Updated from keep_warm
    max_inputs=1,  # Do not reuse instances across requests
    buffer_containers=1,
    enable_memory_snapshot=False,
)
@modal.asgi_app()
def endpoint():
    from fastapi import FastAPI, WebSocket
    from fastapi.responses import HTMLResponse
    from fastapi.middleware.cors import CORSMiddleware
    from bot import run_bot

    web_app = FastAPI()
    web_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Allows all origins
        allow_credentials=True,
        allow_methods=["*"],  # Allows all methods
        allow_headers=["*"],  # Allows all headers
    )

    @web_app.post("/")
    async def start_call():
        logger.info("POST TwiML received")
        # Read the TwiML template
        with open("/root/templates/streams.xml") as f:
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
