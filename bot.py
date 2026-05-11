"""
Ника — main bot entrypoint.

Architecture notes:
  • aiogram 3 router with a single Router instance.
  • FSM groups: Onboarding (multi-step), MealLog, SOSDialog, Settings.
  • Per-user APScheduler jobs for meal reminders / evening reflection /
    weekly report / daily energy check / micro-insight check.
  • restore_schedules() on boot recreates jobs from users table.
  • evening_pending flag in users gates whether the next free text from
    that user is treated as an evening reflection reply (3x/2x) or
    a full day recap (1x).
  • The bottom-of-router fallback handler is intentional and must remain
    LAST — it catches evening replies and unknown text.
  • Texts and labels live in texts.py. Promts live in prompts.py.
  • Long-running GPT-side fact extraction is fire-and-forget via
    asyncio.create_task.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import db
import gpt
import texts as T

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler()


# ──────────────────────────────────────────────
# FSM States
# ──────────────────────────────────────────────

class Onboarding(StatesGroup):
    started = State()
    name = State()
    goal = State()
    gender = State()
    age = State()
    height = State()
    weight = State()
    bmi_review = State()             # low-BMI confirmation step (conditional)
    activity = State()
    training = State()
    concerns = State()
    frequency = State()
    breakfast_time = State()
    lunch_time = State()
    dinner_time = State()
    evening_time_1x = State()        # for evening_only mode
    timezone = State()
    final_agreed = State()
    disclaimer = State()


class MealLog(StatesGroup):
    food = State()
    photo_confirm = State()
    trigger = State()       # Q1: why did you eat?
    after_state = State()   # Q2: how do you feel now? (emotional triggers only)


class SOSDialog(StatesGroup):
    chatting = State()


class Settings(StatesGroup):
    menu = State()
    change_goal = State()
    change_frequency = State()
    change_timezone = State()
    change_breakfast = State()
    change_lunch = State()
    change_dinner = State()
    change_evening_1x = State()


# ──────────────────────────────────────────────
# Keyboards
# ──────────────────────────────────────────────

def kb_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=T.BTN_RECORD_MEAL)],
            [KeyboardButton(text=T.BTN_SOS)],
            [KeyboardButton(text=T.BTN_SETTINGS)],
        ],
        resize_keyboard=True,
    )


def kb_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T.STEP0_BUTTON, callback_data="onb_start")]
    ])


def kb_goals() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T.GOAL_LABELS[g])] for g in T.GOAL_ORDER],
        resize_keyboard=True,
    )


def kb_gender() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=T.GENDER_FEMALE_LABEL),
             KeyboardButton(text=T.GENDER_MALE_LABEL)],
            [KeyboardButton(text=T.BTN_SKIP)],
        ],
        resize_keyboard=True,
    )


def kb_skip() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T.BTN_SKIP)]],
        resize_keyboard=True,
    )


def kb_activity() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T.ACTIVITY_LABELS[a])]
                  for a in [T.ACTIVITY_SITTING, T.ACTIVITY_MIXED,
                            T.ACTIVITY_ON_FEET, T.ACTIVITY_PHYSICAL]],
        resize_keyboard=True,
    )


def kb_training() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T.TRAINING_LABELS[t])]
                  for t in [T.TRAINING_REGULAR, T.TRAINING_SOMETIMES,
                            T.TRAINING_RARE, T.TRAINING_NONE]],
        resize_keyboard=True,
    )


def kb_concerns(selected: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for i, label in enumerate(T.CONCERNS):
        prefix = "✅ " if i in selected else "◻️ "
        rows.append([InlineKeyboardButton(
            text=f"{prefix}{label}", callback_data=f"c_toggle_{i}",
        )])
    rows.append([InlineKeyboardButton(text=T.CONCERNS_DONE, callback_data="c_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_frequency() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T.FREQ_LABELS[f])]
                  for f in [T.FREQ_EACH_MEAL, T.FREQ_MORNING_EVENING,
                            T.FREQ_EVENING_ONLY, T.FREQ_OFF]],
        resize_keyboard=True,
    )


def kb_timezone() -> ReplyKeyboardMarkup:
    rows = []
    labels = list(T.TZ_PRESETS.keys())
    for i in range(0, len(labels), 2):
        rows.append([KeyboardButton(text=l) for l in labels[i:i + 2]])
    rows.append([KeyboardButton(text=T.TZ_OTHER_LABEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def kb_final_agree() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T.STEP8_BUTTON, callback_data="onb_agree")]
    ])


def kb_disclaimer_ok() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T.STEP9_BUTTON, callback_data="disclaimer_ok")]
    ])


def kb_bmi_review() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T.LOW_BMI_BTN_DISCUSS, callback_data="bmi_discuss")],
        [InlineKeyboardButton(text=T.LOW_BMI_BTN_CHANGE_GOAL, callback_data="bmi_change")],
        [InlineKeyboardButton(text=T.LOW_BMI_BTN_OK, callback_data="bmi_ok")],
    ])


def kb_triggers(gender: str | None) -> ReplyKeyboardMarkup:
    """Q1 — six possible meal triggers, gender-aware labels."""
    options = T.trigger_options(gender)
    rows = [[KeyboardButton(text=o)] for o in options]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def kb_after_state(trigger: str, gender: str | None) -> ReplyKeyboardMarkup:
    """Q2 — four after-state options. 'didn't help' label depends on trigger+gender."""
    options = T.after_state_options(trigger, gender)
    rows = [[KeyboardButton(text=label)] for _, label in options]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def kb_photo_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T.PHOTO_CONFIRM_OK, callback_data="photo_ok")],
        [InlineKeyboardButton(text=T.PHOTO_CONFIRM_FIX, callback_data="photo_fix")],
    ])


def kb_sos_end() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T.BTN_SOS_END)]],
        resize_keyboard=True,
    )


def kb_energy() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T.ENERGY_LABELS[i], callback_data=f"energy_{i}")
         for i in range(1, 6)]
    ])


def kb_settings() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=T.SETTING_GOAL)],
            [KeyboardButton(text=T.SETTING_TIME)],
            [KeyboardButton(text=T.SETTING_FREQ)],
            [KeyboardButton(text=T.SETTING_TZ)],
            [KeyboardButton(text="↩️ Назад")],
        ],
        resize_keyboard=True,
    )


# ──────────────────────────────────────────────
# Utility parsers
# ──────────────────────────────────────────────

def parse_time(text: str) -> dt_time:
    text = text.strip().replace(".", ":").replace("-", ":").replace(" ", ":")
    parts = text.split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("out of range")
    return dt_time(h, m)


def resolve_timezone(text: str) -> str | None:
    """Resolve a user-typed city name or preset label to an IANA timezone."""
    text = text.strip()
    if text in T.TZ_PRESETS:
        return T.TZ_PRESETS[text]
    # Try direct IANA
    try:
        ZoneInfo(text)
        return text
    except Exception:
        pass
    # City lookup
    lower = text.lower().strip(" ,.")
    if lower in T.CITY_TO_TZ:
        return T.CITY_TO_TZ[lower]
    return None


def compute_bmi(weight_kg: float, height_cm: int) -> float:
    h = height_cm / 100.0
    if h <= 0:
        return 0
    return weight_kg / (h * h)


# ──────────────────────────────────────────────
# /start — entry point
# ──────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if user and user["onboarding_done"]:
        # Legacy user: completed the OLD onboarding but has no goal set yet.
        # Run a shortened migration (goal → params → freq → meal times) and
        # skip parts they already filled (name, timezone, disclaimer).
        if user["goal"] is None:
            await db.touch_last_active(user["id"])
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=T.LEGACY_MIGRATION_BUTTON,
                                      callback_data="legacy_start")]
            ])
            await message.answer(T.LEGACY_MIGRATION_INTRO, reply_markup=kb)
            return
        await db.touch_last_active(user["id"])
        await message.answer("С возвращением 🤍", reply_markup=kb_main_menu())
        return
    await message.answer(T.STEP0_GREETING, reply_markup=kb_start())
    await state.set_state(Onboarding.started)


@router.callback_query(F.data == "onb_start")
async def cb_onb_start(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await cq.message.answer(T.STEP1_ASK_NAME, reply_markup=ReplyKeyboardRemove())
    await state.set_state(Onboarding.name)


@router.callback_query(F.data == "legacy_start")
async def cb_legacy_start(cq: CallbackQuery, state: FSMContext):
    """Start the legacy migration flow — skip name/timezone/concerns/disclaimer."""
    await cq.answer()
    await state.update_data(legacy=True)
    await cq.message.answer(T.STEP2_ASK_GOAL, reply_markup=kb_goals())
    await state.set_state(Onboarding.goal)


# ──────────────────────────────────────────────
# Onboarding — sequential states
# ──────────────────────────────────────────────

@router.message(Onboarding.name)
async def onb_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer(T.STEP1_ASK_NAME)
        return
    await db.create_user(message.from_user.id, name)
    await state.update_data(name=name)
    await message.answer(T.step1_name_ack(name))
    await message.answer(T.STEP2_ASK_GOAL, reply_markup=kb_goals())
    await state.set_state(Onboarding.goal)


@router.message(Onboarding.goal)
async def onb_goal(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    goal_key = T.GOAL_BY_LABEL.get(text)
    if not goal_key:
        await message.answer("Выбери одну из кнопок ниже 👇", reply_markup=kb_goals())
        return
    await db.update_user_field(message.from_user.id, "goal", goal_key)
    await message.answer(T.GOAL_CONFIRMATIONS[goal_key])
    await message.answer(T.STEP3_PARAMS_INTRO)
    await message.answer(T.STEP3_ASK_GENDER, reply_markup=kb_gender())
    await state.set_state(Onboarding.gender)


@router.message(Onboarding.gender)
async def onb_gender(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    value = None
    if text == T.GENDER_FEMALE_LABEL:
        value = "female"
    elif text == T.GENDER_MALE_LABEL:
        value = "male"
    elif text == T.BTN_SKIP:
        value = None
    else:
        await message.answer("Выбери одну из кнопок 👇", reply_markup=kb_gender())
        return
    if value:
        await db.update_user_field(message.from_user.id, "gender", value)
    await message.answer(T.STEP3_ASK_AGE, reply_markup=kb_skip())
    await state.set_state(Onboarding.age)


@router.message(Onboarding.age)
async def onb_age(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text != T.BTN_SKIP:
        try:
            age = int(text)
            if 10 <= age <= 100:
                await db.update_user_field(message.from_user.id, "age", age)
            else:
                await message.answer(T.BAD_NUMBER)
                return
        except ValueError:
            await message.answer(T.BAD_NUMBER)
            return
    await message.answer(T.STEP3_ASK_HEIGHT, reply_markup=kb_skip())
    await state.set_state(Onboarding.height)


@router.message(Onboarding.height)
async def onb_height(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text != T.BTN_SKIP:
        try:
            h = int(text)
            if 100 <= h <= 230:
                await db.update_user_field(message.from_user.id, "height_cm", h)
            else:
                await message.answer(T.BAD_NUMBER)
                return
        except ValueError:
            await message.answer(T.BAD_NUMBER)
            return
    await message.answer(T.STEP3_ASK_WEIGHT, reply_markup=kb_skip())
    await state.set_state(Onboarding.weight)


@router.message(Onboarding.weight)
async def onb_weight(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    weight_value: float | None = None
    if text != T.BTN_SKIP:
        try:
            w = float(text.replace(",", "."))
            if 30 <= w <= 300:
                await db.update_user_field(message.from_user.id, "weight_kg", w)
                weight_value = w
            else:
                await message.answer(T.BAD_NUMBER)
                return
        except ValueError:
            await message.answer(T.BAD_NUMBER)
            return

    # BMI safety check — only for goal=lose_weight, when both height & weight known
    user = await db.get_user(message.from_user.id)
    if (weight_value is not None
            and user["height_cm"]
            and user["goal"] == T.GOAL_LOSE_WEIGHT):
        bmi = compute_bmi(weight_value, user["height_cm"])
        if bmi < 18.5:
            await message.answer(T.low_bmi_warning(user["name"] or ""), reply_markup=kb_bmi_review())
            await state.set_state(Onboarding.bmi_review)
            return

    await _ask_activity(message, state)


async def _ask_activity(message: Message, state: FSMContext):
    await message.answer(T.STEP3_ASK_ACTIVITY, reply_markup=kb_activity())
    await state.set_state(Onboarding.activity)


@router.callback_query(F.data.in_({"bmi_discuss", "bmi_change", "bmi_ok"}))
async def cb_bmi_review(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    action = cq.data
    if action == "bmi_change":
        await cq.message.answer(T.STEP2_ASK_GOAL, reply_markup=kb_goals())
        await state.set_state(Onboarding.goal)
        return
    # In both "discuss" and "ok" cases — continue onboarding. Ника will pay
    # extra attention to risk signals via safety_check on every later message.
    await _ask_activity(cq.message, state)


@router.message(Onboarding.activity)
async def onb_activity(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    key = T.ACTIVITY_BY_LABEL.get(text)
    if not key:
        await message.answer("Выбери одну из кнопок 👇", reply_markup=kb_activity())
        return
    await db.update_user_field(message.from_user.id, "activity_level", key)
    await message.answer(T.STEP3_ASK_TRAINING, reply_markup=kb_training())
    await state.set_state(Onboarding.training)


@router.message(Onboarding.training)
async def onb_training(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    key = T.TRAINING_BY_LABEL.get(text)
    if not key:
        await message.answer("Выбери одну из кнопок 👇", reply_markup=kb_training())
        return
    await db.update_user_field(message.from_user.id, "training_frequency", key)

    data = await state.get_data()
    if data.get("legacy"):
        # Legacy migration: skip concerns step.
        await message.answer(T.STEP5_ASK_FREQUENCY, reply_markup=kb_frequency())
        await state.set_state(Onboarding.frequency)
        return

    await state.update_data(concerns_selected=set())
    await message.answer(
        T.STEP4_ASK_CONCERNS,
        reply_markup=kb_concerns(set()),
    )
    await state.set_state(Onboarding.concerns)


@router.callback_query(Onboarding.concerns, F.data.startswith("c_toggle_"))
async def cb_concern_toggle(cq: CallbackQuery, state: FSMContext):
    idx = int(cq.data.split("_")[-1])
    data = await state.get_data()
    selected: set[int] = set(data.get("concerns_selected", set()))
    if idx in selected:
        selected.remove(idx)
    else:
        selected.add(idx)
    await state.update_data(concerns_selected=selected)
    await cq.message.edit_reply_markup(reply_markup=kb_concerns(selected))
    await cq.answer()


@router.callback_query(Onboarding.concerns, F.data == "c_done")
async def cb_concerns_done(cq: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected: set[int] = set(data.get("concerns_selected", set()))
    user_id = cq.from_user.id
    await db.clear_concerns(user_id)
    for idx in selected:
        await db.add_concern(user_id, T.CONCERNS[idx])
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.answer()
    await cq.message.answer(T.STEP5_ASK_FREQUENCY, reply_markup=kb_frequency())
    await state.set_state(Onboarding.frequency)


@router.message(Onboarding.frequency)
async def onb_frequency(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    key = T.FREQ_BY_LABEL.get(text)
    if not key:
        await message.answer("Выбери одну из кнопок 👇", reply_markup=kb_frequency())
        return
    await db.update_user_field(message.from_user.id, "reminder_frequency", key)

    if key == T.FREQ_OFF:
        await message.answer(T.STEP6_NO_REMINDERS, reply_markup=ReplyKeyboardRemove())
        await _go_to_timezone(message, state)
        return

    await message.answer(T.STEP6_PREAMBLE_WITH_REMINDERS, reply_markup=ReplyKeyboardRemove())

    if key == T.FREQ_EACH_MEAL:
        await message.answer(T.STEP6_ASK_BREAKFAST)
        await state.set_state(Onboarding.breakfast_time)
    elif key == T.FREQ_MORNING_EVENING:
        await message.answer(T.STEP6_ASK_BREAKFAST)
        await state.set_state(Onboarding.breakfast_time)
    elif key == T.FREQ_EVENING_ONLY:
        await message.answer(T.STEP6_ASK_EVENING_1X)
        await state.set_state(Onboarding.evening_time_1x)


@router.message(Onboarding.breakfast_time)
async def onb_breakfast(message: Message, state: FSMContext):
    text = (message.text or "").strip().lower()
    if text == T.SKIP_BREAKFAST:
        await db.update_user_field(message.from_user.id, "breakfast_time", None)
    else:
        try:
            t = parse_time(text)
        except Exception:
            await message.answer(T.BAD_TIME_FORMAT)
            return
        await db.update_user_field(message.from_user.id, "breakfast_time", t)

    user = await db.get_user(message.from_user.id)
    if user["reminder_frequency"] == T.FREQ_EACH_MEAL:
        await message.answer(T.STEP6_ASK_LUNCH)
        await state.set_state(Onboarding.lunch_time)
    else:  # morning_evening
        await message.answer(T.STEP6_ASK_DINNER_2X)
        await state.set_state(Onboarding.dinner_time)


@router.message(Onboarding.lunch_time)
async def onb_lunch(message: Message, state: FSMContext):
    try:
        t = parse_time(message.text or "")
    except Exception:
        await message.answer(T.BAD_TIME_FORMAT)
        return
    await db.update_user_field(message.from_user.id, "lunch_time", t)
    await message.answer(T.STEP6_ASK_DINNER_3X)
    await state.set_state(Onboarding.dinner_time)


@router.message(Onboarding.dinner_time)
async def onb_dinner(message: Message, state: FSMContext):
    try:
        t = parse_time(message.text or "")
    except Exception:
        await message.answer(T.BAD_TIME_FORMAT)
        return
    await db.update_user_field(message.from_user.id, "dinner_time", t)
    await _go_to_timezone(message, state)


@router.message(Onboarding.evening_time_1x)
async def onb_evening_1x(message: Message, state: FSMContext):
    try:
        t = parse_time(message.text or "")
    except Exception:
        await message.answer(T.BAD_TIME_FORMAT)
        return
    await db.update_user_field(message.from_user.id, "evening_message_time", t)
    await _go_to_timezone(message, state)


async def _go_to_timezone_or_finish_legacy(message: Message, state: FSMContext):
    """For new users → ask timezone. For legacy users → finalize immediately."""
    data = await state.get_data()
    if data.get("legacy"):
        user_id = message.from_user.id
        user = await db.get_user(user_id)
        schedule_user_jobs(user)
        await db.touch_last_active(user_id)
        await message.answer(
            T.legacy_migration_done(user["name"] or ""),
            reply_markup=kb_main_menu(),
        )
        await state.clear()
        return
    await message.answer(T.STEP7_ASK_TIMEZONE, reply_markup=kb_timezone())
    await state.set_state(Onboarding.timezone)


# Kept for back-compat in case the name is referenced elsewhere.
_go_to_timezone = _go_to_timezone_or_finish_legacy


@router.message(Onboarding.timezone)
async def onb_timezone(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == T.TZ_OTHER_LABEL:
        await message.answer(
            "Напиши название твоего города или IANA-код (например, Europe/Berlin).",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    tz = resolve_timezone(text)
    if not tz:
        await message.answer(T.UNKNOWN_TIMEZONE)
        return
    await db.update_user_field(message.from_user.id, "timezone", tz)

    user = await db.get_user(message.from_user.id)
    await message.answer(T.step8_final(user["name"] or ""), reply_markup=ReplyKeyboardRemove())
    await message.answer("Жми, когда готова ↓", reply_markup=kb_final_agree())
    await state.set_state(Onboarding.final_agreed)


@router.callback_query(F.data == "onb_agree")
async def cb_onb_agree(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await cq.message.answer(T.STEP9_DISCLAIMER, reply_markup=kb_disclaimer_ok())
    await state.set_state(Onboarding.disclaimer)


@router.callback_query(F.data == "disclaimer_ok")
async def cb_disclaimer_ok(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    user_id = cq.from_user.id
    await db.mark_onboarding_done(user_id)
    await db.touch_last_active(user_id)
    # Schedule all jobs based on the new user settings
    user = await db.get_user(user_id)
    schedule_user_jobs(user)
    await cq.message.answer(T.STEP10_MAIN_MENU, reply_markup=kb_main_menu())
    await state.clear()


# ──────────────────────────────────────────────
# Meal logging — text + photo
# ──────────────────────────────────────────────

@router.message(F.text == T.BTN_RECORD_MEAL)
async def meal_start(message: Message, state: FSMContext):
    await state.clear()
    await db.set_evening_pending(message.from_user.id, False)
    await db.touch_last_active(message.from_user.id)
    await message.answer(T.MEAL_LOG_ASK_FOOD, reply_markup=ReplyKeyboardRemove())
    await state.set_state(MealLog.food)


@router.message(MealLog.food, F.photo)
async def meal_photo(message: Message, state: FSMContext):
    # Download largest photo
    photo = message.photo[-1]
    buf = io.BytesIO()
    await bot.download(photo.file_id, destination=buf)
    buf.seek(0)
    photo_bytes = buf.read()
    description = await gpt.recognize_meal_photo(photo_bytes)
    if not description or "не похоже на еду" in description.lower():
        await message.answer(T.PHOTO_NOT_FOOD)
        return
    await state.update_data(food_text=description, from_photo=True)
    await message.answer(T.photo_confirm(description), reply_markup=kb_photo_confirm())
    await state.set_state(MealLog.photo_confirm)


@router.callback_query(MealLog.photo_confirm, F.data == "photo_ok")
async def cb_photo_ok(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await cq.message.edit_reply_markup(reply_markup=None)
    user = await db.get_user(cq.from_user.id)
    await cq.message.answer(T.MEAL_LOG_ASK_TRIGGER,
                            reply_markup=kb_triggers(user["gender"]))
    await state.set_state(MealLog.trigger)


@router.callback_query(MealLog.photo_confirm, F.data == "photo_fix")
async def cb_photo_fix(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.message.answer("Поправь — напиши, что было на самом деле.")
    await state.set_state(MealLog.food)


@router.message(MealLog.food)
async def meal_food_text(message: Message, state: FSMContext):
    food_text = (message.text or "").strip()
    if not food_text:
        await message.answer(T.MEAL_LOG_ASK_FOOD)
        return
    await state.update_data(food_text=food_text, from_photo=False)
    user = await db.get_user(message.from_user.id)
    await message.answer(T.MEAL_LOG_ASK_TRIGGER, reply_markup=kb_triggers(user["gender"]))
    await state.set_state(MealLog.trigger)


@router.message(MealLog.trigger)
async def meal_trigger(message: Message, state: FSMContext):
    """Q1 handler: resolve trigger, then either finalize or ask Q2."""
    text = (message.text or "").strip()
    user = await db.get_user(message.from_user.id)
    trigger = T.trigger_by_label(text)
    if trigger is None:
        await message.answer("Выбери одну из кнопок 👇", reply_markup=kb_triggers(user["gender"]))
        return

    # Safety classifier on the trigger label + food text
    data = await state.get_data()
    food_text = data.get("food_text", "")
    red_flag = await gpt.safety_check(f"{text}\n{food_text}")
    if red_flag:
        await message.answer(
            T.safety_red_flag_message(user["name"] or ""),
            reply_markup=kb_main_menu(),
        )
        await state.clear()
        return

    # Emotional trigger → ask Q2
    if trigger in T.EMOTIONAL_TRIGGERS:
        await state.update_data(trigger=trigger, trigger_label=text)
        await message.answer(
            T.MEAL_LOG_ASK_AFTER_STATE,
            reply_markup=kb_after_state(trigger, user["gender"]),
        )
        await state.set_state(MealLog.after_state)
        return

    # Non-emotional trigger → finalize immediately
    await _finalize_meal_log(
        message, state, user,
        trigger=trigger, trigger_label=text,
        food_text=food_text, after_state=None,
    )


@router.message(MealLog.after_state)
async def meal_after_state(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    trigger: str = data.get("trigger") or T.TRIGGER_TIRED
    user = await db.get_user(message.from_user.id)

    after_state_key = T.after_state_by_label(text, trigger, user["gender"])
    if after_state_key is None:
        await message.answer(
            "Выбери одну из кнопок 👇",
            reply_markup=kb_after_state(trigger, user["gender"]),
        )
        return

    food_text = data.get("food_text", "")
    trigger_label = data.get("trigger_label", text)
    await _finalize_meal_log(
        message, state, user,
        trigger=trigger, trigger_label=trigger_label,
        food_text=food_text, after_state=after_state_key,
    )


async def _finalize_meal_log(
    message: Message,
    state: FSMContext,
    user,
    *,
    trigger: str,
    trigger_label: str,
    food_text: str,
    after_state: str | None,
):
    """Common finalization for meal logging — runs GPT, saves, returns to main menu."""
    today = await db.get_today_meals(user["id"], user["timezone"] or "Europe/Moscow")
    week = await db.get_week_meals(user["id"])

    # Build the message that goes into MAIN_PROMPT
    parts = [
        f"Еда: {food_text}",
        f"Триггер: {T.TRIGGER_HUMAN.get(trigger, trigger_label)} ({trigger_label})",
    ]
    if after_state:
        parts.append(f"После еды: {T.AFTER_HUMAN.get(after_state, after_state)}")
    combined = "\n".join(parts)
    response = await gpt.meal_checkin_response(user, today, week, combined)

    is_hungry = (trigger == T.TRIGGER_HUNGER)
    await db.save_meal_log(
        user["id"],
        mood=trigger_label,
        meal_text=food_text,
        is_hungry=is_hungry,
        gpt_response=response,
        trigger=trigger,
        after_state=after_state,
    )
    await db.touch_last_active(user["id"])
    asyncio.create_task(gpt.persist_facts(user["id"], combined))

    await message.answer(response, reply_markup=kb_main_menu())
    await state.clear()


# ──────────────────────────────────────────────
# SOS
# ──────────────────────────────────────────────

@router.message(F.text == T.BTN_SOS)
async def sos_start(message: Message, state: FSMContext):
    await state.clear()
    await db.set_evening_pending(message.from_user.id, False)
    await db.set_sos_pending(message.from_user.id, True)
    await db.touch_last_active(message.from_user.id)
    # First reply is fixed — no GPT call to make the soma-first opener consistent
    await state.update_data(
        sos_conversation=[
            {"role": "assistant", "content": T.SOS_FIRST_REPLY},
        ],
        sos_trigger="Нажала SOS",
    )
    await message.answer(T.SOS_FIRST_REPLY, reply_markup=kb_sos_end())
    await state.set_state(SOSDialog.chatting)


@router.message(SOSDialog.chatting, F.text == T.BTN_SOS_END)
async def sos_end(message: Message, state: FSMContext):
    data = await state.get_data()
    conversation = data.get("sos_conversation", [])
    trigger = data.get("sos_trigger", "")
    dialogue_text = "\n".join(
        f"{'Я' if m['role'] == 'user' else 'Ника'}: {m['content']}"
        for m in conversation
    )
    await db.save_sos_session(message.from_user.id, trigger, dialogue_text)
    await db.set_sos_pending(message.from_user.id, False)
    await message.answer(T.SOS_END_MESSAGE, reply_markup=kb_main_menu())
    await state.clear()


@router.message(SOSDialog.chatting)
async def sos_chat(message: Message, state: FSMContext):
    data = await state.get_data()
    conversation = data.get("sos_conversation", [])
    user = await db.get_user(message.from_user.id)

    user_text = message.text or ""
    red_flag = await gpt.safety_check(user_text)
    if red_flag:
        await message.answer(
            T.safety_red_flag_message(user["name"] or ""),
            reply_markup=kb_main_menu(),
        )
        # Persist the partial SOS for context
        dialogue_text = "\n".join(
            f"{'Я' if m['role'] == 'user' else 'Ника'}: {m['content']}"
            for m in conversation
        )
        await db.save_sos_session(user["id"], data.get("sos_trigger", ""), dialogue_text)
        await db.set_sos_pending(user["id"], False)
        await state.clear()
        return

    conversation.append({"role": "user", "content": user_text})
    response = await gpt.sos_response(user, conversation)
    conversation.append({"role": "assistant", "content": response})
    await state.update_data(sos_conversation=conversation)
    # Fire-and-forget fact extraction
    asyncio.create_task(gpt.persist_facts(user["id"], user_text))

    await message.answer(response, reply_markup=kb_sos_end())


# ──────────────────────────────────────────────
# Scheduled jobs — evening reflection, weekly, energy, micro-insight, meal reminders
# ──────────────────────────────────────────────

async def send_meal_reminder(user_id: int, slot: str):
    """Simple nudge after a meal. No GPT call."""
    try:
        user = await db.get_user(user_id)
        if not user or not user["onboarding_done"]:
            return
        slot_label = {"breakfast": "завтрак", "lunch": "обед", "dinner": "ужин"}.get(slot, "приём пищи")
        await bot.send_message(
            user_id,
            f"Лёгкое напоминание: если уже поела ({slot_label}) — запиши, пока свежо в памяти 🍃",
        )
    except Exception as e:
        logger.error(f"send_meal_reminder error for {user_id}: {e}")


async def send_evening_reflection(user_id: int):
    """3x / 2x evening reflection — GPT-generated opener."""
    try:
        user = await db.get_user(user_id)
        if not user or not user["onboarding_done"]:
            return
        if user["reminder_frequency"] == T.FREQ_EVENING_ONLY:
            return  # different scheduler job handles 1x mode
        tz = user["timezone"] or "Europe/Moscow"
        today = await db.get_today_meals(user_id, tz)
        week = await db.get_week_meals(user_id)
        week_sos = await db.get_week_sos(user_id)
        response = await gpt.evening_reflection_start(user, today, week, week_sos)
        await db.save_evening_log(user_id, None, response)
        await db.set_evening_pending(user_id, True)
        await bot.send_message(user_id, response)
        # Energy check for energy-goal users
        if user["goal"] == T.GOAL_ENERGY:
            await asyncio.sleep(1)
            await bot.send_message(user_id, T.ENERGY_CHECK_PROMPT, reply_markup=kb_energy())
    except Exception as e:
        logger.error(f"send_evening_reflection error for {user_id}: {e}")


async def send_day_recap_prompt(user_id: int):
    """1x/day mode — whole-day recap message."""
    try:
        user = await db.get_user(user_id)
        if not user or not user["onboarding_done"]:
            return
        if user["reminder_frequency"] != T.FREQ_EVENING_ONLY:
            return
        await db.set_evening_pending(user_id, True)
        await bot.send_message(user_id, T.EVENING_RECAP_1X)
        if user["goal"] == T.GOAL_ENERGY:
            await asyncio.sleep(1)
            await bot.send_message(user_id, T.ENERGY_CHECK_PROMPT, reply_markup=kb_energy())
    except Exception as e:
        logger.error(f"send_day_recap_prompt error for {user_id}: {e}")


async def send_weekly_report(user_id: int):
    try:
        user = await db.get_user(user_id)
        if not user or not user["onboarding_done"]:
            return
        week_meals = await db.get_week_meals(user_id)
        week_sos = await db.get_week_sos(user_id)
        week_evenings = await db.get_week_evenings(user_id)
        result = await gpt.weekly_report(user, week_meals, week_sos, week_evenings)
        # Persist summary
        week_start = (datetime.utcnow() - timedelta(days=7)).date()
        await db.save_weekly_summary(
            user_id, week_start,
            result.get("pattern"), result.get("suggestion"),
            result.get("text", ""),
        )
        await bot.send_message(user_id, f"📋 Твой недельный разбор:\n\n{result['text']}")
    except Exception as e:
        logger.error(f"send_weekly_report error for {user_id}: {e}")


async def check_micro_insight(user_id: int):
    """Daily check: detect local signals, optionally send a micro-insight."""
    try:
        user = await db.get_user(user_id)
        if not user or not user["onboarding_done"]:
            return
        # Throttle: skip if there was an insight in the last 2 days
        last_at = await db.get_last_micro_insight_at(user_id)
        if last_at and (datetime.utcnow() - last_at.replace(tzinfo=None)) < timedelta(days=2):
            return
        week = await db.get_week_meals(user_id)
        week_sos = await db.get_week_sos(user_id)
        signal = _detect_local_signal(week, week_sos)
        if not signal:
            return
        insight = await gpt.micro_insight(user, signal, week_meals=week)
        if not insight or not insight.strip():
            return
        await db.save_micro_insight(user_id, insight, signal)
        await bot.send_message(user_id, insight)
    except Exception as e:
        logger.error(f"check_micro_insight error for {user_id}: {e}")


def _detect_local_signal(week_meals, week_sos) -> str | None:
    """Heuristic detection of patterns worth pinging about. Returns a short
    Russian description of the signal, or None."""
    if len(week_meals) < 4:
        return None

    # Breakfast skipping streak
    by_day: dict = {}
    for m in week_meals:
        d = m["created_at"].date()
        by_day.setdefault(d, []).append(m)
    no_breakfast_days = 0
    for day_meals in by_day.values():
        has_breakfast = any(
            (m.get("meal_slot") == "breakfast") or
            (m["created_at"].hour < 11) for m in day_meals
        )
        if not has_breakfast:
            no_breakfast_days += 1
    if no_breakfast_days >= 3:
        return f"пропуск завтрака — {no_breakfast_days} дней за неделю"

    # Late dinners (after 22:00)
    late = sum(1 for m in week_meals if m["created_at"].hour >= 22)
    if late >= 3:
        return f"поздние ужины (после 22:00) — {late} раз за неделю"

    # Frequent SOS
    if len(week_sos) >= 3:
        return f"SOS пришёл {len(week_sos)} раз за неделю"

    return None


# ──────────────────────────────────────────────
# Energy check — inline button callbacks
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("energy_"))
async def cb_energy(cq: CallbackQuery):
    score = int(cq.data.split("_")[1])
    await db.save_energy_score(cq.from_user.id, score)
    await cq.answer("Записала 🤍")
    await cq.message.edit_reply_markup(reply_markup=None)


# ──────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────

@router.message(F.text == T.BTN_SETTINGS)
async def settings_open(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(T.SETTINGS_MENU_PROMPT, reply_markup=kb_settings())
    await state.set_state(Settings.menu)


@router.message(Settings.menu, F.text == "↩️ Назад")
async def settings_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(T.STEP10_MAIN_MENU, reply_markup=kb_main_menu())


@router.message(Settings.menu, F.text == T.SETTING_GOAL)
async def settings_goal(message: Message, state: FSMContext):
    await message.answer(T.STEP2_ASK_GOAL, reply_markup=kb_goals())
    await state.set_state(Settings.change_goal)


@router.message(Settings.change_goal)
async def settings_goal_set(message: Message, state: FSMContext):
    key = T.GOAL_BY_LABEL.get((message.text or "").strip())
    if not key:
        await message.answer("Выбери одну из кнопок 👇", reply_markup=kb_goals())
        return
    await db.update_user_field(message.from_user.id, "goal", key)
    user = await db.get_user(message.from_user.id)
    schedule_user_jobs(user)  # energy check may need to be (de)scheduled
    await message.answer("Цель обновила 🤍", reply_markup=kb_main_menu())
    await state.clear()


@router.message(Settings.menu, F.text == T.SETTING_FREQ)
async def settings_freq(message: Message, state: FSMContext):
    await message.answer(T.STEP5_ASK_FREQUENCY, reply_markup=kb_frequency())
    await state.set_state(Settings.change_frequency)


@router.message(Settings.change_frequency)
async def settings_freq_set(message: Message, state: FSMContext):
    key = T.FREQ_BY_LABEL.get((message.text or "").strip())
    if not key:
        await message.answer("Выбери одну из кнопок 👇", reply_markup=kb_frequency())
        return
    await db.update_user_field(message.from_user.id, "reminder_frequency", key)
    # Clear all meal/evening times so the user can re-enter; simplest UX.
    if key == T.FREQ_OFF:
        for f in ("breakfast_time", "lunch_time", "dinner_time", "evening_message_time"):
            await db.update_user_field(message.from_user.id, f, None)
        user = await db.get_user(message.from_user.id)
        schedule_user_jobs(user)
        await message.answer("Готово, напоминания выключила.", reply_markup=kb_main_menu())
        await state.clear()
        return
    if key == T.FREQ_EVENING_ONLY:
        await message.answer(T.STEP6_ASK_EVENING_1X, reply_markup=ReplyKeyboardRemove())
        await state.set_state(Settings.change_evening_1x)
        return
    # 3x / 2x — ask breakfast first
    await message.answer(T.STEP6_ASK_BREAKFAST, reply_markup=ReplyKeyboardRemove())
    await state.set_state(Settings.change_breakfast)


@router.message(Settings.change_breakfast)
async def settings_breakfast(message: Message, state: FSMContext):
    text = (message.text or "").strip().lower()
    if text == T.SKIP_BREAKFAST:
        await db.update_user_field(message.from_user.id, "breakfast_time", None)
    else:
        try:
            t = parse_time(text)
        except Exception:
            await message.answer(T.BAD_TIME_FORMAT)
            return
        await db.update_user_field(message.from_user.id, "breakfast_time", t)
    user = await db.get_user(message.from_user.id)
    if user["reminder_frequency"] == T.FREQ_EACH_MEAL:
        await message.answer(T.STEP6_ASK_LUNCH)
        await state.set_state(Settings.change_lunch)
    else:
        await message.answer(T.STEP6_ASK_DINNER_2X)
        await state.set_state(Settings.change_dinner)


@router.message(Settings.change_lunch)
async def settings_lunch(message: Message, state: FSMContext):
    try:
        t = parse_time(message.text or "")
    except Exception:
        await message.answer(T.BAD_TIME_FORMAT)
        return
    await db.update_user_field(message.from_user.id, "lunch_time", t)
    await message.answer(T.STEP6_ASK_DINNER_3X)
    await state.set_state(Settings.change_dinner)


@router.message(Settings.change_dinner)
async def settings_dinner(message: Message, state: FSMContext):
    try:
        t = parse_time(message.text or "")
    except Exception:
        await message.answer(T.BAD_TIME_FORMAT)
        return
    await db.update_user_field(message.from_user.id, "dinner_time", t)
    user = await db.get_user(message.from_user.id)
    schedule_user_jobs(user)
    await message.answer("Готово, время обновила 🤍", reply_markup=kb_main_menu())
    await state.clear()


@router.message(Settings.change_evening_1x)
async def settings_evening_1x(message: Message, state: FSMContext):
    try:
        t = parse_time(message.text or "")
    except Exception:
        await message.answer(T.BAD_TIME_FORMAT)
        return
    await db.update_user_field(message.from_user.id, "evening_message_time", t)
    user = await db.get_user(message.from_user.id)
    schedule_user_jobs(user)
    await message.answer("Готово, время обновила 🤍", reply_markup=kb_main_menu())
    await state.clear()


@router.message(Settings.menu, F.text == T.SETTING_TIME)
async def settings_time(message: Message, state: FSMContext):
    user = await db.get_user(message.from_user.id)
    freq = user["reminder_frequency"]
    if freq == T.FREQ_OFF or not freq:
        await message.answer(
            "Сейчас у тебя выключены напоминания — поменяй сначала частоту в этом меню.",
            reply_markup=kb_settings(),
        )
        return
    if freq == T.FREQ_EVENING_ONLY:
        await message.answer(T.STEP6_ASK_EVENING_1X, reply_markup=ReplyKeyboardRemove())
        await state.set_state(Settings.change_evening_1x)
    else:
        await message.answer(T.STEP6_ASK_BREAKFAST, reply_markup=ReplyKeyboardRemove())
        await state.set_state(Settings.change_breakfast)


@router.message(Settings.menu, F.text == T.SETTING_TZ)
async def settings_tz(message: Message, state: FSMContext):
    await message.answer(T.STEP7_ASK_TIMEZONE, reply_markup=kb_timezone())
    await state.set_state(Settings.change_timezone)


@router.message(Settings.change_timezone)
async def settings_tz_set(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == T.TZ_OTHER_LABEL:
        await message.answer("Напиши название города или IANA-код.",
                             reply_markup=ReplyKeyboardRemove())
        return
    tz = resolve_timezone(text)
    if not tz:
        await message.answer(T.UNKNOWN_TIMEZONE)
        return
    await db.update_user_field(message.from_user.id, "timezone", tz)
    user = await db.get_user(message.from_user.id)
    schedule_user_jobs(user)
    await message.answer("Часовой пояс обновила 🤍", reply_markup=kb_main_menu())
    await state.clear()


# ──────────────────────────────────────────────
# Scheduler helpers
# ──────────────────────────────────────────────

def _remove_user_jobs(user_id: int):
    prefixes = [
        "rmb", "rml", "rmd",        # meal reminders
        "evening", "recap1x",       # evening flows
        "weekly", "micro",          # weekly + insight
    ]
    for p in prefixes:
        jid = f"{p}_{user_id}"
        if scheduler.get_job(jid):
            scheduler.remove_job(jid)


def _add_minutes(t: dt_time, minutes: int) -> tuple[int, int]:
    total = t.hour * 60 + t.minute + minutes
    total %= 24 * 60
    return total // 60, total % 60


def schedule_user_jobs(user):
    """(Re)schedule all per-user jobs based on current settings."""
    user_id = user["id"]
    _remove_user_jobs(user_id)

    tz = user["timezone"] or "Europe/Moscow"
    freq = user["reminder_frequency"]

    # Determine "evening anchor" time — used for weekly report and as fallback
    evening_anchor: dt_time | None = (
        user["evening_message_time"]
        or user["dinner_time"]
        or dt_time(20, 0)
    )

    if freq == T.FREQ_EACH_MEAL:
        for t, slot, prefix in [
            (user["breakfast_time"], "breakfast", "rmb"),
            (user["lunch_time"], "lunch", "rml"),
        ]:
            if t:
                h, m = _add_minutes(t, 30)
                scheduler.add_job(
                    send_meal_reminder,
                    CronTrigger(hour=h, minute=m, timezone=tz),
                    args=[user_id, slot], id=f"{prefix}_{user_id}",
                    replace_existing=True,
                )
        if user["dinner_time"]:
            h, m = _add_minutes(user["dinner_time"], 30)
            scheduler.add_job(
                send_evening_reflection,
                CronTrigger(hour=h, minute=m, timezone=tz),
                args=[user_id], id=f"evening_{user_id}", replace_existing=True,
            )
    elif freq == T.FREQ_MORNING_EVENING:
        if user["breakfast_time"]:
            h, m = _add_minutes(user["breakfast_time"], 30)
            scheduler.add_job(
                send_meal_reminder,
                CronTrigger(hour=h, minute=m, timezone=tz),
                args=[user_id, "breakfast"], id=f"rmb_{user_id}",
                replace_existing=True,
            )
        if user["dinner_time"]:
            h, m = _add_minutes(user["dinner_time"], 30)
            scheduler.add_job(
                send_evening_reflection,
                CronTrigger(hour=h, minute=m, timezone=tz),
                args=[user_id], id=f"evening_{user_id}", replace_existing=True,
            )
    elif freq == T.FREQ_EVENING_ONLY:
        t = user["evening_message_time"] or dt_time(21, 0)
        scheduler.add_job(
            send_day_recap_prompt,
            CronTrigger(hour=t.hour, minute=t.minute, timezone=tz),
            args=[user_id], id=f"recap1x_{user_id}", replace_existing=True,
        )

    # Weekly — always
    scheduler.add_job(
        send_weekly_report,
        CronTrigger(day_of_week="sun", hour=evening_anchor.hour,
                    minute=evening_anchor.minute, timezone=tz),
        args=[user_id], id=f"weekly_{user_id}", replace_existing=True,
    )

    # Micro-insight check — once a day in the afternoon
    scheduler.add_job(
        check_micro_insight,
        CronTrigger(hour=15, minute=0, timezone=tz),
        args=[user_id], id=f"micro_{user_id}", replace_existing=True,
    )


async def restore_schedules():
    users = await db.get_all_onboarded_users()
    for u in users:
        schedule_user_jobs(u)
    logger.info(f"Restored schedules for {len(users)} users")


# ──────────────────────────────────────────────
# Fallback — evening replies, day recap, long-silence return, unknown
# ──────────────────────────────────────────────

LONG_SILENCE_THRESHOLD = timedelta(days=3)


@router.message()
async def fallback(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return  # Already in some FSM flow, skip

    user = await db.get_user(message.from_user.id)
    if not user or not user["onboarding_done"]:
        # Not onboarded yet — point back to /start
        await message.answer("Напиши /start, чтобы мы познакомились 🌿")
        return

    user_text = message.text or ""
    red_flag = await gpt.safety_check(user_text)
    if red_flag:
        await message.answer(
            T.safety_red_flag_message(user["name"] or ""),
            reply_markup=kb_main_menu(),
        )
        await db.touch_last_active(user["id"])
        return

    # Long-silence return
    last_active = user["last_active_at"]
    if last_active:
        # asyncpg returns timestamptz as aware datetime
        delta = datetime.now(last_active.tzinfo) - last_active
        if delta >= LONG_SILENCE_THRESHOLD and not user["evening_pending"]:
            await message.answer(T.LONG_SILENCE_RETURN, reply_markup=kb_main_menu())
            await db.touch_last_active(user["id"])
            # We let the next free message be treated normally
            return

    # Evening reply or day recap?
    if user["evening_pending"]:
        tz = user["timezone"] or "Europe/Moscow"
        if user["reminder_frequency"] == T.FREQ_EVENING_ONLY:
            # Parse whole-day recap and store as multiple meal logs
            parsed = await gpt.parse_day_recap(user_text)
            now_local = datetime.now(ZoneInfo(tz))
            slot_default_hours = {"breakfast": 9, "lunch": 13, "dinner": 19, "snack": 16}
            for entry in parsed:
                slot = entry.get("slot") or "unknown"
                text = entry.get("text") or ""
                mood = entry.get("mood")
                if not text:
                    continue
                hour = slot_default_hours.get(slot)
                if hour is not None:
                    local_ts = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
                    created_at = local_ts.astimezone(ZoneInfo("UTC"))
                else:
                    created_at = None
                await db.save_meal_log(
                    user["id"], mood=mood, meal_text=text,
                    is_hungry=None, gpt_response=None,
                    meal_slot=slot if slot in {"breakfast", "lunch", "dinner", "snack"} else None,
                    created_at=created_at,
                )
            today = await db.get_today_meals(user["id"], tz)
            week = await db.get_week_meals(user["id"])
            response = await gpt.day_recap_response(user, today, week, user_text)
        else:
            today = await db.get_today_meals(user["id"], tz)
            week = await db.get_week_meals(user["id"])
            response = await gpt.evening_reflection_reply(user, today, week, user_text)

        await db.save_evening_log(user["id"], user_text, response)
        await db.set_evening_pending(user["id"], False)
        await db.touch_last_active(user["id"])
        asyncio.create_task(gpt.persist_facts(user["id"], user_text))
        await message.answer(response, reply_markup=kb_main_menu())
        return

    await db.touch_last_active(user["id"])
    await message.answer(T.USE_BUTTONS_HINT, reply_markup=kb_main_menu())


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
