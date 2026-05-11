import asyncpg
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo

pool: asyncpg.Pool = None


# ──────────────────────────────────────────────
# Init / shutdown
# ──────────────────────────────────────────────

async def init_db(database_url: str):
    """Create tables and run idempotent migrations for new columns."""
    global pool
    pool = await asyncpg.create_pool(database_url)
    async with pool.acquire() as conn:
        # Base tables (legacy + extended schema). Safe on first run and existing dbs.
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
                mood TEXT,                -- legacy: free-form label seen by user
                meal_text TEXT,
                is_hungry BOOLEAN,
                gpt_response TEXT,
                meal_slot TEXT,           -- breakfast | lunch | dinner | snack | unknown
                trigger TEXT,             -- canonical: hunger|company|craving|tired|anxious|bored
                after_state TEXT,         -- canonical: better|heavy|no_help|neutral  (NULL if not asked)
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

            -- New: multi-select concerns
            CREATE TABLE IF NOT EXISTS user_concerns (
                user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                concern_text TEXT,
                PRIMARY KEY (user_id, concern_text)
            );

            -- New: internal LLM memory. NOT shown to user.
            -- category: dislikes | likes | allergies | triggers | helps | context
            CREATE TABLE IF NOT EXISTS user_facts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                category TEXT NOT NULL,
                fact_text TEXT NOT NULL,
                added_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (user_id, category, fact_text)
            );

            -- New: weekly summaries for cross-week callbacks
            CREATE TABLE IF NOT EXISTS weekly_summaries (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                week_start DATE NOT NULL,
                main_pattern TEXT,
                suggestion TEXT,
                full_text TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (user_id, week_start)
            );

            -- New: daily energy score for goal=energy users
            CREATE TABLE IF NOT EXISTS energy_scores (
                user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                day DATE NOT NULL,
                score INT NOT NULL CHECK (score BETWEEN 1 AND 5),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (user_id, day)
            );

            -- New: micro-insight log (to avoid spamming juzer with insights)
            CREATE TABLE IF NOT EXISTS micro_insights (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                insight_text TEXT,
                signal TEXT,              -- which local signal triggered it
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Idempotent migrations: add new columns to users if not present.
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS goal TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS gender TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS age INT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS height_cm INT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS weight_kg REAL;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS activity_level TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS training_frequency TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS reminder_frequency TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS breakfast_time TIME;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS lunch_time TIME;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS dinner_time TIME;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS evening_message_time TIME;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS sos_pending BOOLEAN DEFAULT FALSE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS paused_until TIMESTAMPTZ;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMPTZ;
            ALTER TABLE meal_logs ADD COLUMN IF NOT EXISTS meal_slot TEXT;
            ALTER TABLE meal_logs ADD COLUMN IF NOT EXISTS trigger TEXT;
            ALTER TABLE meal_logs ADD COLUMN IF NOT EXISTS after_state TEXT;
        """)


async def close_db():
    global pool
    if pool:
        await pool.close()


# ──────────────────────────────────────────────
# Users — base CRUD
# ──────────────────────────────────────────────

async def get_user(user_id: int):
    return await pool.fetchrow("SELECT * FROM users WHERE id = $1", user_id)


async def create_user(user_id: int, name: str):
    await pool.execute(
        """INSERT INTO users (id, name) VALUES ($1, $2)
           ON CONFLICT (id) DO UPDATE SET name = $2""",
        user_id, name,
    )


async def update_user_field(user_id: int, field: str, value):
    """Generic field updater. `field` must be a known column name (whitelist below)."""
    allowed = {
        "name", "goal", "gender", "age", "height_cm", "weight_kg",
        "activity_level", "training_frequency", "reminder_frequency",
        "breakfast_time", "lunch_time", "dinner_time", "evening_message_time",
        "timezone", "evening_hour", "evening_minute",
        "onboarding_done", "evening_pending", "sos_pending",
        "paused_until", "last_active_at",
    }
    if field not in allowed:
        raise ValueError(f"Field {field} is not allowed for update")
    await pool.execute(
        f"UPDATE users SET {field} = $1 WHERE id = $2",
        value, user_id,
    )


async def mark_onboarding_done(user_id: int):
    await pool.execute(
        "UPDATE users SET onboarding_done = TRUE WHERE id = $1", user_id
    )


async def touch_last_active(user_id: int):
    await pool.execute(
        "UPDATE users SET last_active_at = NOW() WHERE id = $1", user_id
    )


async def get_all_onboarded_users():
    """Returns every onboarded user with their schedule-relevant fields."""
    return await pool.fetch("""
        SELECT id, name, timezone, goal, reminder_frequency,
               breakfast_time, lunch_time, dinner_time, evening_message_time,
               evening_hour, evening_minute
        FROM users
        WHERE onboarding_done = TRUE
    """)


# ──────────────────────────────────────────────
# Evening / SOS pending flags
# ──────────────────────────────────────────────

async def set_evening_pending(user_id: int, pending: bool):
    await pool.execute(
        "UPDATE users SET evening_pending = $1 WHERE id = $2", pending, user_id
    )


async def set_sos_pending(user_id: int, pending: bool):
    await pool.execute(
        "UPDATE users SET sos_pending = $1 WHERE id = $2", pending, user_id
    )


# ──────────────────────────────────────────────
# Concerns (many-to-many)
# ──────────────────────────────────────────────

async def add_concern(user_id: int, concern_text: str):
    await pool.execute(
        """INSERT INTO user_concerns (user_id, concern_text)
           VALUES ($1, $2) ON CONFLICT DO NOTHING""",
        user_id, concern_text,
    )


async def remove_concern(user_id: int, concern_text: str):
    await pool.execute(
        "DELETE FROM user_concerns WHERE user_id = $1 AND concern_text = $2",
        user_id, concern_text,
    )


async def get_concerns(user_id: int) -> list[str]:
    rows = await pool.fetch(
        "SELECT concern_text FROM user_concerns WHERE user_id = $1",
        user_id,
    )
    return [r["concern_text"] for r in rows]


async def clear_concerns(user_id: int):
    await pool.execute("DELETE FROM user_concerns WHERE user_id = $1", user_id)


# ──────────────────────────────────────────────
# User facts (internal LLM memory — not exposed)
# ──────────────────────────────────────────────

async def add_fact(user_id: int, category: str, fact_text: str):
    await pool.execute(
        """INSERT INTO user_facts (user_id, category, fact_text)
           VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
        user_id, category, fact_text,
    )


async def get_facts(user_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT category, fact_text FROM user_facts WHERE user_id = $1 ORDER BY added_at",
        user_id,
    )
    return [dict(r) for r in rows]


async def remove_fact(user_id: int, category: str, fact_text: str):
    await pool.execute(
        """DELETE FROM user_facts
           WHERE user_id = $1 AND category = $2 AND fact_text = $3""",
        user_id, category, fact_text,
    )


# ──────────────────────────────────────────────
# Meal logs
# ──────────────────────────────────────────────

async def save_meal_log(
    user_id: int,
    mood: str | None,
    meal_text: str,
    is_hungry: bool | None,
    gpt_response: str | None,
    meal_slot: str | None = None,
    created_at: datetime | None = None,
    trigger: str | None = None,
    after_state: str | None = None,
):
    """If `created_at` is provided, use it (for day-recap parsing in 1x/day mode)."""
    if created_at is None:
        await pool.execute(
            """INSERT INTO meal_logs (user_id, mood, meal_text, is_hungry, gpt_response,
                                      meal_slot, trigger, after_state)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            user_id, mood, meal_text, is_hungry, gpt_response,
            meal_slot, trigger, after_state,
        )
    else:
        await pool.execute(
            """INSERT INTO meal_logs (user_id, mood, meal_text, is_hungry, gpt_response,
                                      meal_slot, trigger, after_state, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            user_id, mood, meal_text, is_hungry, gpt_response,
            meal_slot, trigger, after_state, created_at,
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


async def get_recent_meals(user_id: int, days: int = 28):
    """Wider window for monthly pattern detection."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    return await pool.fetch(
        "SELECT * FROM meal_logs WHERE user_id = $1 AND created_at >= $2 ORDER BY created_at",
        user_id, cutoff,
    )


async def get_last_meal_log_at(user_id: int):
    row = await pool.fetchrow(
        "SELECT MAX(created_at) AS ts FROM meal_logs WHERE user_id = $1",
        user_id,
    )
    return row["ts"] if row else None


async def get_recent_assistant_replies(user_id: int, n: int = 3) -> list[str]:
    """Return up to N most recent Ника replies across meal_logs and evening_logs.
    Used for anti-repetition: we ask GPT not to echo these phrasings."""
    rows = await pool.fetch(
        """
        SELECT response FROM (
            SELECT gpt_response AS response, created_at
            FROM meal_logs
            WHERE user_id = $1 AND gpt_response IS NOT NULL AND gpt_response <> ''
            UNION ALL
            SELECT gpt_response AS response, created_at
            FROM evening_logs
            WHERE user_id = $1 AND gpt_response IS NOT NULL AND gpt_response <> ''
        ) t
        ORDER BY created_at DESC
        LIMIT $2
        """,
        user_id, n,
    )
    return [r["response"] for r in rows]


# ──────────────────────────────────────────────
# SOS sessions
# ──────────────────────────────────────────────

async def save_sos_session(user_id: int, trigger_text: str, gpt_dialogue: str):
    await pool.execute(
        "INSERT INTO sos_sessions (user_id, trigger_text, gpt_dialogue) VALUES ($1, $2, $3)",
        user_id, trigger_text, gpt_dialogue,
    )


async def get_week_sos(user_id: int):
    week_ago = datetime.utcnow() - timedelta(days=7)
    return await pool.fetch(
        "SELECT * FROM sos_sessions WHERE user_id = $1 AND created_at >= $2 ORDER BY created_at",
        user_id, week_ago,
    )


async def get_recent_sos(user_id: int, days: int = 28):
    cutoff = datetime.utcnow() - timedelta(days=days)
    return await pool.fetch(
        "SELECT * FROM sos_sessions WHERE user_id = $1 AND created_at >= $2 ORDER BY created_at",
        user_id, cutoff,
    )


# ──────────────────────────────────────────────
# Evening logs
# ──────────────────────────────────────────────

async def save_evening_log(user_id: int, user_text: str | None, gpt_response: str):
    await pool.execute(
        "INSERT INTO evening_logs (user_id, user_text, gpt_response) VALUES ($1, $2, $3)",
        user_id, user_text, gpt_response,
    )


async def get_week_evenings(user_id: int):
    week_ago = datetime.utcnow() - timedelta(days=7)
    return await pool.fetch(
        "SELECT * FROM evening_logs WHERE user_id = $1 AND created_at >= $2 ORDER BY created_at",
        user_id, week_ago,
    )


# ──────────────────────────────────────────────
# Weekly summaries
# ──────────────────────────────────────────────

async def save_weekly_summary(
    user_id: int,
    week_start: date,
    main_pattern: str | None,
    suggestion: str | None,
    full_text: str,
):
    await pool.execute(
        """INSERT INTO weekly_summaries (user_id, week_start, main_pattern, suggestion, full_text)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (user_id, week_start) DO UPDATE
           SET main_pattern = EXCLUDED.main_pattern,
               suggestion = EXCLUDED.suggestion,
               full_text = EXCLUDED.full_text""",
        user_id, week_start, main_pattern, suggestion, full_text,
    )


async def get_last_weekly_summary(user_id: int):
    return await pool.fetchrow(
        """SELECT * FROM weekly_summaries
           WHERE user_id = $1 ORDER BY week_start DESC LIMIT 1""",
        user_id,
    )


# ──────────────────────────────────────────────
# Energy scores
# ──────────────────────────────────────────────

async def save_energy_score(user_id: int, score: int, day: date | None = None):
    if day is None:
        day = datetime.utcnow().date()
    await pool.execute(
        """INSERT INTO energy_scores (user_id, day, score)
           VALUES ($1, $2, $3)
           ON CONFLICT (user_id, day) DO UPDATE SET score = EXCLUDED.score""",
        user_id, day, score,
    )


async def get_week_energy(user_id: int):
    week_ago = (datetime.utcnow() - timedelta(days=7)).date()
    return await pool.fetch(
        "SELECT day, score FROM energy_scores WHERE user_id = $1 AND day >= $2 ORDER BY day",
        user_id, week_ago,
    )


# ──────────────────────────────────────────────
# Micro-insights log (to avoid duplicate sends)
# ──────────────────────────────────────────────

async def save_micro_insight(user_id: int, insight_text: str, signal: str):
    await pool.execute(
        "INSERT INTO micro_insights (user_id, insight_text, signal) VALUES ($1, $2, $3)",
        user_id, insight_text, signal,
    )


async def get_last_micro_insight_at(user_id: int):
    row = await pool.fetchrow(
        "SELECT MAX(created_at) AS ts FROM micro_insights WHERE user_id = $1",
        user_id,
    )
    return row["ts"] if row else None


async def get_recent_micro_insights(user_id: int, days: int = 14):
    cutoff = datetime.utcnow() - timedelta(days=days)
    return await pool.fetch(
        "SELECT * FROM micro_insights WHERE user_id = $1 AND created_at >= $2 ORDER BY created_at",
        user_id, cutoff,
    )
