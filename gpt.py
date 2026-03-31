from openai import AsyncOpenAI
from config import OPENAI_API_KEY, GPT_MODEL
from prompts import MAIN_PROMPT, SOS_PROMPT, WEEKLY_REPORT_PROMPT

client = AsyncOpenAI(api_key=OPENAI_API_KEY)


def _format_meals_for_context(meals) -> str:
    if not meals:
        return "Записей за этот период нет."
    lines = []
    for m in meals:
        day = m["created_at"].strftime("%d.%m %H:%M")
        mood = m["mood"] or "не указано"
        meal = m["meal_text"] or "не указано"
        lines.append(f"[{day}] Настроение: {mood} | Еда: {meal}")
    return "\n".join(lines)


def _format_sos_for_context(sessions) -> str:
    if not sessions:
        return ""
    lines = [f"SOS-сессий за неделю: {len(sessions)}"]
    for s in sessions:
        day = s["created_at"].strftime("%d.%m %H:%M")
        trigger = s["trigger_text"] or ""
        lines.append(f"[{day}] {trigger}")
    return "\n".join(lines)


def _build_user_context(user, today_meals=None, week_meals=None, week_sos=None) -> str:
    parts = [f"Имя: {user['name']}", f"Проблема: {user['concern']}"]
    if today_meals is not None:
        parts.append(f"\n--- Записи за сегодня ---\n{_format_meals_for_context(today_meals)}")
    if week_meals is not None:
        parts.append(f"\n--- Записи за последние 7 дней ---\n{_format_meals_for_context(week_meals)}")
    if week_sos:
        parts.append(f"\n--- SOS ---\n{_format_sos_for_context(week_sos)}")
    return "\n".join(parts)


async def ask_gpt(system_prompt: str, user_context: str, user_message: str) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": f"Контекст пользователя:\n{user_context}"},
        {"role": "user", "content": user_message},
    ]
    resp = await client.chat.completions.create(
        model=GPT_MODEL,
        messages=messages,
        max_tokens=500,
        temperature=0.7,
    )
    return resp.choices[0].message.content


async def ask_gpt_conversation(system_prompt: str, user_context: str, conversation: list[dict]) -> str:
    """For multi-turn dialogues like SOS."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": f"Контекст пользователя:\n{user_context}"},
        *conversation,
    ]
    resp = await client.chat.completions.create(
        model=GPT_MODEL,
        messages=messages,
        max_tokens=500,
        temperature=0.7,
    )
    return resp.choices[0].message.content


# === High-level functions ===

async def meal_checkin_response(user, today_meals, week_meals, user_message: str) -> str:
    ctx = _build_user_context(user, today_meals=today_meals, week_meals=week_meals)
    return await ask_gpt(MAIN_PROMPT, ctx, f"[ЗАПИСЬ ЕДЫ] {user_message}")


async def evening_reflection_start(user, today_meals, week_meals, week_sos) -> str:
    ctx = _build_user_context(user, today_meals=today_meals, week_meals=week_meals, week_sos=week_sos)
    return await ask_gpt(
        MAIN_PROMPT, ctx,
        "[ВЕЧЕРНЯЯ РЕФЛЕКСИЯ] Начни вечерний диалог. Спроси как прошёл день с едой.",
    )


async def evening_reflection_reply(user, today_meals, week_meals, user_message: str) -> str:
    ctx = _build_user_context(user, today_meals=today_meals, week_meals=week_meals)
    return await ask_gpt(MAIN_PROMPT, ctx, f"[ВЕЧЕРНЯЯ РЕФЛЕКСИЯ] {user_message}")


async def sos_response(user, conversation: list[dict]) -> str:
    ctx = _build_user_context(user)
    return await ask_gpt_conversation(SOS_PROMPT, ctx, conversation)


async def weekly_report(user, week_meals, week_sos, week_evenings) -> str:
    parts = [f"Имя: {user['name']}", f"Проблема: {user['concern']}"]
    parts.append(f"\n--- Записи еды за неделю ---\n{_format_meals_for_context(week_meals)}")
    if week_sos:
        parts.append(f"\n--- SOS-сессии ---\n{_format_sos_for_context(week_sos)}")
    if week_evenings:
        lines = []
        for e in week_evenings:
            day = e["created_at"].strftime("%d.%m %H:%M")
            lines.append(f"[{day}] {e['user_text'] or '(нет ответа)'}")
        parts.append(f"\n--- Вечерние рефлексии ---\n" + "\n".join(lines))

    ctx = "\n".join(parts)
    return await ask_gpt(
        WEEKLY_REPORT_PROMPT, ctx,
        "Напиши еженедельный разбор на основе данных выше.",
    )
