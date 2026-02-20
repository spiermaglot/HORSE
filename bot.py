import os
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

# ================= НАСТРОЙКИ =================
TOKEN = os.getenv("TOKEN")  # Railway/хостинг: Variables -> TOKEN

# Канал, где работают команды и кнопка отметки
TEXT_CHANNEL_ID = 1473388888528654422

# Голосовой канал, где "присутствующие" = кто сейчас там сидит
VOICE_CHANNEL_ID = 1468615527894224998

# Роль, которая может нажимать кнопку и использовать !say
ALLOWED_ROLE_ID = 1468613036901138514

# Канал для напоминаний (ОТДЕЛЬНЫЙ от канала с кнопкой/командами)
PING_CHANNEL_ID = 1473729059514224784

# Роль, которую упоминать в напоминаниях
PING_ROLE_ID = 1468614100358795284

# Текст напоминания
PING_MESSAGE = "босс через 10 минут епта!"

# Расписание напоминаний (Europe/Tallinn): 08:50, 11:50, 14:50, 17:50, 20:50, 23:50
PING_HOURS = (8, 11, 14, 17, 20, 23)
PING_MINUTE = 50

DB_PATH = "attendance.db"
LOCAL_TZ = ZoneInfo("Europe/Moscow")
# =============================================


# ----------------- БАЗА ДАННЫХ -----------------
def db():
    con = sqlite3.connect(DB_PATH)

    con.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            voice_channel_id INTEGER NOT NULL,
            marker_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_display TEXT
        )
    """)

    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_attendance_lookup
        ON attendance (guild_id, voice_channel_id, ts_utc, user_id)
    """)
    con.commit()

    # авто-миграция на случай старой БД без user_display
    cols = {row[1] for row in con.execute("PRAGMA table_info(attendance)").fetchall()}
    if "user_display" not in cols:
        con.execute("ALTER TABLE attendance ADD COLUMN user_display TEXT")
        con.commit()

    return con


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def has_role(member: discord.Member, role_id: int) -> bool:
    return any(r.id == role_id for r in member.roles)


def display_name(member: discord.Member) -> str:
    return member.display_name  # ник на сервере / display name


# ----------------- UI: КНОПКА -----------------
class MarkAllView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ Отметить всех (кто в войсе)",
        style=discord.ButtonStyle.success,
        custom_id="attendance:mark_all"
    )
    async def mark_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Кнопка работает только в одном канале
        if interaction.channel_id != TEXT_CHANNEL_ID:
            return await interaction.response.send_message(
                "Кнопка работает только в нужном канале.",
                ephemeral=True
            )

        # Нажимать может только нужная роль
        if not isinstance(interaction.user, discord.Member) or not has_role(interaction.user, ALLOWED_ROLE_ID):
            return await interaction.response.send_message(
                "У тебя нет нужной роли.",
                ephemeral=True
            )

        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Ошибка: guild=None", ephemeral=True)

        voice = guild.get_channel(VOICE_CHANNEL_ID)
        if voice is None or not isinstance(voice, discord.VoiceChannel):
            return await interaction.response.send_message(
                "Ошибка: голосовой канал не найден. Проверь VOICE_CHANNEL_ID.",
                ephemeral=True
            )

        members_in_voice = [m for m in voice.members if not m.bot]
        if not members_in_voice:
            return await interaction.response.send_message(
                "В голосовом канале сейчас никого нет.",
                ephemeral=True
            )

        con = db()
        ts = utc_now_iso()

        for m in members_in_voice:
            con.execute(
                """
                INSERT INTO attendance (ts_utc, guild_id, voice_channel_id, marker_id, user_id, user_display)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ts, guild.id, voice.id, interaction.user.id, m.id, display_name(m))
            )

        con.commit()
        con.close()

        await interaction.response.send_message(
            f"Отмечено **{len(members_in_voice)}** человек ✅",
            ephemeral=True
        )


# ----------------- BOT -----------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    bot.add_view(MarkAllView())

    if not ping_role_scheduler.is_running():
        ping_role_scheduler.start()

    print(f"Бот запущен как {bot.user} (ID: {bot.user.id})")


# ----------------- Команда: setup -----------------
@bot.command(name="setup")
async def setup(ctx: commands.Context):
    if ctx.channel.id != TEXT_CHANNEL_ID:
        return await ctx.send("Команда работает только в нужном канале.")

    await ctx.send(
        "Нажмите кнопку для отметки присутствующих (бот отметит всех, кто сейчас в войсе):",
        view=MarkAllView()
    )


# ----------------- Команда: say -----------------
@bot.command(name="say")
async def say(ctx: commands.Context, *, message: str):
    if ctx.channel.id != TEXT_CHANNEL_ID:
        return await ctx.send("Команда работает только в нужном канале.")

    if not isinstance(ctx.author, discord.Member) or not has_role(ctx.author, ALLOWED_ROLE_ID):
        return await ctx.send("У тебя нет нужной роли.")

    # Чтобы удалять команду, дай боту Manage Messages. Если нет — просто не удалит.
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

    await ctx.send(message)


# ----------------- Команда: report (по дням) -----------------
@bot.command(name="report")
async def report(ctx: commands.Context, days: int = 7):
    if ctx.channel.id != TEXT_CHANNEL_ID:
        return await ctx.send("Команда работает только в нужном канале.")

    if days < 1 or days > 60:
        return await ctx.send("Укажи days от 1 до 60 (чтобы отчёт не был слишком длинным).")

    since_dt = utc_now() - timedelta(days=days)
    since_iso = since_dt.isoformat()

    con = db()
    cur = con.execute(
        """
        SELECT ts_utc, user_id, COALESCE(user_display, '') as user_display
        FROM attendance
        WHERE guild_id = ? AND voice_channel_id = ? AND ts_utc >= ?
        """,
        (ctx.guild.id, VOICE_CHANNEL_ID, since_iso)
    )
    rows = cur.fetchall()
    con.close()

    if not rows:
        return await ctx.send("Нет данных за выбранный период.")

    # day -> user_id -> {name, count}
    per_day: dict[str, dict[int, dict]] = {}
    for ts_iso, user_id, user_display in rows:
        day_str = ts_iso[:10]  # YYYY-MM-DD (UTC)
        per_day.setdefault(day_str, {})
        per_day[day_str].setdefault(user_id, {"name": user_display or f"ID:{user_id}", "count": 0})
        per_day[day_str][user_id]["count"] += 1
        if user_display:
            per_day[day_str][user_id]["name"] = user_display

    day_keys = sorted(per_day.keys())

    await ctx.send(f"Отчёт по дням за последние **{days}** дней (канал: <#{VOICE_CHANNEL_ID}>):")

    current = ""
    sent = 0

    for day_str in day_keys:
        block_lines = [f"📅 **{day_str}**"]
        user_items = list(per_day[day_str].values())
        user_items.sort(key=lambda x: (-x["count"], x["name"].lower()))

        for u in user_items:
            block_lines.append(f"• **{u['name']}** — {u['count']}")

        block = "\n".join(block_lines) + "\n\n"

        if len(current) + len(block) > 1800:
            await ctx.send(current)
            sent += 1
            current = block
            if sent >= 5:
                await ctx.send("Отчёт слишком длинный — уменьшите days (например `!report 7`).")
                return
        else:
            current += block

    if current:
        await ctx.send(current)


# ----------------- Авто-пинг роли -----------------
@tasks.loop(minutes=1)
async def ping_role_scheduler():
    now = datetime.now(LOCAL_TZ)

    if now.minute != PING_MINUTE:
        return

    if now.hour not in PING_HOURS:
        return

    channel = bot.get_channel(PING_CHANNEL_ID)
    if channel is None:
        return

    await channel.send(f"<@&{PING_ROLE_ID}> {PING_MESSAGE}")


@ping_role_scheduler.before_loop
async def before_ping_role_scheduler():
    await bot.wait_until_ready()


if not TOKEN:
    raise RuntimeError("Переменная окружения TOKEN не задана. Добавь её в Railway (Variables) или в систему.")

bot.run(TOKEN)


