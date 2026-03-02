import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

# ================= НАСТРОЙКИ =================
# Токен хранится в переменной окружения TOKEN (Railway -> Variables)
TOKEN = os.getenv("TOKEN")

# Канал, где работают команды и кнопка отметки
TEXT_CHANNEL_ID = 1473388888528654422

# Голосовой канал, где "присутствующие" = кто сейчас там сидит
VOICE_CHANNEL_ID = 1468615527894224998

# Роль, которая может нажимать кнопку и использовать !setup / !report / !say
ALLOWED_ROLE_ID = 1468613036901138514

# Канал для напоминаний (ОТДЕЛЬНЫЙ от канала с кнопкой/командами)
PING_CHANNEL_ID = 1473729059514224784

# Роль, которую упоминать в напоминаниях
PING_ROLE_ID = 1468614100358795284

# Часовой пояс (МСК)
LOCAL_TZ = ZoneInfo("Europe/Moscow")

# Напоминания: ровно в :50 в эти часы, текст выбирается случайно из списка
PING_MINUTE = 50
PING_SCHEDULE = {
    8: [
        "проснулся? пиздуй на босса через 10 минут.",
        "норм спалось? а мне нет. пиздуй на босса через 10 минут.",
        "очнулся? отлично, доброго утра не будет, ведь босс через 10 минут.",
    ],
    11: [
        "альтушки газ знакомиться на боссе через 10 минут.",
        "групповая мастурбация на боссе через 10 минут.",
        "аааа прогуливаешь сынок))), кабанчиком на босса через 10 минут.",
    ],
    12: [
        "каловая лудка через 10 минут.",
        "данжик крутится, калик мутится (данж через 10 минут).",
    ],
    14: [
        "пришел со школы? на босса нахуй через 10 минут.",
        "свага тут? нет, она на боссе через 10 минут.",
        "опозорился в школе? не опозорься при мне на боссе через 10 минут.",
    ],
    17: [
        "кружок коллективного превозмогания - босс через 10 минут.",
        "стрельба спермой на боссе через 10 минут, открываем рты.",
        "на циве ебальничек слетает только так, еби на боссе карлик, четко попадая в такт (через 10 минут босс).",
    ],
    18: [
        "ВЫ ПРОСИЛИ КОНТЕНТ? ХАВАЙТЕ ДАНЖ ЧЕРЕЗ 10 МИНУТ.",
        "UMAzing лут в данже (АЖ 90%!!!!!!) через 10 минут.",
    ],
    20: [
        "Я ПРОЕБЫВАЮ КВ НА КАЛЕ РАДИ ЭТОГО БОССА ЕБАННОГО ЧЕРЕЗ 10 МИНУТ.",
        "мамбо. (босс через 10 минут).",
        "出席老板晚宴的人有权获得一只猫老婆和一碗米饭。(10分钟后老板).",
    ],
    23: [
        "не спать солдат, на босса через 10 минут.",
        "хватит считать овец, у меня на них хуй стоит (босс через 10 минут).",
        "ГОСПОДИ ДАЙ МНЕ СИЛ ЕЩЁ ОДИН ДЕНЬ ПРОЖИТЬ С ЭТИМИ БОССАМИ ЕБАННЫМИ (он кстати через 10 минут).",
    ],
}

DB_PATH = "attendance.db"
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
    # Ник на сервере / display name
    return member.display_name


def in_main_channel(ctx: commands.Context) -> bool:
    return ctx.channel and ctx.channel.id == TEXT_CHANNEL_ID


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
    if not in_main_channel(ctx):
        return await ctx.send("Команда работает только в основном канале.")

    await ctx.send(
        "Нажмите кнопку для отметки присутствующих (бот отметит всех, кто сейчас в войсе):",
        view=MarkAllView()
    )


# ----------------- Команда: report (по дням) -----------------
@bot.command(name="report")
async def report(ctx: commands.Context, days: int = 7):
    if not in_main_channel(ctx):
        return await ctx.send("Команда работает только в основном канале.")

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


# ----------------- Команда: say (#канал текст) -----------------
@bot.command(name="say")
async def say(ctx: commands.Context, channel: discord.TextChannel, *, message: str):
    # Команда работает только в основном канале
    if not in_main_channel(ctx):
        return await ctx.send("Эту команду можно использовать только в основном канале.")

    # Проверка роли
    if not isinstance(ctx.author, discord.Member) or not has_role(ctx.author, ALLOWED_ROLE_ID):
        return await ctx.send("У тебя нет нужной роли.")

    # Проверка прав бота в целевом канале
    me = ctx.guild.me if ctx.guild else None
    if me is None:
        return await ctx.send("Ошибка: не удалось определить права бота.")

    if not channel.permissions_for(me).send_messages:
        return await ctx.send("У меня нет прав писать в этот канал.")

    # Удаляем сообщение с командой (если есть права)
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

    await channel.send(message)


# ----------------- Авто-пинг роли (случайный текст из списка) -----------------
@tasks.loop(minutes=1)
async def ping_role_scheduler():
    now = datetime.now(LOCAL_TZ)

    if now.minute != PING_MINUTE:
        return

    if now.hour not in PING_SCHEDULE:
        return

    channel = bot.get_channel(PING_CHANNEL_ID)
    if channel is None:
        return

    message_text = random.choice(PING_SCHEDULE[now.hour])
    await channel.send(f"<@&{PING_ROLE_ID}> {message_text}")


@ping_role_scheduler.before_loop
async def before_ping_role_scheduler():
    await bot.wait_until_ready()


if not TOKEN:
    raise RuntimeError("Переменная окружения TOKEN не задана. Добавь её в Railway (Variables).")

bot.run(TOKEN)


