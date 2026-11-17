"""One browser voice call: the call page's WebSocket ⇄ ADK live (Gemini Live) session.

Per call (see ``run_call``; wire format in ``protocol``):

1. Wait for the page's ``start``: call id, the caller's number as typed, and
   whether it wants transcript frames. An empty or invalid number ends the call
   (``end`` / ``invalid_caller``) before any live session or conversation exists.
   The number is normalised to the digits form ``patients``/``transcripts`` use.
2. Seed the model like ``run_turn`` does: ``contextualize`` (today's date, the
   caller's number, prior ``transcripts`` history incl. tool results) plus a cue
   so the model greets first. The live session is FRESH (random session id):
   nothing relies on ADK session state, exactly like the text harness.
3. Two tasks, until either side ends:
   * upstream — page ``media`` (16 kHz PCM, the model's input rate) → base64
     decode → ``LiveRequestQueue.send_realtime``. ``stop``/disconnect ends the call.
   * downstream — live events. The first one proves the session is up, so the
     bridge sends ``ready`` with the model's output rate. Audio → ~100 ms chunks →
     ``media`` (no resampling: the page plays the native rate); ``interrupted``
     (barge-in) → drop the local buffer + ``clear``; transcriptions → ``transcript``
     frames (if requested) and, once finished, user/assistant turns; tool calls
     captured with the same recorder as ``run_turn``.
   If the live session ends or fails, the page gets ``end`` with a reason.
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
from typing import Any, Literal, Protocol

from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import InMemoryRunner, Runner
from google.genai import types
from starlette.websockets import WebSocketDisconnect

from clinic_agent.config import settings
from clinic_agent.phone import is_valid_phone, normalize_phone
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
# The page sends ``start`` right after connecting; give up on a silent socket.
START_TIMEOUT_SECONDS = 10.0

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
        start: protocol.CallStart,
        *,
        runner: Runner,
        run_config: RunConfig,
    ) -> None:
        self.ws = ws
        self.call_id = start.call_id
        self.send_transcripts = start.transcripts
        self.runner = runner
        self.run_config = run_config
        self.phone = normalize_phone(start.caller)
        self.user_id = self.phone or self.call_id
        self.session_id = uuid.uuid4().hex
        self.queue = LiveRequestQueue()
        self.recorder = ToolCallRecorder()
        self.transcript = CallTranscript()
        # Set from the first live event (see ``_send_ready``).
        self.output_rate: int | None = None
        self.chunker: audio.OutboundChunker | None = None
        # Partial transcription text so far, per role (ADK sends partials as deltas).
        self.partial = {"user": "", "assistant": ""}
        self.started_at = datetime.now(UTC)
        self.conv_id: str | None = None
        self.stop_reason = ""
        self.end_reason = ""  # set when the SERVER ends the call (sent as ``end``)

    # ---- lifecycle --------------------------------------------------------

    async def run(self) -> None:
        log.info("Call %s from %s started", self.call_id, self.phone)
        try:
            try:
                self.conv_id = await conversations.start_call(
                    self.phone, self.call_id, self.started_at
                )
            except Exception:
                log.exception("Could not open conversation for call %s", self.call_id)

            history = await get_history(self.phone) if self.phone else []
            await self.runner.session_service.create_session(
                app_name=self.runner.app_name, user_id=self.user_id, session_id=self.session_id
            )
            seeded = contextualize(GREETING_CUE, history, self.phone, channel="voice")
            self.queue.send_content(types.Content(role="user", parts=[types.Part(text=seeded)]))
            await self._pump()
        except Exception:
            log.exception("Call %s failed", self.call_id)
            self.end_reason = protocol.END_ERROR
        finally:
            self.queue.close()
            if self.end_reason:
                with contextlib.suppress(Exception):
                    await self.ws.send_text(protocol.end_event(self.end_reason))
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
                self.call_id,
                self.end_reason or self.stop_reason or "closed",
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
            elif down in done:
                # The live session ended (or failed) while the caller was still on.
                failed = down.cancelled() or down.exception() is not None
                self.end_reason = protocol.END_ERROR if failed else protocol.END_SESSION_ENDED
        finally:
            for task in (up, down):
                if not task.done():
                    task.cancel()
            for task in (up, down):
                with contextlib.suppress(asyncio.CancelledError):
                    try:
                        await task
                    except Exception:
                        log.exception("%s failed (call %s)", task.get_name(), self.call_id)

    # ---- page → model -----------------------------------------------------

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
                try:
                    pcm = audio.whole_samples(audio.b64decode(protocol.media_payload(msg)))
                except ValueError:
                    log.debug("Bad media payload on call %s (dropped)", self.call_id)
                    continue
                if pcm:
                    self.queue.send_realtime(types.Blob(data=pcm, mime_type=MODEL_INPUT_MIME))
            elif event == "stop":
                self.stop_reason = "stop"
                return
            else:  # a repeated start, or anything unknown
                log.debug("Ignored %r event on call %s", event, self.call_id)

    # ---- model → page -----------------------------------------------------

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
        log.info("Live session ended on its own (call %s)", self.call_id)

    async def _send_ready(self, blobs: list[Any]) -> None:
        """Announce the session to the page with the model's native output rate.

        The rate comes from the first audio blob's MIME type (``rate=``); if the
        first event carries no audio, Gemini Live's documented default is used.
        """
        rate = audio.rate_from_mime(blobs[0].mime_type) if blobs else audio.DEFAULT_OUTPUT_RATE
        self.output_rate = rate
        self.chunker = audio.OutboundChunker(audio.chunk_size_for(rate))
        await self.ws.send_text(protocol.ready_event(audio.MODEL_INPUT_RATE, rate))
        log.info("Call %s ready (model output %d Hz)", self.call_id, rate)

    async def _send_media(self, chunks: list[bytes]) -> None:
        for chunk in chunks:
            await self.ws.send_text(protocol.media_event(audio.b64encode(chunk)))

    async def _send_transcript(self, role: Literal["user", "assistant"], text: str, final: bool) -> None:
        """Best effort: a failed transcript frame is logged and never disturbs audio."""
        if not self.send_transcripts:
            return
        try:
            await self.ws.send_text(protocol.transcript_event(role, text, final))
        except Exception as exc:
            log.debug("transcript frame not sent (call %s): %s", self.call_id, exc)

    async def _on_transcription(self, role: Literal["user", "assistant"], tr: Any) -> None:
        if tr is None:
            return
        if tr.finished:
            text = (tr.text or self.partial[role]).strip()
            self.partial[role] = ""
            if text:
                if role == "user":
                    self.transcript.user(text)
                else:
                    self.transcript.assistant(text)
                await self._send_transcript(role, text, final=True)
        elif tr.text:
            self.partial[role] += tr.text
            await self._send_transcript(role, self.partial[role].strip(), final=False)

    async def _on_event(self, event: Any) -> None:
        content = getattr(event, "content", None)
        blobs = [
            blob
            for part in getattr(content, "parts", None) or []
            if (blob := getattr(part, "inline_data", None))
            and blob.data
            and (blob.mime_type or "").startswith("audio/")
        ]
        if self.chunker is None:
            await self._send_ready(blobs)
        assert self.chunker is not None

        if getattr(event, "interrupted", None):
            # Barge-in: drop audio we haven't sent and tell the page to drop what
            # it has queued but not yet played.
            self.chunker.clear()
            await self.ws.send_text(protocol.clear_event())

        for call in self.recorder.observe(event):
            self.transcript.tool(call)

        for blob in blobs:
            rate = audio.rate_from_mime(blob.mime_type, default=self.output_rate or 0)
            if rate != self.output_rate:
                # Gemini Live has one fixed output rate; never seen in practice.
                log.warning(
                    "Call %s: model audio at %d Hz but the page plays %s Hz",
                    self.call_id,
                    rate,
                    self.output_rate,
                )
            await self._send_media(self.chunker.push(audio.whole_samples(blob.data)))

        await self._on_transcription("user", getattr(event, "input_transcription", None))
        await self._on_transcription("assistant", getattr(event, "output_transcription", None))

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
                    log.exception("transcripts write failed (call %s)", self.call_id)
                    break
        try:
            if self.conv_id is None:
                self.conv_id = await conversations.start_call(
                    self.phone, self.call_id, self.started_at
                )
            await conversations.finish_call(
                self.conv_id, self.phone, turns, self.started_at, ended_at
            )
        except Exception:
            log.exception("conversations write failed (call %s)", self.call_id)


async def wait_for_start(ws: CallSocket) -> protocol.CallStart | None:
    """Read frames until the page's ``start``; ``None`` if the socket ends first."""
    while True:
        try:
            text = await ws.receive_text()
        except WebSocketDisconnect:
            return None
        msg = protocol.parse_message(text)
        if msg is None:
            continue
        if msg["event"] == "start":
            return protocol.parse_start(msg)
        if msg["event"] == "stop":
            return None
        log.debug("%r before start (ignored)", msg["event"])


async def _end(ws: CallSocket, reason: str) -> None:
    """Tell the page why the call is over and close the socket (never raises)."""
    with contextlib.suppress(Exception):
        await ws.send_text(protocol.end_event(reason))
    with contextlib.suppress(Exception):
        await ws.close()


async def run_call(
    ws: CallSocket,
    *,
    runner: Runner | None = None,
    run_config: RunConfig | None = None,
) -> None:
    """Handle one accepted call-page socket end to end. Never raises."""
    try:
        try:
            async with asyncio.timeout(START_TIMEOUT_SECONDS):
                start = await wait_for_start(ws)
        except TimeoutError:
            log.info("Voice socket sent no start within %.0fs", START_TIMEOUT_SECONDS)
            await _end(ws, protocol.END_NO_START)
            return
        if start is None:
            log.info("Voice socket closed before start")
            return
        if not is_valid_phone(start.caller):
            log.info("Call %s rejected: invalid caller number %r", start.call_id, start.caller)
            await _end(ws, protocol.END_INVALID_CALLER)
            return
        bridge = CallBridge(
            ws,
            start,
            runner=runner or get_runner(),
            run_config=run_config or build_run_config(),
        )
        await bridge.run()
    except Exception:
        log.exception("Voice call handler crashed")
        await _end(ws, protocol.END_ERROR)
