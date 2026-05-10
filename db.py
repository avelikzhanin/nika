import asyncpg
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

pool: asyncpg.Pool = None


async def init_db(database_url: str):
    global pool
    pool = await asyncpg.create_pool(database_url)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,
                name TEXT,
                concern TEXT,
                timezone TEXT DEFAULT 'Europe/Moscow',
                evening_hour INT DEFAULT 20,
                evening_minute INT DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                onboarding_done BOOLEAN DEFAULT FALSE,
                evening_pending BOOLEAN DEFAULT FALSE
            );

            CREATE TABLE IF NOT EXISTS meal_logs (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(id),
                mood TEXT,
                meal_text TEXT,
                is_hungry BOOLEAN,
                gpt_response TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS sos_sessions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(id),
                trigger_text TEXT,
                gpt_dialogue TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS evening_logs (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(id),
                user_text TEXT,
                gpt_response TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)


async def close_db():
    global pool
    if pool:
        await pool.close()


# === Users ===

async def create_user(user_id: int, name: str):
    await pool.execute(
        "INSERT INTO users (id, name) VALUES ($1, $2) ON CONFLICT (id) DO UPDATE SET name = $2",
        user_id, name,
    )


async def update_user_concern(user_id: int, concern: str):
    await pool.execute(
        "UPDATE users SET concern = $1 WHERE id = $2", concern, user_id
    )


async def update_user_timezone(user_id: int, timezone: str):
    await pool.execute(
        "UPDATE users SET timezone = $1 WHERE id = $2", timezone, user_id
    )


async def update_user_evening_time(user_id: int, hour: int, minute: int):
    await pool.execute(
        "UPDATE users SET evening_hour = $1, evening_minute = $2, onboarding_done = TRUE WHERE id = $3",
        hour, minute, user_id,
    )


async def get_user(user_id: int):
    return await pool.fetchrow("SELECT * FROM users WHERE id = $1", user_id)


async def get_all_users_with_evening():
    return await pool.fetch(
        "SELECT id, name, concern, timezone, evening_hour, evening_minute "
        "FROM users WHERE onboarding_done = TRUE"
    )


# === Meal Logs ===

async def save_meal_log(user_id: int, mood: str, meal_text: str, is_hungry: bool, gpt_response: str):
    await pool.execute(
        """INSERT INTO meal_logs (user_id, mood, meal_text, is_hungry, gpt_response)
           VALUES ($1, $2, $3, $4, $5)""",
        user_id, mood, meal_text, is_hungry, gpt_response,
    )


async def get_today_meals(user_id: int, tz_name: str = "Europe/Moscow"):
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_local.astimezone(ZoneInfo("UTC"))
    return await pool.fetch(
        "SELECT * FROM meal_logs WHERE user_id = $1 AND created_at >= $2 ORDER BY created_at",
        user_id, today_start_utc,
    )


async def get_week_meals(user_id: int):
    week_ago = datetime.utcnow() - timedelta(days=7)
    return await pool.fetch(
        "SELECT * FROM meal_logs WHERE user_id = $1 AND created_at >= $2 ORDER BY created_at",
        user_id, week_ago,
    )


async def get_week_sos(user_id: int):
    week_ago = datetime.utcnow() - timedelta(days=7)
    return await pool.fetch(
        "SELECT * FROM sos_sessions WHERE user_id = $1 AND created_at >= $2 ORDER BY created_at",
        user_id, week_ago,
    )


async def get_week_evenings(user_id: int):
    week_ago = datetime.utcnow() - timedelta(days=7)
    return await pool.fetch(
        "SELECT * FROM evening_logs WHERE user_id = $1 AND created_at >= $2 ORDER BY created_at",
        user_id, week_ago,
    )


# === SOS ===

async def save_sos_session(user_id: int, trigger_text: str, gpt_dialogue: str):
    await pool.execute(
        "INSERT INTO sos_sessions (user_id, trigger_text, gpt_dialogue) VALUES ($1, $2, $3)",
        user_id, trigger_text, gpt_dialogue,
    )


# === Evening ===

async def save_evening_log(user_id: int, user_text: str, gpt_response: str):
    await pool.execute(
        "INSERT INTO evening_logs (user_id, user_text, gpt_response) VALUES ($1, $2, $3)",
        user_id, user_text, gpt_response,
    )


async def set_evening_pending(user_id: int, pending: bool):
    await pool.execute(
        "UPDATE users SET evening_pending = $1 WHERE id = $2", pending, user_id
    )
