import discord
from discord.ext import commands, tasks
import csv
import json
import os
import time
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN not found in .env file. Copy .env.example to .env and add your token.")

# ───────────────────────────────────────────────
# ONLY works in this specific channel
# https://discord.com/channels/1509614946181447741/1529770062628655135
ALLOWED_CHANNEL_ID = 1529770062628655135
# ───────────────────────────────────────────────

DAILY_LIMIT = 5
COOLDOWN_SECONDS = 2 * 60 * 60       # 2 hours after a successful ✅
WINDOW_SECONDS = 24 * 60 * 60        # credits reset every 24 hours
USERS_FILE = "users.json"
NAMES_FILE = "names.csv"
_names_mtime: float = 0.0

# Intents - Message Content is required (enable it in the Discord Developer Portal)
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory lookup dictionary: lowercase_name -> link
name_to_link: dict[str, str] = {}

# user_id -> {credits, window_start, last_success, notified}
users: dict[str, dict] = {}


def load_names(csv_path: str = NAMES_FILE) -> None:
    """Load all name->link mappings from a CSV file into memory."""
    global name_to_link, _names_mtime
    name_to_link = {}

    if not os.path.exists(csv_path):
        print(f"WARNING: {csv_path} not found. No names loaded.")
        return

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or "").strip().lower()
            link = (row.get("link") or "").strip()
            if name and link:
                name_to_link[name] = link

    try:
        _names_mtime = os.path.getmtime(csv_path)
    except OSError:
        _names_mtime = 0.0

    print(f"✅ Loaded {len(name_to_link):,} names from {csv_path}")


@tasks.loop(seconds=10)
async def watch_names_file() -> None:
    """Reload names.csv automatically when the file changes. No restart needed."""
    global _names_mtime
    if not os.path.exists(NAMES_FILE):
        return
    try:
        current = os.path.getmtime(NAMES_FILE)
    except OSError:
        return
    if current != _names_mtime:
        print("🔄 names.csv changed — reloading…")
        load_names()


def load_users() -> None:
    global users
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                users = json.load(f)
            print(f"✅ Loaded usage data for {len(users)} users")
        except Exception as e:
            print(f"Warning: could not load {USERS_FILE}: {e}")
            users = {}
    else:
        users = {}


def save_users() -> None:
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f)
    except Exception as e:
        print(f"Warning: could not save {USERS_FILE}: {e}")


def get_user(user_id: int) -> dict:
    """Get (and create/reset) a user's usage record."""
    uid = str(user_id)
    now = time.time()
    data = users.get(uid)

    if not data:
        data = {
            "credits": 0,
            "window_start": now,
            "last_success": 0.0,
            "notified": False,
        }
        users[uid] = data
        return data

    # Reset daily credits when the 24h window expires
    if now - float(data.get("window_start", 0)) >= WINDOW_SECONDS:
        data["credits"] = 0
        data["window_start"] = now
        data["notified"] = False
        users[uid] = data
        save_users()

    return data


def next_allowed_ts(data: dict) -> float:
    """Unix time when the user may make another successful request."""
    times = []
    last_success = float(data.get("last_success") or 0)
    if last_success:
        times.append(last_success + COOLDOWN_SECONDS)
    if int(data.get("credits") or 0) >= DAILY_LIMIT:
        times.append(float(data.get("window_start") or 0) + WINDOW_SECONDS)
    return max(times) if times else 0.0


def is_blocked(data: dict) -> bool:
    return time.time() < next_allowed_ts(data)


@bot.event
async def on_ready():
    print(f"🤖 Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"📡 Listening ONLY in channel ID: {ALLOWED_CHANNEL_ID}")
    print(f"🎫 {DAILY_LIMIT} requests / 24h  |  ⏰ {COOLDOWN_SECONDS // 3600}h between successes")
    print("------")
    load_names()
    load_users()
    if not watch_names_file.is_running():
        watch_names_file.start()
    print("Bot is ready and listening for names...")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Commands still work everywhere
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    if message.channel.id != ALLOWED_CHANNEL_ID:
        return

    content = message.content.strip().lower()
    if not content:
        return

    data = get_user(message.author.id)

    # ── On 2h / daily-limit lockout: delete + DM anti-spam ONCE ──
    if is_blocked(data):
        try:
            await message.delete()
        except discord.HTTPException as e:
            print(f"Failed to delete cooldown message: {e}")

        if not data.get("notified"):
            ts = int(next_allowed_ts(data))
            anti_spam = (
                "⏳ **Anti-Spam**\n"
                "Please wait a moment.\n"
                f"Try again <t:{ts}:R>."
            )
            try:
                await message.author.send(anti_spam)
                data["notified"] = True
                save_users()
            except discord.Forbidden:
                print(f"Cannot DM {message.author} (DMs closed or bot blocked)")
            except Exception as e:
                print(f"Error sending anti-spam DM: {e}")
        return

    # ── Not on cooldown: treat the message as a name submit ──
    try:
        await message.add_reaction("⏳")
    except discord.HTTPException as e:
        print(f"Failed to add reaction: {e}")

    if content not in name_to_link:
        # Not found → ❌  (no credit used, no 2h lock)
        try:
            await message.remove_reaction("⏳", bot.user)
        except discord.HTTPException:
            pass
        try:
            await message.add_reaction("❌")
        except discord.HTTPException:
            pass
        return

    # Found → send DM, then swap ⏳ for ✅
    link = name_to_link[content]
    original_name = message.content.strip()
    data["credits"] = int(data.get("credits") or 0) + 1
    data["last_success"] = time.time()
    data["notified"] = False
    save_users()

    next_ts = int(next_allowed_ts(data))
    credits_now = data["credits"]

    dm_message = (
        f"- 📥 Request for: **{original_name}** found! ✅\n"
        f"- 📦 Collection: {link}\n\n"
        f"`Credits: {credits_now}/{DAILY_LIMIT}`\n"
        f"`Status: Confirmed`\n\n"
        f"> ⏳ Next Request: <t:{next_ts}:R>\n"
        f"> -# *💖 Thank you for being a loyal subscriber!*"
    )

    try:
        await message.author.send(dm_message)

        try:
            await message.remove_reaction("⏳", bot.user)
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

    except discord.Forbidden:
        print(f"Cannot DM {message.author} (DMs closed or bot blocked)")
        # Roll back the credit if the DM never went out
        data["credits"] = max(0, int(data.get("credits") or 1) - 1)
        data["last_success"] = 0.0
        save_users()
        try:
            await message.add_reaction("❌")
        except discord.HTTPException:
            pass
    except Exception as e:
        print(f"Error sending DM: {e}")


@bot.command(name="reload")
@commands.has_permissions(administrator=True)
async def reload_names(ctx: commands.Context):
    """Reload the names.csv file (admin only)."""
    load_names()
    await ctx.send(f"✅ Reloaded **{len(name_to_link):,}** names from `names.csv`")


@bot.command(name="count")
async def name_count(ctx: commands.Context):
    """Show how many names are currently loaded."""
    await ctx.send(f"📊 Currently tracking **{len(name_to_link):,}** names.")


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    """Simple latency check."""
    await ctx.send(f"Pong! Latency: {round(bot.latency * 1000)}ms")


if __name__ == "__main__":
    bot.run(TOKEN)
