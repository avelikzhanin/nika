"""
OpenAI client wrapper + high-level scenario functions for Ника.

Notes:
- `_build_user_context()` reads everything Ника needs to know about the user
  (profile, concerns, facts, recent meals/SOS, last weekly summary) and
  produces a single text block injected as the second system message.
- Photo recognition uses GPT-4o-class vision via `client.chat.completions`
  with image_url input. We pass the Telegram file as a base64 data URL.
- Safety classifier runs BEFORE the main GPT call in scenarios that involve
  free text from the user (meal log, SOS, evening). On RED_FLAG it bypasses
  the normal response and returns a fixed safety message.
- Fact extractor runs AFTER the user message in main scenarios as a separate
  GPT call. Failures are non-fatal (we just skip storing).
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime

from openai import AsyncOpenAI

import db
from config import OPENAI_API_KEY, GPT_MODEL
from prompts import (
    MAIN_PROMPT,
    SOS_PROMPT,
    WEEKLY_REPORT_PROMPT,
    MICRO_INSIGHT_PROMPT,
    SAFETY_CLASSIFIER_PROMPT,
    FACT_EXTRACTOR_PROMPT,
    DAY_RECAP_PARSER_PROMPT,
    PHOTO_RECOGNITION_PROMPT,
)

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENAI_API_KEY)


# Vision-capable model is required for photo recognition. Keep it overridable.
VISION_MODEL = "gpt-4o-mini"


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

GOAL_NAMES = {
    "lose_weight": "похудеть",
    "energy": "больше энергии в течение дня",
    "food_relationship": "наладить отношения с едой",
    "mindful": "осознанно питаться",
    "muscle": "набрать мышцы",
}

ACTIVITY_NAMES = {
    "sitting": "сидячий образ жизни (в основном за компьютером)",
    "mixed": "смешанный — сидит и на ногах поровну",
    "on_feet": "много на ногах в течение дня",
    "physical": "физически активная работа",
}

TRAINING_NAMES = {
    "regular": "тренируется регулярно (3+ раз в неделю)",
    "sometimes": "тренируется иногда (1-2 раза в неделю)",
    "rare": "редко тренируется или сейчас в паузе",
    "none": "не тренируется",
}


def gender_form(user) -> str:
    """Return 'female' | 'male' | 'neutral' for prompt hints."""
    g = user.get("gender") if isinstance(user, dict) else user["gender"]
    if g == "female":
        return "female"
    if g == "male":
        return "male"
    return "neutral"


def _format_meals(meals) -> str:
    if not meals:
        return "Записей нет."
    lines = []
    for m in meals:
        day = m["created_at"].strftime("%d.%m %H:%M")
        mood = m["mood"] or "—"
        meal = m["meal_text"] or "—"
        slot = f" [{m.get('meal_slot')}]" if m.get("meal_slot") else ""
        lines.append(f"[{day}]{slot} состояние: {mood} | еда: {meal}")
    return "\n".join(lines)


def _format_sos(sessions) -> str:
    if not sessions:
        return "SOS на этой неделе не нажимала."
    lines = [f"SOS-сессий: {len(sessions)}."]
    for s in sessions:
        day = s["created_at"].strftime("%d.%m %H:%M")
        trigger = s["trigger_text"] or ""
        lines.append(f"[{day}] триггер: {trigger}")
    return "\n".join(lines)


def _format_evenings(evenings) -> str:
    if not evenings:
        return "Вечерних записей не было."
    lines = []
    for e in evenings:
        day = e["created_at"].strftime("%d.%m %H:%M")
        text = e["user_text"] or "(нет ответа)"
        lines.append(f"[{day}] {text}")
    return "\n".join(lines)


def _format_facts(facts: list[dict]) -> str:
    if not facts:
        return "Пока ничего особого про юзера не выучила."
    by_cat: dict[str, list[str]] = {}
    for f in facts:
        by_cat.setdefault(f["category"], []).append(f["fact_text"])
    cat_labels = {
        "dislikes": "не ест / не любит",
        "likes": "любит",
        "allergies": "аллергии / непереносимости",
        "triggers": "триггеры срывов",
        "helps": "что помогает в моменте",
        "context": "обстоятельства жизни",
    }
    lines = []
    for cat, items in by_cat.items():
        label = cat_labels.get(cat, cat)
        lines.append(f"  • {label}: {', '.join(items)}")
    return "\n".join(lines)


async def _build_user_context(
    user,
    *,
    today_meals=None,
    week_meals=None,
    week_sos=None,
    week_evenings=None,
    include_facts: bool = True,
    include_last_summary: bool = False,
    include_recent_replies: bool = True,
) -> str:
    """
    Build the second system message — everything Ника needs to know.
    Reads facts and last weekly summary from DB when requested.
    """
    name = user["name"] or "(имя не указано)"
    goal_key = user.get("goal") if isinstance(user, dict) else user["goal"]
    goal_text = GOAL_NAMES.get(goal_key, "не указана")

    gform = gender_form(user)
    age = user["age"]
    height = user["height_cm"]
    weight = user["weight_kg"]
    activity = ACTIVITY_NAMES.get(user["activity_level"], "не указано")
    training = TRAINING_NAMES.get(user["training_frequency"], "не указано")

    parts = [
        f"Имя: {name}",
        f"Цель (внутренний фокус, НЕ называй её юзеру вслух): {goal_text}",
        f"Род обращения (gender_form): {gform}",
    ]
    body_bits = []
    if age:
        body_bits.append(f"возраст {age}")
    if height:
        body_bits.append(f"рост {height} см")
    if weight:
        body_bits.append(f"вес {weight} кг")
    if body_bits:
        parts.append("Тело: " + ", ".join(body_bits))
    parts.append(f"Активность днём: {activity}")
    parts.append(f"Тренировки: {training}")

    concerns = await db.get_concerns(user["id"])
    if concerns:
        parts.append("Концерны (что беспокоит): " + "; ".join(concerns))

    if include_facts:
        facts = await db.get_facts(user["id"])
        parts.append(f"Что я выучила про юзера (user_facts):\n{_format_facts(facts)}")

    if today_meals is not None:
        parts.append(f"\n--- Сегодня ---\n{_format_meals(today_meals)}")
    if week_meals is not None:
        parts.append(f"\n--- За последние 7 дней ---\n{_format_meals(week_meals)}")
    if week_sos is not None:
        parts.append(f"\n--- SOS за неделю ---\n{_format_sos(week_sos)}")
    if week_evenings is not None:
        parts.append(f"\n--- Вечерние записи за неделю ---\n{_format_evenings(week_evenings)}")

    if include_last_summary:
        last = await db.get_last_weekly_summary(user["id"])
        if last:
            parts.append(
                f"\n--- Прошлый воскресный разбор ({last['week_start']}) ---\n"
                f"Главное наблюдение: {last['main_pattern']}\n"
                f"Идея, которую обронила: {last['suggestion']}"
            )

    if include_recent_replies:
        recent = await db.get_recent_assistant_replies(user["id"], n=3)
        if recent:
            joined = "\n---\n".join(recent)
            parts.append(
                "\n--- Твои последние 3 ответа этому юзеру (для разнообразия) ---\n"
                f"{joined}\n\n"
                "Не повторяй те же открывающие фразы и обороты. "
                "Сохраняй стиль, но формулируй иначе."
            )

    return "\n".join(parts)


# ──────────────────────────────────────────────
# Low-level OpenAI wrappers
# ──────────────────────────────────────────────

async def _chat(
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int = 500,
    temperature: float = 0.7,
) -> str:
    resp = await client.chat.completions.create(
        model=model or GPT_MODEL,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return resp.choices[0].message.content.strip()


async def _ask_with_context(
    system_prompt: str,
    user_context: str,
    user_message: str,
    *,
    max_tokens: int = 500,
    temperature: float = 0.7,
) -> str:
    return await _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": f"Контекст пользователя:\n{user_context}"},
            {"role": "user", "content": user_message},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )


async def _ask_conversation(
    system_prompt: str,
    user_context: str,
    conversation: list[dict],
    *,
    max_tokens: int = 500,
    temperature: float = 0.7,
) -> str:
    return await _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": f"Контекст пользователя:\n{user_context}"},
            *conversation,
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )


# ──────────────────────────────────────────────
# Safety classifier
# ──────────────────────────────────────────────

async def safety_check(user_text: str) -> bool:
    """Return True if RED_FLAG (RPP / crisis signal detected), False otherwise."""
    if not user_text or not user_text.strip():
        return False
    try:
        result = await _chat(
            [
                {"role": "system", "content": SAFETY_CLASSIFIER_PROMPT},
                {"role": "user", "content": user_text},
            ],
            max_tokens=5,
            temperature=0,
        )
        return result.strip().upper().startswith("RED_FLAG")
    except Exception as e:
        logger.error(f"safety_check error: {e}")
        return False


# ──────────────────────────────────────────────
# Fact extractor (long-term memory)
# ──────────────────────────────────────────────

async def extract_facts(user_text: str) -> list[dict]:
    """Returns a list like [{"category": "dislikes", "fact": "яйца"}, ...]."""
    if not user_text or not user_text.strip():
        return []
    try:
        raw = await _chat(
            [
                {"role": "system", "content": FACT_EXTRACTOR_PROMPT},
                {"role": "user", "content": user_text},
            ],
            max_tokens=300,
            temperature=0,
        )
        # The model sometimes wraps JSON in code fences. Strip them.
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            # remove leading and trailing code fences
            cleaned = cleaned.strip("`")
            # remove optional language tag
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        parsed = json.loads(cleaned)
        if not isinstance(parsed, list):
            return []
        out = []
        for item in parsed:
            cat = item.get("category")
            fact = item.get("fact")
            if cat and fact and isinstance(cat, str) and isinstance(fact, str):
                out.append({"category": cat.strip(), "fact": fact.strip()})
        return out
    except Exception as e:
        logger.error(f"extract_facts error: {e}")
        return []


async def persist_facts(user_id: int, user_text: str) -> None:
    """Convenience: extract facts and write them to DB. Non-fatal on failure."""
    try:
        facts = await extract_facts(user_text)
        for f in facts:
            await db.add_fact(user_id, f["category"], f["fact"])
    except Exception as e:
        logger.error(f"persist_facts error: {e}")


# ──────────────────────────────────────────────
# Meal-checkin response
# ──────────────────────────────────────────────

async def meal_checkin_response(user, today_meals, week_meals, user_message: str) -> str:
    ctx = await _build_user_context(
        user,
        today_meals=today_meals,
        week_meals=week_meals,
        include_last_summary=True,
    )
    return await _ask_with_context(
        MAIN_PROMPT, ctx, f"[ЗАПИСЬ ЕДЫ] {user_message}",
    )


# ──────────────────────────────────────────────
# Evening reflection (3x / 2x modes)
# ──────────────────────────────────────────────

async def evening_reflection_start(user, today_meals, week_meals, week_sos) -> str:
    ctx = await _build_user_context(
        user,
        today_meals=today_meals,
        week_meals=week_meals,
        week_sos=week_sos,
        include_last_summary=True,
    )
    return await _ask_with_context(
        MAIN_PROMPT,
        ctx,
        "[ВЕЧЕРНЯЯ РЕФЛЕКСИЯ] Начни вечерний диалог. Спроси мягко, как прошёл день с едой.",
    )


async def evening_reflection_reply(user, today_meals, week_meals, user_message: str) -> str:
    ctx = await _build_user_context(
        user, today_meals=today_meals, week_meals=week_meals
    )
    return await _ask_with_context(MAIN_PROMPT, ctx, f"[ВЕЧЕРНЯЯ РЕФЛЕКСИЯ] {user_message}")


# ──────────────────────────────────────────────
# Day recap (1x/day mode)
# ──────────────────────────────────────────────

async def parse_day_recap(user_message: str) -> list[dict]:
    """Parse a free-form recap into per-meal entries.
    Returns list of {slot, text, mood}. Falls back to a single unknown entry."""
    try:
        raw = await _chat(
            [
                {"role": "system", "content": DAY_RECAP_PARSER_PROMPT},
                {"role": "user", "content": user_message},
            ],
            max_tokens=400,
            temperature=0,
        )
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        parsed = json.loads(cleaned)
        if isinstance(parsed, list) and parsed:
            return parsed
    except Exception as e:
        logger.error(f"parse_day_recap error: {e}")
    return [{"slot": "unknown", "text": user_message, "mood": None}]


async def day_recap_response(user, today_meals, week_meals, user_message: str) -> str:
    """Generate Ника's reply to a whole-day recap message."""
    ctx = await _build_user_context(
        user, today_meals=today_meals, week_meals=week_meals, include_last_summary=True,
    )
    return await _ask_with_context(
        MAIN_PROMPT, ctx, f"[ВЕЧЕРНИЙ ОБЗОР ДНЯ] {user_message}",
    )


# ──────────────────────────────────────────────
# SOS
# ──────────────────────────────────────────────

async def sos_response(user, conversation: list[dict]) -> str:
    ctx = await _build_user_context(user, week_sos=await db.get_week_sos(user["id"]))
    return await _ask_conversation(SOS_PROMPT, ctx, conversation)


# ──────────────────────────────────────────────
# Weekly report
# ──────────────────────────────────────────────

async def weekly_report(user, week_meals, week_sos, week_evenings) -> dict:
    """
    Returns dict with keys:
      text     — the full letter to send to the user
      pattern  — extracted PATTERN line (or None)
      suggestion — extracted SUGGESTION line (or None)
    """
    ctx = await _build_user_context(
        user,
        week_meals=week_meals,
        week_sos=week_sos,
        week_evenings=week_evenings,
        include_last_summary=True,
    )
    raw = await _ask_with_context(
        WEEKLY_REPORT_PROMPT,
        ctx,
        "Напиши еженедельное письмо на основе данных выше. "
        "В конце добавь блок ---META--- как описано в инструкции.",
        max_tokens=900,
    )
    return _parse_weekly_meta(raw)


def _parse_weekly_meta(raw: str) -> dict:
    text = raw
    pattern = None
    suggestion = None
    if "---META---" in raw:
        text, meta = raw.split("---META---", 1)
        text = text.strip()
        for line in meta.splitlines():
            line = line.strip()
            if line.upper().startswith("PATTERN:"):
                p = line.split(":", 1)[1].strip()
                pattern = p if p.lower() != "none" else None
            elif line.upper().startswith("SUGGESTION:"):
                s = line.split(":", 1)[1].strip()
                suggestion = s if s.lower() != "none" else None
    return {"text": text, "pattern": pattern, "suggestion": suggestion}


# ──────────────────────────────────────────────
# Micro-insight
# ──────────────────────────────────────────────

async def micro_insight(user, signal_text: str, week_meals=None) -> str:
    ctx = await _build_user_context(
        user, week_meals=week_meals, include_last_summary=False,
    )
    return await _ask_with_context(
        MICRO_INSIGHT_PROMPT,
        ctx,
        f"[СИГНАЛ] {signal_text}\n\nНапиши короткое наблюдение (1-2 предложения).",
        max_tokens=180,
    )


# ──────────────────────────────────────────────
# Photo recognition
# ──────────────────────────────────────────────

async def recognize_meal_photo(photo_bytes: bytes) -> str:
    """Run vision on a Telegram photo. Returns short food description or 'не похоже на еду'."""
    b64 = base64.b64encode(photo_bytes).decode("ascii")
    data_url = f"data:image/jpeg;base64,{b64}"
    try:
        resp = await client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
                {"role": "system", "content": PHOTO_RECOGNITION_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Опиши, что на фото."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            max_tokens=80,
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"recognize_meal_photo error: {e}")
        return ""
