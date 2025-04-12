import json
import modal
from loguru import logger
import asyncio

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
    from fastapi import FastAPI, WebSocket, Request
    from fastapi.responses import HTMLResponse
    from fastapi.middleware.cors import CORSMiddleware
    from bot import prepare_bot_components

    web_app = FastAPI()
    web_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Allows all origins
        allow_credentials=True,
        allow_methods=["*"],  # Allows all methods
        allow_headers=["*"],  # Allows all headers
    )

    # Modified bot.py will need to have prepare_bot_components function
    # that pre-initializes all the components without requiring a websocket

    @web_app.post("/")
    async def start_call(request: Request):
        logger.info("POST TwiML received - Starting to prepare bot components")

        # Read the TwiML template
        with open("/root/templates/streams.xml") as f:
            twiml_content = f.read()

        # Pre-initialize bot components in the background
        asyncio.create_task(prepare_bot_components())

        return HTMLResponse(content=twiml_content, media_type="application/xml")

    @web_app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        from bot import connect_bot_to_websocket

        await websocket.accept()
        logger.info("WebSocket connection accepted")

        # Get initial data from Twilio
        start_data = websocket.iter_text()
        await start_data.__anext__()  # Skip first message
        call_data = json.loads(await start_data.__anext__())

        stream_sid = call_data["start"]["streamSid"]
        logger.info(f"Stream SID: {stream_sid}")

        # Connect the pre-initialized bot to the websocket
        await connect_bot_to_websocket(websocket, stream_sid)

    return web_app
