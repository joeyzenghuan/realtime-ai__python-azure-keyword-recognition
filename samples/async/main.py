import asyncio
import logging
import base64
import os, sys
from typing import Any, Dict

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.abspath(os.path.join(current_dir, '..'))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from utils.audio_playback import AudioPlayer
from utils.audio_capture import AudioCapture, AudioCaptureEventHandler
from realtime_ai.aio.realtime_ai_client import RealtimeAIClient
from realtime_ai.models.realtime_ai_options import RealtimeAIOptions
from realtime_ai.models.audio_stream_options import AudioStreamOptions
from realtime_ai.aio.realtime_ai_event_handler import RealtimeAIEventHandler
from realtime_ai.models.realtime_ai_events import *

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]  # Streaming logs to the console
)

# Specific loggers for mentioned packages
logging.getLogger("utils.audio_playback").setLevel(logging.DEBUG)
logging.getLogger("utils.audio_capture").setLevel(logging.ERROR)
logging.getLogger("utils.vad").setLevel(logging.ERROR)
logging.getLogger("realtime_ai").setLevel(logging.ERROR)
logging.getLogger("websockets.client").setLevel(logging.ERROR)

# Root logger for general logging
logger = logging.getLogger()


class MyAudioCaptureEventHandler(AudioCaptureEventHandler):
    def __init__(self, client: RealtimeAIClient, event_handler: "MyRealtimeEventHandler", event_loop):
        """
        Initializes the event handler.
        
        :param client: Instance of RealtimeClient.
        :param event_handler: Instance of MyRealtimeEventHandler
        :param event_loop: The asyncio event loop.
        """
        self.client = client
        self.event_handler = event_handler
        self.event_loop = event_loop
        self.cancelled = False

    def send_audio_data(self, audio_data: bytes):
        """
        Sends audio data to the RealtimeClient.

        :param audio_data: Raw audio data in bytes.
        """
        logger.info("Sending audio data to the client.")
        asyncio.run_coroutine_threadsafe(self.client.send_audio(audio_data), self.event_loop)

    def on_speech_start(self):
        """
        Handles actions to perform when speech starts.

        """
        logger.info("Speech has started.")
        if self.event_handler.is_audio_playing():
            logger.info(f"User started speaking while audio is playing.")

            logger.info("Clearing input audio buffer.")
            asyncio.run_coroutine_threadsafe(self.client.clear_input_audio_buffer(), self.event_loop)

            logger.info("Cancelling response.")
            asyncio.run_coroutine_threadsafe(self.client.cancel_response(), self.event_loop)
            self.cancelled = True

            current_item_id = self.event_handler.get_current_conversation_item_id()
            current_audio_content_index = self.event_handler.get_current_audio_content_id()
            logger.info(f"Truncate the current audio, current item ID: {current_item_id}, current audio content index: {current_audio_content_index}")
            asyncio.run_coroutine_threadsafe(self.client.truncate_response(item_id=current_item_id, content_index=current_audio_content_index, audio_end_ms=1000), self.event_loop)

            # Restart the audio player
            self.event_handler.audio_player.drain_and_restart(clear_buffer=True)
        else:
            logger.info("Assistant is not speaking, cancelling response is not required.")
            self.cancelled = False

    def on_speech_end(self):
        """
        Handles actions to perform when speech ends.
        """
        logger.info("Speech has ended")
        logger.info("Requesting the client to generate a response.")
        asyncio.run_coroutine_threadsafe(self.client.generate_response(), self.event_loop)


class MyRealtimeEventHandler(RealtimeAIEventHandler):
    def __init__(self, audio_player: AudioPlayer):
        super().__init__()
        self._audio_player = audio_player
        self.lock = asyncio.Lock()
        self.audio_buffer = []
        self.client = None
        self.current_item_id = None
        self.current_audio_content_index = None
        self._is_audio_playing = False

    @property
    def audio_player(self):
        return self._audio_player

    def get_current_conversation_item_id(self):
        return self.current_item_id
    
    def get_current_audio_content_id(self):
        return self.current_audio_content_index
    
    def is_audio_playing(self):
        return self._is_audio_playing
    
    def set_client(self, client: RealtimeAIClient):
        self.client = client
        logger.info("RealtimeAIClient has been set in MyRealtimeEventHandler.")

    async def on_error(self, event: ErrorEvent) -> None:
        logger.error(f"Error occurred: {event.error.message}")

    async def on_input_audio_buffer_speech_stopped(self, event: InputAudioBufferSpeechStopped) -> None:
        logger.info(f"Speech stopped at {event.audio_end_ms}ms, Item ID: {event.item_id}")

    async def on_input_audio_buffer_committed(self, event: InputAudioBufferCommitted) -> None:
        logger.info(f"Audio Buffer Committed: {event.item_id}")

    async def on_conversation_item_created(self, event: ConversationItemCreated) -> None:
        logger.info(f"New Conversation Item: {event.item}")

    async def on_response_created(self, event: ResponseCreated) -> None:
        logger.info(f"Response Created: {event.response}")

    async def on_response_content_part_added(self, event: ResponseContentPartAdded) -> None:
        logger.info(f"New Part Added: {event.part}")

    async def on_response_audio_delta(self, event: ResponseAudioDelta) -> None:
        logger.info(f"Received audio delta for Response ID {event.response_id}, Item ID {event.item_id}, Content Index {event.content_index}")
        self.current_item_id = event.item_id
        self.current_audio_content_index = event.content_index
        self.handle_audio_delta(event)

    async def on_response_audio_transcript_delta(self, event: ResponseAudioTranscriptDelta) -> None:
        logger.info(f"Transcript Delta: {event.delta}")

    async def on_rate_limits_updated(self, event: RateLimitsUpdated) -> None:
        for rate in event.rate_limits:
            logger.info(f"Rate Limit: {rate.name}, Remaining: {rate.remaining}")

    async def on_conversation_item_input_audio_transcription_completed(self, event: ConversationItemInputAudioTranscriptionCompleted) -> None:
        logger.info(f"Transcription completed for item {event.item_id}: {event.transcript}")

    async def on_response_audio_done(self, event: ResponseAudioDone) -> None:
        logger.info(f"Audio done for response ID {event.response_id}, item ID {event.item_id}")
        if self.client:
            # Clear the input audio buffer after audio is done
            #logger.info("Clearing input audio buffer.")
            #await self.client.clear_input_audio_buffer()
            # Update audio playback status
            self._is_audio_playing = False
        self._audio_player.drain_and_restart()

    async def on_response_audio_transcript_done(self, event: ResponseAudioTranscriptDone) -> None:
        logger.info(f"Audio transcript done: '{event.transcript}' for response ID {event.response_id}")

    async def on_response_content_part_done(self, event: ResponseContentPartDone) -> None:
        part_type = event.part.get("type")
        part_text = event.part.get("text", "")
        logger.info(f"Content part done: '{part_text}' of type '{part_type}' for response ID {event.response_id}")

    async def on_response_output_item_done(self, event: ResponseOutputItemDone) -> None:
        item_content = event.item.get("content", [])
        logger.info(f"Output item done for response ID {event.response_id} with content: {item_content}")

    async def on_response_done(self, event: ResponseDone) -> None:
        logger.info(f"Response completed with status '{event.response.get('status')}' and ID '{event.response.get('id')}'")
        # Update audio playback status
        # self._is_audio_playing = False

    async def on_session_created(self, event: SessionCreated) -> None:
        logger.info(f"Session created: {event.session}")

    async def on_session_updated(self, event: SessionUpdated) -> None:
        logger.info(f"Session updated: {event.session}")

    async def on_input_audio_buffer_speech_started(self, event: InputAudioBufferSpeechStarted) -> None:
        logger.info(f"Speech started at {event.audio_start_ms}ms for item ID {event.item_id}")
        
        #if self._is_audio_playing:
        #    logger.info("Clearing input audio buffer.")
        #    await self.client.clear_input_audio_buffer()

        #    logger.info("Audio playback is ongoing. Sending response cancel event to client.")
        #    await self.client.cancel_response()

    async def on_response_output_item_added(self, event: ResponseOutputItemAdded) -> None:
        logger.info(f"Output item added for response ID {event.response_id} with item: {event.item}")

    async def on_response_function_call_arguments_delta(self, event: ResponseFunctionCallArgumentsDelta) -> None:
        logger.info(f"Function call arguments delta for call ID {event.call_id}: {event.delta}")

    async def on_response_function_call_arguments_done(self, event: ResponseFunctionCallArgumentsDone) -> None:
        logger.info(f"Function call arguments done for call ID {event.call_id} with arguments: {event.arguments}")

    async def on_unhandled_event(self, event_type: str, event_data: Dict[str, Any]) -> None:
        logger.warning(f"Unhandled Event: {event_type} - {event_data}")

    def handle_audio_delta(self, event: ResponseAudioDelta):
        delta_audio = event.delta
        if delta_audio:
            try:
                audio_bytes = base64.b64decode(delta_audio)
                self._audio_player.enqueue_audio_data(audio_bytes)
                self._is_audio_playing = True
            except base64.binascii.Error as e:
                logger.error(f"Failed to decode audio delta: {e}")
        else:
            logger.warning("Received 'ResponseAudioDelta' event without 'delta' field.")


async def main():
    """
    Main function to initialize and run the audio processing and realtime client asynchronously.
    """
    client = None
    audio_player = None
    audio_capture = None

    try:
        # Retrieve OpenAI API key from environment variables
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.error("OpenAI API key not found. Please set the OPENAI_API_KEY environment variable.")
            return

        # Define RealtimeOptions
        options = RealtimeAIOptions(
            api_key=api_key,
            model="gpt-4o-realtime-preview-2024-10-01",
            modalities=["audio", "text"],
            instructions="You are a helpful assistant. Respond concisely. If user asks to tell story, tell story very shortly.",
            turn_detection=None,
            tools=[
                {
                    "type": "function",
                    "name": "get_weather",
                    "description": "Get the current weather for a location.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {"type": "string"}
                        },
                        "required": ["location"]
                    }
                }
            ],
            tool_choice="auto",
            temperature=0.8,
            max_output_tokens=None
        )

        # Define AudioStreamOptions
        stream_options = AudioStreamOptions(
            sample_rate=24000,
            channels=1,
            bytes_per_sample=2
        )

        # Initialize AudioPlayer
        audio_player = AudioPlayer()

        # Initialize RealtimeAIClient with MyRealtimeEventHandler to handle events
        event_handler = MyRealtimeEventHandler(audio_player=audio_player)
        client = RealtimeAIClient(options, stream_options, event_handler)
        event_handler.set_client(client)
        await client.start()

        loop = asyncio.get_running_loop()  # Get the current event loop
        
        audio_capture_event_handler = MyAudioCaptureEventHandler(
            client=client,
            event_handler=event_handler,
            event_loop=loop,
        )

        # Initialize AudioCapture with the event handler
        audio_capture = AudioCapture(
            event_handler=audio_capture_event_handler,
            sample_rate=24000,
            channels=1,
            frames_per_buffer=1024,
            buffer_duration_sec=1.0,
            cross_fade_duration_ms=20,
            vad_parameters={
                "sample_rate": 24000,
                "chunk_size": 1024,
                "window_duration": 1.0,
                "silence_ratio": 1.5,
                "min_speech_duration": 0.3,
                "min_silence_duration": 0.3
            }
        )

        logger.info("Recording... Press Ctrl+C to stop.")

        # Keep the loop running while the stream is active
        await asyncio.Event().wait()  # Effectively blocks indefinitely

    except KeyboardInterrupt:
        logger.info("Recording stopped by user.")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        if audio_player:
            audio_player.close()

        if audio_capture:
            audio_capture.close()

        if client:
            try:
                logger.info("Stopping client...")
                await client.stop()
            except Exception as e:
                logger.error(f"Error during client shutdown: {e}")


if __name__ == "__main__":
    asyncio.run(main())
