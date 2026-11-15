"""One phone call: Exotel Voicebot WebSocket ⇄ ADK live (Gemini Live) session.

Per call (see ``run_call``):

1. Wait for Exotel's ``start``; take stream_sid, call SID and the caller number
   (normalised to the digits form ``patients``/``transcripts`` use).
2. Seed the model like ``run_turn`` does: ``contextualize`` (today's date, the
   caller's number, prior ``transcripts`` history incl. tool results) plus a cue
   so the model greets first. The live session is FRESH (random session id):
   nothing relies on ADK session state, exactly like the text harness.
3. Two tasks, until either side ends:
   * upstream — Exotel ``media`` → base64 decode → resample to 16 kHz →
     ``LiveRequestQueue.send_realtime``. ``stop``/disconnect ends the call.
   * downstream — live events: audio → resample to the Exotel rate → 320-byte-
     multiple chunks → Exotel ``media``; ``interrupted`` (barge-in) → drop the
     local buffer + Exotel ``clear``; finished transcriptions → user/assistant
     turns; tool calls captured with the same recorder as ``run_turn``.
4. Whatever happens, persist what we have: the call's turns are appended to
   ``transcripts`` (so a later chat or call remembers it) and written as ONE
   ``conversations`` doc (channel ``voice``), finalized and analytics-eligible.

A failure in one call is logged and contained; it never propagates to the app.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import InMemoryRunner, Runner
from google.genai import types
from starlette.websockets import WebSocketDisconnect

from clinic_agent.config import settings
from clinic_agent.phone import normalize_phone
from clinic_agent.runtime import conversations
from clinic_agent.runtime.agent import build_agent
from clinic_agent.runtime.history import append_turn, get_history
from clinic_agent.runtime.tool_capture import ToolCallRecorder
from clinic_agent.runtime.turn import contextualize
from clinic_agent.voice import audio, protocol

log = logging.getLogger("clinic_agent.voice.bridge")

APP_NAME = "clinic-ai-agent-voice"
GREETING_CUE = "The caller has just connected. Greet them as the clinic receptionist."
MODEL_INPUT_MIME = f"audio/pcm;rate={audio.MODEL_INPUT_RATE}"
# After a hangup, wait this long for an in-flight tool call (e.g. a booking) to
# return so its result is recorded.
TOOL_GRACE_SECONDS = 5.0

_runner: Runner | None = None


def get_runner() -> Runner:
    """Module-level voice runner, built on first call (agent tree is static)."""
    global _runner
    if _runner is None:
        _runner = InMemoryRunner(agent=build_agent("voice"), app_name=APP_NAME)
    return _runner


def build_run_config() -> RunConfig:
    speech = None
    if settings.VOICE_NAME:
        speech = types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.VOICE_NAME)
            )
        )
    return RunConfig(
        streaming_mode=StreamingMode.BIDI,
        response_modalities=[types.Modality.AUDIO],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        speech_config=speech,
        # Audio-only Live sessions cap at ~15 min without compression; the
        # connection itself is recycled (goAway) and ADK reconnects with the
        # resumption handle.
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()
        ),
        session_resumption=types.SessionResumptionConfig(),
    )


class CallSocket(Protocol):
    """The slice of Starlette's ``WebSocket`` the bridge uses."""

    async def receive_text(self) -> str: ...
    async def send_text(self, data: str) -> None: ...
    async def close(self, code: int = 1000) -> None: ...


class CallTranscript:
    """Groups a call's finished transcriptions + tool calls into turns.

    A turn is ``{user, assistant, tools, ts}``. Caller speech after the agent
    has spoken starts a new turn; the agent's opening greeting gets the
    ``CALL_STARTED`` marker as its user text.
    """

    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def _new(self, user: str) -> dict[str, Any]:
        turn = {"user": user, "assistant": "", "tools": [], "ts": datetime.now(UTC)}
        self.turns.append(turn)
        return turn

    def _current(self) -> dict[str, Any]:
        return self.turns[-1] if self.turns else self._new(conversations.CALL_STARTED)

    def user(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if not self.turns or self.turns[-1]["assistant"]:
            self._new(text)
            return
        cur = self.turns[-1]
        cur["user"] = text if cur["user"] == conversations.CALL_STARTED else f"{cur['user']} {text}"

    def assistant(self, text: str) -> None:
        text = text.strip()
        if text:
            cur = self._current()
            cur["assistant"] = f"{cur['assistant']} {text}".strip()

    def tool(self, call: dict) -> None:
        self._current()["tools"].append(call)


class CallBridge:
    def __init__(
        self,
        ws: CallSocket,
        info: protocol.CallInfo,
        *,
        sample_rate: int,
        runner: Runner,
        run_config: RunConfig,
    ) -> None:
        self.ws = ws
        self.info = info
        self.rate = sample_rate
        self.runner = runner
        self.run_config = run_config
        self.phone = normalize_phone(info.caller)
        self.user_id = self.phone or info.call_sid or "unknown"
        self.session_id = uuid.uuid4().hex
        self.queue = LiveRequestQueue()
        self.recorder = ToolCallRecorder()
        self.transcript = CallTranscript()
        self.chunker = audio.OutboundChunker(audio.chunk_size_for(sample_rate))
        self.in_resampler = audio.StreamResampler(sample_rate, audio.MODEL_INPUT_RATE)
        self.out_resampler = audio.StreamResampler(audio.MODEL_OUTPUT_RATE, sample_rate)
        self.started_at = datetime.now(UTC)
        self.conv_id: str | None = None
        self.stop_reason = ""

    # ---- lifecycle --------------------------------------------------------

    async def run(self) -> None:
        log.info(
            "Call %s from %s started (stream %s, %d Hz)",
            self.info.call_sid,
            self.phone or "<unknown>",
            self.info.stream_sid,
            self.rate,
        )
        try:
            try:
                self.conv_id = await conversations.start_call(
                    self.phone, self.info.call_sid, self.started_at
                )
            except Exception:
                log.exception("Could not open conversation for call %s", self.info.call_sid)

            history = await get_history(self.phone) if self.phone else []
            await self.runner.session_service.create_session(
                app_name=self.runner.app_name, user_id=self.user_id, session_id=self.session_id
            )
            seeded = contextualize(GREETING_CUE, history, self.phone, channel="voice")
            self.queue.send_content(types.Content(role="user", parts=[types.Part(text=seeded)]))
            await self._pump()
        except Exception:
            log.exception("Call %s failed", self.info.call_sid)
        finally:
            self.queue.close()
            with contextlib.suppress(Exception):
                await self.ws.close()
            await self._persist()
            with contextlib.suppress(Exception):
                await self.runner.session_service.delete_session(
                    app_name=self.runner.app_name,
                    user_id=self.user_id,
                    session_id=self.session_id,
                )
            log.info(
                "Call %s ended (%s): %d turns, %d tool calls",
                self.info.call_sid,
                self.stop_reason or "session ended",
                len(self.transcript.turns),
                len(self.recorder.calls),
            )

    async def _pump(self) -> None:
        up = asyncio.create_task(self._upstream(), name="voice-upstream")
        down = asyncio.create_task(self._downstream(), name="voice-downstream")
        try:
            done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            if up in done and not down.done():
                # Caller hung up. Let an in-flight tool call finish so a booking
                # that actually happened is recorded.
                deadline = time.monotonic() + TOOL_GRACE_SECONDS
                while self.recorder.has_pending and not down.done() and time.monotonic() < deadline:  # noqa: ASYNC110 (bounded poll of recorder state)
                    await asyncio.sleep(0.05)
        finally:
            for task in (up, down):
                if not task.done():
                    task.cancel()
            for task in (up, down):
                with contextlib.suppress(asyncio.CancelledError):
                    try:
                        await task
                    except Exception:
                        log.exception("%s failed (call %s)", task.get_name(), self.info.call_sid)

    # ---- Exotel → model ---------------------------------------------------

    async def _upstream(self) -> None:
        while True:
            try:
                text = await self.ws.receive_text()
            except WebSocketDisconnect:
                self.stop_reason = "disconnected"
                return
            msg = protocol.parse_message(text)
            if msg is None:
                continue
            event = msg["event"]
            if event == "media":
                pcm = self.in_resampler.process(audio.b64decode(protocol.media_payload(msg)))
                if pcm:
                    self.queue.send_realtime(types.Blob(data=pcm, mime_type=MODEL_INPUT_MIME))
            elif event == "stop":
                self.stop_reason = str((msg.get("stop") or {}).get("reason") or "stop")
                return
            else:  # dtmf / mark / connected / start repeats: not used yet
                log.debug("Exotel %s event on call %s: %s", event, self.info.call_sid, msg)

    # ---- model → Exotel ---------------------------------------------------

    async def _downstream(self) -> None:
        events = self.runner.run_live(
            user_id=self.user_id,
            session_id=self.session_id,
            live_request_queue=self.queue,
            run_config=self.run_config,
        )
        try:
            async for event in events:
                await self._on_event(event)
        finally:
            with contextlib.suppress(Exception):
                await events.aclose()
        log.info("Live session ended on its own (call %s)", self.info.call_sid)

    async def _send_media(self, chunks: list[bytes]) -> None:
        for chunk in chunks:
            await self.ws.send_text(
                protocol.media_event(self.info.stream_sid, audio.b64encode(chunk))
            )

    async def _on_event(self, event: Any) -> None:
        if getattr(event, "interrupted", None):
            # Barge-in: drop audio we haven't sent and tell Exotel to drop what
            # it has queued but not yet played.
            self.chunker.clear()
            self.out_resampler.reset()
            await self.ws.send_text(protocol.clear_event(self.info.stream_sid))

        for call in self.recorder.observe(event):
            self.transcript.tool(call)

        content = getattr(event, "content", None)
        for part in getattr(content, "parts", None) or []:
            blob = getattr(part, "inline_data", None)
            if not blob or not blob.data or not (blob.mime_type or "").startswith("audio/"):
                continue
            src_rate = audio.rate_from_mime(blob.mime_type)
            if src_rate != self.out_resampler.in_rate:
                self.out_resampler = audio.StreamResampler(src_rate, self.rate)
            await self._send_media(self.chunker.push(self.out_resampler.process(blob.data)))

        it = getattr(event, "input_transcription", None)
        if it and it.finished and it.text:
            self.transcript.user(it.text)
        ot = getattr(event, "output_transcription", None)
        if ot and ot.finished and ot.text:
            self.transcript.assistant(ot.text)

        if getattr(event, "turn_complete", None):
            await self._send_media(self.chunker.flush())

    # ---- persistence ------------------------------------------------------

    async def _persist(self) -> None:
        turns = self.transcript.turns
        ended_at = datetime.now(UTC)
        if self.phone:
            for turn in turns:
                try:
                    await append_turn(
                        self.phone, turn["user"], turn["assistant"], turn["tools"]
                    )
                except Exception:
                    log.exception("transcripts write failed (call %s)", self.info.call_sid)
                    break
        try:
            if self.conv_id is None:
                self.conv_id = await conversations.start_call(
                    self.phone, self.info.call_sid, self.started_at
                )
            await conversations.finish_call(
                self.conv_id, self.phone, turns, self.started_at, ended_at
            )
        except Exception:
            log.exception("conversations write failed (call %s)", self.info.call_sid)


async def wait_for_start(ws: CallSocket) -> protocol.CallInfo | None:
    """Read frames until Exotel's ``start``; ``None`` if the socket ends first."""
    while True:
        try:
            text = await ws.receive_text()
        except WebSocketDisconnect:
            return None
        msg = protocol.parse_message(text)
        if msg is None:
            continue
        if msg["event"] == "start":
            log.debug("Exotel start payload: %s", msg)
            return protocol.parse_start(msg)
        if msg["event"] == "stop":
            return None
        log.debug("Exotel %s before start (ignored)", msg["event"])


async def run_call(
    ws: CallSocket,
    *,
    sample_rate: int | None = None,
    runner: Runner | None = None,
    run_config: RunConfig | None = None,
) -> None:
    """Handle one accepted Exotel Voicebot socket end to end. Never raises."""
    try:
        info = await wait_for_start(ws)
        if info is None:
            log.info("Exotel socket closed before start")
            return
        rate = sample_rate or settings.VOICE_SAMPLE_RATE
        if info.sample_rate and info.sample_rate != rate:
            log.warning(
                "Exotel media_format.sample_rate=%s but we requested %s; using %s",
                info.sample_rate,
                rate,
                rate,
            )
        bridge = CallBridge(
            ws,
            info,
            sample_rate=rate,
            runner=runner or get_runner(),
            run_config=run_config or build_run_config(),
        )
        await bridge.run()
    except Exception:
        log.exception("Voice call handler crashed")
        with contextlib.suppress(Exception):
            await ws.close()
