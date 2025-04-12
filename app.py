#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import os
import sys
from dotenv import load_dotenv
from fastapi import WebSocket
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.cartesia import CartesiaTTSService
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.services.deepgram import DeepgramSTTService
from pipecat.services.cerebras import CerebrasLLMService
from pipecat.transports.network.fastapi_websocket import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from audio_s3 import save_audio_to_s3

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

# Global variables to store pre-initialized components
_llm = None
_stt = None
_tts = None
_context = None
_messages = None
_audiobuffer = None
_runner = None


async def prepare_bot_components():
    """Pre-initialize all bot components that don't require an active websocket"""
    global _llm, _stt, _tts, _context, _messages, _audiobuffer, _runner

    logger.info("Pre-initializing bot components")

    # Initialize LLM service
    _llm = CerebrasLLMService(
        api_key=os.getenv("CEREBRAS_API_KEY"),
        model="llama-4-scout-17b-16e-instruct",
    )

    # Initialize STT service
    _stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"), audio_passthrough=True
    )

    # Initialize TTS service
    _tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id="156fb8d2-335b-4950-9cb3-a2d33befec77",
        params=CartesiaTTSService.InputParams(
            speed="normal",
            emotion=[
                "positivity",
                "curiosity",
            ],
        ),
    )

    # Initialize context
    _messages = [
        {
            "role": "system",
            "content": """
        YOU ARE JESSICA. your output is connected to audio. so try to punctuate the sentences so that tts model can give emotions in voice. try to speak as if a human would speak.
        Use appropriate punctuation. Use short sentences. Dont be chatty. Add punctuation where appropriate and at the end of each transcript whenever possible.
        Use dates in MM/DD/YYYY form.
        To insert pauses, insert "-". dont use ... for pauses.
        """,
        },
    ]

    _context = OpenAILLMContext(_messages)

    # Initialize audio buffer
    _audiobuffer = AudioBufferProcessor()

    # Initialize the runner
    _runner = PipelineRunner(handle_sigint=True, force_gc=True)

    logger.info("Bot components pre-initialized successfully")


async def connect_bot_to_websocket(websocket_client: WebSocket, stream_sid: str):
    """Connect the pre-initialized bot components to the provided websocket"""
    global _llm, _stt, _tts, _context, _messages, _audiobuffer, _runner

    # If components haven't been pre-initialized, do it now
    if _llm is None:
        await prepare_bot_components()

    logger.info(f"Connecting bot to WebSocket with stream SID: {stream_sid}")

    # Create transport with the active websocket
    transport = FastAPIWebsocketTransport(
        websocket=websocket_client,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            vad_audio_passthrough=True,
            serializer=TwilioFrameSerializer(stream_sid),
        ),
    )

    # Create context aggregator
    context_aggregator = _llm.create_context_aggregator(_context)

    # Create the pipeline with all components
    pipeline = Pipeline(
        [
            transport.input(),  # Websocket input from client
            _stt,  # Speech-To-Text
            context_aggregator.user(),
            _llm,  # LLM
            _tts,  # Text-To-Speech
            transport.output(),  # Websocket output to client
            _audiobuffer,
            context_aggregator.assistant(),
        ]
    )

    # Create the pipeline task
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
        ),
    )

    # Set up event handlers
    @_audiobuffer.event_handler("on_audio_data")
    async def on_audio_data(buffer, audio, sample_rate, num_channels):
        try:
            print("starting upload")
            s3_url = await save_audio_to_s3(
                audio=audio,
                sample_rate=sample_rate,
                num_channels=num_channels,
                bucket_name="careadhdaudio",
            )
            logger.info(
                f"Successfully saved {len(audio)} bytes of audio to S3.{s3_url}"
            )
        except Exception as e:
            logger.error(f"Error saving audio to S3: {e}")

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        await _audiobuffer.start_recording()
        print("Recording started")

        # Kick off the conversation.
        _messages.append(
            {
                "role": "system",
                "content": "Please introduce yourself as jessica to the user and ask how you can help them.",
            }
        )
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        await _audiobuffer.stop_recording()
        await task.cancel()

    # Run the pipeline task
    await _runner.run(task)


# For backward compatibility
async def run_bot(websocket_client: WebSocket, stream_sid: str):
    """Legacy function for backward compatibility"""
    await connect_bot_to_websocket(websocket_client, stream_sid)
