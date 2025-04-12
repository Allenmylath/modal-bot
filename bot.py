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
from pipecat.services.openai import OpenAILLMService
from pipecat.services.cerebras import CerebrasLLMService
from pipecat.transports.network.fastapi_websocket import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from audio_s3 import save_audio_to_s3

load_dotenv(override=True)


logger.remove(0)
logger.add(sys.stderr, level="DEBUG")


async def run_bot(
    websocket_client: WebSocket,
    stream_sid: str,
):
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

    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o-mini")

    llm = CerebrasLLMService(
        # api_key=os.getenv("CEREBRAS_API_KEY"), model="llama-3.3-70b"
        api_key=os.getenv("CEREBRAS_API_KEY"),
        model="llama-4-scout-17b-16e-instruct",
    )

    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"), audio_passthrough=True
    )

    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id="156fb8d2-335b-4950-9cb3-a2d33befec77",
        params=CartesiaTTSService.InputParams(
            speed="normal",
            emotion=[
                "positivity",
                "curiosity",
            ],  # Adding high positivity and moderate curiosity
        ),
    )
    text = """
    YOU ARE JESSICA. your output is connected to audio. so try to punctuate the sentences so that tts model can give emotions in voice. try to speak as if a human would speak.
Use appropriate punctuation. Use short sentences.Dont be chatty.Add punctuation where appropriate and at the end of each transcript whenever possible.
Use dates in MM/DD/YYYY form.
To insert pauses, insert “-”. dont use ... for pauses.
"""

    messages = [
        {"role": "system", "content": text},
    ]

    context = OpenAILLMContext(messages)
    context_aggregator = llm.create_context_aggregator(context)
    audiobuffer = AudioBufferProcessor()

    pipeline = Pipeline(
        [
            transport.input(),  # Websocket input from client
            stt,  # Speech-To-Text
            context_aggregator.user(),
            llm,  # LLM
            tts,  # Text-To-Speech
            transport.output(),  # Websocket output to client
            audiobuffer,
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            # audio_in_sample_rate=8000,
            # audio_out_sample_rate=8000,
            allow_interruptions=True,
        ),
    )

    @audiobuffer.event_handler("on_audio_data")
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
        await audiobuffer.start_recording()
        print("Recording started")

        # Kick off the conversation.
        messages.append(
            {
                "role": "system",
                "content": "Please introduce yourself as jessica to the user and ask how you can help them.",
            }
        )
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        await audiobuffer.stop_recording()
        await task.cancel()

    # We use `handle_sigint=False` because `uvicorn` is controlling keyboard
    # interruptions. We use `force_gc=True` to force garbage collection after
    # the runner finishes running a task which could be useful for long running
    # applications with multiple clients connecting.
    runner = PipelineRunner(handle_sigint=True, force_gc=True)

    await runner.run(task)
