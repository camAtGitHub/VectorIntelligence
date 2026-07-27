"""Chat generate flow: SSE chunks, fillers, sentence stream wrap, summaries."""
import asyncio
import json
import re
import time
import uuid
from typing import AsyncIterator, List, Optional

import deps
import process_state
from debug_log import _redact_messages, debug
from llm import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_SUMMARY_TEMPERATURE,
    LLM_TEMPERATURE,
    MODEL,
    SUMMARY_MODEL,
    VISION_MODEL,
    _llm_timeout,
    image_payload_too_large,
    llm_chat_once,
    llm_sentence_stream,
)
from logging_util import print  # noqa: F401
from process_state import (
    THINKING_DELAY,
    _ALL_FILLER_PHRASES,
    pick_thinking_phrase,
)
from image_archive import save_submitted_image
from prompt_assembly import _effective_face, _extract_last_user_text, prepare_messages
from response_cleanup import clean_response, strip_markdown
from vision import _GETIMAGE_PAYLOAD, is_vision_intent


def sse_chunk(
    content: str = "",
    finish: Optional[str] = None,
    *,
    model: Optional[str] = None,
) -> str:
    """Build one OpenAI-compatible SSE chunk. ``model`` labels the effective
    upstream model for this turn (None → MODEL); vision image turns pass
    VISION_MODEL for debug parity with the stream body."""
    payload = {
        "id":      f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object":  "chat.completion.chunk",
        "created": int(time.time()),
        "model":   model or MODEL,
        "choices": [{
            "index":         0,
            "delta":         {"content": content} if content else {},
            "finish_reason": finish,
        }],
    }
    return f"data: {json.dumps(payload)}\n\n"


async def stream_sentences_with_filler(
    messages: list,
    temperature: float,
    filler_enabled: bool = True,
    *,
    model: Optional[str] = None,
) -> AsyncIterator[str]:
    """Wrap llm_sentence_stream. If the first sentence takes longer than
    THINKING_DELAY to arrive, yield a short thinking-filler line before it so
    Vector acknowledges the question instead of sitting silent. The filler is
    just an ordinary sentence chunk - it flows through the normal cleanup.
    ``model`` is forwarded to the stream (None → LLM_MODEL; vision turns pass
    VISION_MODEL)."""
    agen = llm_sentence_stream(messages, temperature, model=model).__aiter__()
    first_task = asyncio.ensure_future(agen.__anext__())
    try:
        if filler_enabled:
            try:
                # shield: on timeout the task keeps running - we just stop
                # waiting on it, speak the filler, then await it for real.
                first = await asyncio.wait_for(
                    asyncio.shield(first_task), THINKING_DELAY
                )
            except asyncio.TimeoutError:
                filler = pick_thinking_phrase()
                print(f"[vector-ai] slow first sentence - thinking filler: {filler!r}")
                yield filler
                first = await first_task
        else:
            first = await first_task
    except StopAsyncIteration:
        return
    yield first
    async for sentence in agen:
        yield sentence


def cap_chunk_animations(text: str, allowance: int) -> tuple[str, int]:
    """Keep at most `allowance` animation commands in this chunk; strip the rest.
    Returns (text, count_kept)."""
    matches = list(re.finditer(r'\{\{playAnimation(?:WI)?\|\|[^}]+\}\}', text))
    if len(matches) <= allowance:
        return text, len(matches)
    keep_idx = set(range(allowance))
    out, last_end, kept = [], 0, 0
    for i, m in enumerate(matches):
        out.append(text[last_end:m.start()])
        if i in keep_idx:
            out.append(m.group(0))
            kept += 1
        last_end = m.end()
    out.append(text[last_end:])
    return "".join(out), kept


# -- Conversation memory -------------------------------------------------------

async def _summarise_conversation(messages: list, latest_reply: str,
                                  face_id: int, face_name: Optional[str]) -> None:
    """Background task: distil this conversation into one line and store it as
    the face's 'last conversation', so Vector can recall it next session.

    Runs on SUMMARY_MODEL (cheap/fast). Failures are swallowed - a missing
    summary is harmless."""
    turns = [
        m for m in messages
        if m.role in ("user", "assistant")
        and isinstance(m.content, str) and m.content.strip()
    ]
    if len(turns) < 3:  # too short to be worth a recap
        return
    lines = [
        f"{'User' if m.role == 'user' else 'Vector'}: {m.content.strip()}"
        for m in turns[-16:]
    ]
    if latest_reply.strip():
        lines.append(f"Vector: {latest_reply.strip()}")
    transcript = "\n".join(lines)
    prompt = [
        {"role": "system", "content":
            "You summarise a conversation between a user and a small desktop "
            "robot in ONE short factual sentence, from the robot's point of "
            "view, naming the actual topics discussed. Refer to the human "
            "only as 'the user' and to the robot as 'I' - never use a name "
            "for either, even if names appear in the text. Be literal and "
            "factual, not witty. No preamble, no quotes - just the "
            "sentence."},
        {"role": "user", "content": transcript},
    ]
    try:
        summary = await llm_chat_once(
            prompt,
            model=SUMMARY_MODEL,
            temperature=LLM_SUMMARY_TEMPERATURE,
            top_p=0.95,
            timeout=_llm_timeout(read=60.0),
            max_tokens=128,
            tag="convo_summary",
        )
        summary = strip_markdown(summary).strip().strip('"').strip()
        if summary:
            deps.MEMORY.set_convo_summary(face_id, summary)
            print(f"[memory] convo summary [{face_name}]: {summary!r}")
            # A finished conversation is a notable event - refresh the mood.
            from routes.mood import _reflect_mood
            asyncio.create_task(_reflect_mood())
    except Exception as e:
        print(f"[memory] summary failed: {e}")


# -- Main flow -----------------------------------------------------------------

_OVERSIZED_IMAGE_PLACEHOLDER = "[image omitted: payload too large]"


def sanitize_chat_image_parts(
    content: list, *, limit: Optional[int] = None
) -> tuple[list, list[str], bool]:
    """Replace oversized multimodal image_url parts with a text placeholder.

    Returns ``(new_content, archive_urls, has_image)`` where:
    - ``archive_urls`` are only payloads that passed the size check (safe to
      archive and send upstream);
    - ``has_image`` is True if any image_url part remains after sanitization.

    Pure aside from the size check; does not archive or call the LLM.
    """
    if not isinstance(content, list):
        return content, [], False
    out: list = []
    archive_urls: list[str] = []
    has_image = False
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            out.append(part)
            continue
        iu = part.get("image_url")
        url = iu.get("url") if isinstance(iu, dict) else iu
        if not isinstance(url, str) or not url:
            out.append(part)
            continue
        too_big = (
            image_payload_too_large(url, limit=limit)
            if limit is not None
            else image_payload_too_large(url)
        )
        if too_big:
            print(
                f"[vector-ai] chat image too large "
                f"(chars={len(url)}) — dropping image part"
            )
            out.append({"type": "text", "text": _OVERSIZED_IMAGE_PLACEHOLDER})
            continue
        out.append(part)
        archive_urls.append(url)
        has_image = True
    return out, archive_urls, has_image


async def generate(messages: list, temperature: float | None = None) -> AsyncIterator[str]:
    process_state._LAST_USER_VOICE_TS = time.time()  # suppress proactive speech during chat
    temp = LLM_TEMPERATURE if temperature is None else float(temperature)
    # Multimodal-aware (joins text parts); matches prepare_messages ranking.
    last_user_text = _extract_last_user_text(messages)
    has_image = bool(messages) and isinstance(messages[-1].content, list)
    print(f"[vector-ai] User: {last_user_text!r} (image: {has_image})")
    debug(
        "WIREPOD RECV /v1/chat/completions generate()",
        {
            "temperature": temp,
            "n_messages": len(messages),
            "has_image": has_image,
            "messages": _redact_messages(messages),
        },
    )
    if has_image:
        # Bound absurd payloads; strip oversized image parts before archive/LLM.
        content, archive_urls, has_image = sanitize_chat_image_parts(
            list(messages[-1].content)
        )
        messages[-1].content = content
        for url in archive_urls:
            save_submitted_image(url, "chat")

    # Vision-intent backstop: if the user is clearly asking to look at something
    # and no photo is attached yet, force the camera command rather than letting
    # the LLM hallucinate from stale conversation history. No verbal preamble -
    # the audio cue is the shutter animation Wire-Pod plays for getImage.
    if not has_image and is_vision_intent(last_user_text):
        print("[vector-ai] Vision intent - forcing getImage (shutter only, no preamble)")
        debug("WIREPOD SEND force getImage (vision intent, no image attached)")
        yield sse_chunk(_GETIMAGE_PAYLOAD)
        yield sse_chunk("", finish="stop")
        yield "data: [DONE]\n\n"
        return

    try:
        if not LLM_API_KEY and "openrouter.ai" in LLM_BASE_URL:
            print("[vector-ai] ERROR: OPENROUTER_API_KEY / LLM_API_KEY is not set")
            yield sse_chunk("My cloud brain has no API key. Check vector-ai .env.")
            yield sse_chunk("", finish="stop")
            yield "data: [DONE]\n\n"
            return

        t_req = time.monotonic()
        eff_face = _effective_face()
        prepared = prepare_messages(messages, eff_face)
        debug(
            "PREPARED messages for upstream (after persona/memory/history trim)",
            {
                "face": eff_face,
                "n_prepared": len(prepared),
                "messages": _redact_messages(prepared),
            },
        )

        # Stream sentences as soon as they finish generating so Vector starts
        # speaking before the rest of the response is produced. The vision-
        # intent regex above catches the common "what do you see"-style queries
        # before the LLM runs; if it misses one and the LLM tacks on getImage
        # mid-response, we cut over to the camera trigger here. Any sentences
        # already yielded will have been spoken - accepted trade-off for the
        # latency win. Thinking filler masks slow first-token latency.
        anims_emitted = 0
        any_emitted   = False
        reply_parts   = []
        # Multimodal getImage replies use VISION_MODEL; text-only stays on MODEL.
        stream_model = VISION_MODEL if has_image else None
        async for sentence in stream_sentences_with_filler(
            prepared, temp, filler_enabled=not has_image, model=stream_model
        ):
            cleaned = clean_response(sentence)

            if not has_image:
                # Mid-stream hallucination guard: LLM decided to peek without
                # us asking. Switch to camera trigger immediately, stop.
                if "{{getImage" in cleaned:
                    print("[vector-ai] LLM emitted getImage mid-stream - switching to camera")
                    yield sse_chunk(_GETIMAGE_PAYLOAD, model=stream_model)
                    yield sse_chunk("", finish="stop", model=stream_model)
                    yield "data: [DONE]\n\n"
                    return
            else:
                # A photo is ALREADY attached - the LLM is describing it. Strip
                # any getImage it emits so it can't trigger a second photo and
                # spiral into a multi-shot loop. One query, one photo.
                if "{{getImage" in cleaned:
                    print("[vector-ai] stripped getImage (photo already attached)")
                    cleaned = re.sub(r'\{\{getImage\|\|[^}]*\}\}', '', cleaned)

            allowance      = max(0, 1 - anims_emitted)
            cleaned, kept  = cap_chunk_animations(cleaned, allowance)
            anims_emitted += kept
            if cleaned.strip():
                print(f"[vector-ai] -> {cleaned!r}")
                debug(f"WIREPOD SEND chunk: {cleaned!r}")
                # Filler lines mask latency - they aren't part of what Vector
                # actually said, so keep them out of memory/observations.
                if cleaned.strip() not in _ALL_FILLER_PHRASES:
                    reply_parts.append(cleaned.strip())
                yield sse_chunk(cleaned, model=stream_model)
                any_emitted = True

        print(f"[vector-ai] timing: full response {time.monotonic() - t_req:.2f}s")
        debug(
            f"WIREPOD SEND complete {time.monotonic() - t_req:.2f}s",
            {"reply": " ".join(reply_parts)},
        )

        # -- Companion memory (post-response, non-blocking) --
        # Strip {{...}} commands - memory stores what Vector *said*, not the
        # eye-colour/animation directives chipper consumed.
        reply = re.sub(r'\{\{[^}]*\}\}', '', " ".join(reply_parts))
        reply = re.sub(r'\s+', ' ', reply).strip()
        mem_face = eff_face if (eff_face and not eff_face.get("is_stranger")
                                and eff_face.get("face_id")) else None
        MEMORY = deps.MEMORY
        # Visual memory: store what Vector saw - but only when he genuinely
        # described the scene. A too-thin reply means the describe failed
        # (e.g. the model re-requested the photo); "One sec." isn't a memory.
        if has_image and len(reply) >= 25:
            obs_face = mem_face["face_id"] if mem_face else None
            MEMORY.remember_observation(reply[:300], face_id=obs_face)
            print(f"[memory] +observation: {reply[:80]!r}")
            # Desk layout map: extract durable object→location pairs in the
            # background so ambient can treat moves as novelty (3A). Failures
            # swallowed; must not block the streaming path.
            try:
                from desk_extract import extract_and_upsert_desk_items

                asyncio.create_task(
                    extract_and_upsert_desk_items(reply[:300], MEMORY)
                )
            except Exception as e:
                print(f"[desk] schedule extract failed: {e}")
        elif has_image:
            print(f"[memory] observation skipped (reply too thin): {reply!r}")
        # Conversation memory: distil this exchange in the background.
        if mem_face:
            asyncio.create_task(_summarise_conversation(
                list(messages), reply, mem_face["face_id"], mem_face.get("name")))

        if not any_emitted:
            yield sse_chunk("Hmm.", model=stream_model)
        yield sse_chunk("", finish="stop", model=stream_model)
        yield "data: [DONE]\n\n"
    except Exception as e:
        print(f"[vector-ai] Error: {e}")
        yield sse_chunk("My brain just hiccuped. Try that again.")
        yield sse_chunk("", finish="stop")
        yield "data: [DONE]\n\n"
