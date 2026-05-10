import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import db
import gpt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler()


# ──────────────────────────────────────────────
# Keyboards
# ──────────────────────────────────────────────

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍽 Записать еду")],
        [KeyboardButton(text="🆘 Хочу есть, но не голодна")],
    ],
    resize_keyboard=True,
)

CONCERN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Переедание / заедание стресса")],
        [KeyboardButton(text="Ночные перекусы")],
        [KeyboardButton(text="Бесконтрольные срывы")],
        [KeyboardButton(text="Сложные отношения с едой в целом")],
    ],
    resize_keyboard=True,
)

TIMEZONE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🇷🇺 Москва (МСК)"), KeyboardButton(text="🇷🇺 Питер (МСК)")],
        [KeyboardButton(text="🇷🇺 Екатеринбург (+2)"), KeyboardButton(text="🇷🇺 Новосибирск (+4)")],
        [KeyboardButton(text="🇷🇺 Калининград (−1)"), KeyboardButton(text="Другой")],
    ],
    resize_keyboard=True,
)

TIMEZONE_MAP = {
    "🇷🇺 Москва (МСК)": "Europe/Moscow",
    "🇷🇺 Питер (МСК)": "Europe/Moscow",
    "🇷🇺 Екатеринбург (+2)": "Asia/Yekaterinburg",
    "🇷🇺 Новосибирск (+4)": "Asia/Novosibirsk",
    "🇷🇺 Калининград (−1)": "Europe/Kaliningrad",
}

MOOD_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Голодна"), KeyboardButton(text="Устала")],
        [KeyboardButton(text="Тревожно"), KeyboardButton(text="Скучно")],
        [KeyboardButton(text="Нормально"), KeyboardButton(text="На автомате")],
    ],
    resize_keyboard=True,
)


# ──────────────────────────────────────────────
# FSM States
# ──────────────────────────────────────────────

class Onboarding(StatesGroup):
    name = State()
    concern = State()
    timezone = State()
    evening_time = State()


class MealLog(StatesGroup):
    mood = State()
    meal_text = State()


class SOSDialog(StatesGroup):
    chatting = State()


# ──────────────────────────────────────────────
# /start — Onboarding
# ──────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if user and user["onboarding_done"]:
        await message.answer("С возвращением! 👋", reply_markup=MAIN_MENU)
        return
    await message.answer(
        "Привет! Я Ника — помогу тебе замечать, как эмоции влияют на то, что и как ты ешь.\n\n"
        "Как тебя зовут?",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Onboarding.name)


@router.message(Onboarding.name)
async def onboarding_name(message: Message, state: FSMContext):
    name = message.text.strip()
    await db.create_user(message.from_user.id, name)
    await state.update_data(name=name)
    await message.answer(
        f"{name}, приятно познакомиться.\n\n"
        "Что тебя больше всего беспокоит в отношениях с едой?",
        reply_markup=CONCERN_KB,
    )
    await state.set_state(Onboarding.concern)


@router.message(Onboarding.concern)
async def onboarding_concern(message: Message, state: FSMContext):
    concern = message.text.strip()
    await db.update_user_concern(message.from_user.id, concern)
    await message.answer(
        "В каком ты часовом поясе?",
        reply_markup=TIMEZONE_KB,
    )
    await state.set_state(Onboarding.timezone)


@router.message(Onboarding.timezone)
async def onboarding_timezone(message: Message, state: FSMContext):
    text = message.text.strip()
    tz_name = TIMEZONE_MAP.get(text)
    if not tz_name:
        # Пробуем как IANA timezone напрямую (для варианта "Другой")
        if text == "Другой":
            await message.answer(
                "Напиши свой часовой пояс, например: Europe/Moscow, Asia/Vladivostok, Europe/Berlin",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        # Пробуем распарсить как IANA
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(text)
            tz_name = text
        except (KeyError, Exception):
            await message.answer(
                "Не знаю такой часовой пояс. Попробуй ещё раз, например: Europe/Moscow, Asia/Vladivostok",
            )
            return

    await db.update_user_timezone(message.from_user.id, tz_name)
    await state.update_data(timezone=tz_name)
    await message.answer(
        "Каждый вечер я буду писать тебе — просто спрошу, как прошёл день с едой.\n\n"
        "Во сколько тебе удобно? Напиши время, например: 20:00",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Onboarding.evening_time)


@router.message(Onboarding.evening_time)
async def onboarding_evening_time(message: Message, state: FSMContext):
    text = message.text.strip().replace(".", ":").replace("-", ":")
    try:
        parts = text.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, IndexError):
        await message.answer("Не поняла время. Напиши в формате ЧЧ:ММ, например 20:00")
        return

    await db.update_user_evening_time(message.from_user.id, hour, minute)
    data = await state.get_data()
    tz_name = data.get("timezone", "Europe/Moscow")
    schedule_evening_for_user(message.from_user.id, hour, minute, tz_name)

    days_until_report = 7
    report_date = (datetime.utcnow() + timedelta(days=days_until_report)).strftime("%d.%m")
    schedule_weekly_for_user(message.from_user.id, hour, minute, tz_name)

    await message.answer(
        f"Готово! Буду писать тебе каждый вечер в {hour:02d}:{minute:02d}.\n\n"
        f"А первый недельный разбор пришлю {report_date}.\n\n"
        "В любой момент ты можешь:\n"
        "🍽 Записать еду — когда поела или собираешься\n"
        "🆘 SOS — если хочется есть, но не от голода\n\n"
        "Начнём?",
        reply_markup=MAIN_MENU,
    )
    await state.clear()


# ──────────────────────────────────────────────
# 🍽 Записать еду
# ──────────────────────────────────────────────

@router.message(F.text == "🍽 Записать еду")
async def meal_start(message: Message, state: FSMContext):
    await state.clear()
    await db.set_evening_pending(message.from_user.id, False)
    await message.answer("Как ты сейчас себя чувствуешь?", reply_markup=MOOD_KB)
    await state.set_state(MealLog.mood)


@router.message(MealLog.mood)
async def meal_mood(message: Message, state: FSMContext):
    mood = message.text.strip()
    await state.update_data(mood=mood)
    await message.answer(
        "Что ты ела / собираешься есть? Просто напиши текстом, без деталей.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(MealLog.meal_text)


@router.message(MealLog.meal_text)
async def meal_text(message: Message, state: FSMContext):
    data = await state.get_data()
    mood = data.get("mood", "")
    meal = message.text.strip()
    is_hungry = mood.lower() in ("голодна", "нормально")

    user = await db.get_user(message.from_user.id)
    tz = user["timezone"] or "Europe/Moscow"
    today_meals = await db.get_today_meals(message.from_user.id, tz)
    week_meals = await db.get_week_meals(message.from_user.id)

    combined = f"Состояние: {mood}\nЕда: {meal}"
    gpt_response = await gpt.meal_checkin_response(user, today_meals, week_meals, combined)

    await db.save_meal_log(message.from_user.id, mood, meal, is_hungry, gpt_response)
    await message.answer(gpt_response, reply_markup=MAIN_MENU)
    await state.clear()


# ──────────────────────────────────────────────
# 🆘 SOS
# ──────────────────────────────────────────────

@router.message(F.text == "🆘 Хочу есть, но не голодна")
async def sos_start(message: Message, state: FSMContext):
    await state.clear()
    await db.set_evening_pending(message.from_user.id, False)
    user = await db.get_user(message.from_user.id)

    conversation = [
        {"role": "user", "content": "Я хочу есть, но я не голодна."},
    ]
    response = await gpt.sos_response(user, conversation)
    conversation.append({"role": "assistant", "content": response})

    await state.update_data(sos_conversation=conversation, sos_trigger="Нажала SOS")
    await state.set_state(SOSDialog.chatting)
    await message.answer(response, reply_markup=ReplyKeyboardRemove())


@router.message(SOSDialog.chatting, F.text == "Закончить SOS")
async def sos_end(message: Message, state: FSMContext):
    data = await state.get_data()
    conversation = data.get("sos_conversation", [])
    trigger = data.get("sos_trigger", "")
    dialogue_text = "\n".join(
        f"{'User' if m['role'] == 'user' else 'Ника'}: {m['content']}" for m in conversation
    )
    await db.save_sos_session(message.from_user.id, trigger, dialogue_text)
    await message.answer("Спасибо, что написала. Я рядом, если что ✓", reply_markup=MAIN_MENU)
    await state.clear()


@router.message(SOSDialog.chatting)
async def sos_chat(message: Message, state: FSMContext):
    data = await state.get_data()
    conversation = data.get("sos_conversation", [])
    user = await db.get_user(message.from_user.id)

    conversation.append({"role": "user", "content": message.text})
    response = await gpt.sos_response(user, conversation)
    conversation.append({"role": "assistant", "content": response})

    await state.update_data(sos_conversation=conversation)

    end_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Закончить SOS")]],
        resize_keyboard=True,
    )
    await message.answer(response, reply_markup=end_kb)


# ──────────────────────────────────────────────
# Вечерняя рефлексия (incoming from scheduler)
# ──────────────────────────────────────────────

async def send_evening_message(user_id: int):
    try:
        user = await db.get_user(user_id)
        if not user:
            return
        today_meals = await db.get_today_meals(user_id, user["timezone"] or "Europe/Moscow")
        week_meals = await db.get_week_meals(user_id)
        week_sos = await db.get_week_sos(user_id)

        response = await gpt.evening_reflection_start(user, today_meals, week_meals, week_sos)
        await db.save_evening_log(user_id, None, response)
        await db.set_evening_pending(user_id, True)

        await bot.send_message(user_id, response)
    except Exception as e:
        logger.error(f"Evening message error for {user_id}: {e}")


# ──────────────────────────────────────────────
# Еженедельный отчёт (from scheduler)
# ──────────────────────────────────────────────

async def send_weekly_report(user_id: int):
    try:
        user = await db.get_user(user_id)
        if not user:
            return
        week_meals = await db.get_week_meals(user_id)
        week_sos = await db.get_week_sos(user_id)
        week_evenings = await db.get_week_evenings(user_id)

        report = await gpt.weekly_report(user, week_meals, week_sos, week_evenings)
        await bot.send_message(user_id, f"📋 Твой недельный разбор:\n\n{report}")
    except Exception as e:
        logger.error(f"Weekly report error for {user_id}: {e}")


# ──────────────────────────────────────────────
# Scheduler helpers
# ──────────────────────────────────────────────

def schedule_evening_for_user(user_id: int, hour: int, minute: int, tz_name: str = "Europe/Moscow"):
    job_id = f"evening_{user_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        send_evening_message,
        CronTrigger(hour=hour, minute=minute, timezone=tz_name),
        args=[user_id],
        id=job_id,
        replace_existing=True,
    )


def schedule_weekly_for_user(user_id: int, hour: int, minute: int, tz_name: str = "Europe/Moscow"):
    job_id = f"weekly_{user_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        send_weekly_report,
        CronTrigger(day_of_week="sun", hour=hour, minute=minute, timezone=tz_name),
        args=[user_id],
        id=job_id,
        replace_existing=True,
    )


async def restore_schedules():
    """Restore scheduler jobs for all existing users after bot restart."""
    users = await db.get_all_users_with_evening()
    for user in users:
        tz = user["timezone"] or "Europe/Moscow"
        schedule_evening_for_user(user["id"], user["evening_hour"], user["evening_minute"], tz)
        schedule_weekly_for_user(user["id"], user["evening_hour"], user["evening_minute"], tz)
    logger.info(f"Restored schedules for {len(users)} users")


# ──────────────────────────────────────────────
# Fallback — handles evening replies + unknown text
# ──────────────────────────────────────────────

@router.message()
async def fallback(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return  # Already in some FSM flow, skip

    # Check if we're waiting for evening reply
    user = await db.get_user(message.from_user.id)
    if user and user["evening_pending"]:
        tz = user["timezone"] or "Europe/Moscow"
        today_meals = await db.get_today_meals(message.from_user.id, tz)
        week_meals = await db.get_week_meals(message.from_user.id)

        response = await gpt.evening_reflection_reply(
            user, today_meals, week_meals, message.text
        )
        await db.save_evening_log(message.from_user.id, message.text, response)
        await db.set_evening_pending(message.from_user.id, False)

        await message.answer(response, reply_markup=MAIN_MENU)
        return

    await message.answer(
        "Используй кнопки ниже 👇",
        reply_markup=MAIN_MENU,
    )


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

async def main():
    await db.init_db(config.DATABASE_URL)
    logger.info("Database initialized")

    scheduler.start()
    await restore_schedules()
    logger.info("Scheduler started")

    try:
        await dp.start_polling(bot)
    finally:
        await db.close_db()
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
