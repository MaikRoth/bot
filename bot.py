import os
import re
import html
import random
import discord
import asyncio
import aiohttp
import aiosqlite
import youtube_dl
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from discord.ext import commands
from discord.ui import View, Button
from discord import app_commands
from datetime import datetime, timedelta, date


load_dotenv()

DATABASE = "data.db"

BASE_REWARD = 100
STREAK_BONUS = 20
RARITY_COLORS = {
    "common": discord.Color.light_gray(),
    "uncommon": discord.Color.green(),
    "rare": discord.Color.blue(),
    "epic": discord.Color.purple(),
    "legendary": discord.Color.gold()
}

JACKPOT = 0.00000005 # 1 in 20 million
FIFTY_X = 0.025  #  1 in 40
TEN_X = 0.05 # 1 in 20
THREE_X = 0.10 # 1 in 10
TWO_X = 0.20 # 1 in 5
LOSS = 1 - (JACKPOT + FIFTY_X + TEN_X + THREE_X + TWO_X) # 1 in 

# Bot-Setup mit erweiterten Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Für AFK Messages
user_data = {}
server_stats = {}
# Aktive Autogambles im RAM
active_autogambles = {}
# Lock zur Verhinderung von Race Conditions beim DB-Zugriff
db_lock = asyncio.Lock()




# 🎛 YouTubeDL und FFmpeg Einstellungen
YDL_OPTIONS = {'format': 'bestaudio', 'noplaylist': 'True', 'quiet': True}
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

# 🎵 Musik-Warteschlange
music_queues = {}

# ===========================
#     HILFSFUNKTIONEN
# ===========================
async def play_next(interaction: discord.Interaction):
    """Spielt das nächste Lied in der Warteschlange ab."""
    guild_id = interaction.guild.id
    if guild_id not in music_queues or not music_queues[guild_id]:
        await asyncio.sleep(2)
        vc = interaction.guild.voice_client
        if vc:
            await vc.disconnect()
        return

    vc = interaction.guild.voice_client
    if not vc:
        return

    # Nächstes Lied aus der Queue holen
    link, title = music_queues[guild_id].pop(0)
    with youtube_dl.YoutubeDL(YDL_OPTIONS) as ydl:
        info = ydl.extract_info(link, download=False)
        url2 = info['url']

    vc.play(discord.FFmpegPCMAudio(url2, **FFMPEG_OPTIONS), after=lambda e: asyncio.run_coroutine_threadsafe(play_next(interaction), asyncio.get_event_loop()))
    await interaction.followup.send(f"🎶 **Spiele jetzt:** {title}")

# ===========================
#     /play COMMAND
# ===========================
@bot.tree.command(name="play", description="Spielt ein Lied ab oder fügt es zur Warteschlange hinzu.")
@app_commands.describe(link="Der YouTube/Soundcloud-Link zum Song")
async def play(interaction: discord.Interaction, link: str):
    voice_channel = getattr(interaction.user.voice, 'channel', None)
    if not voice_channel:
        await interaction.response.send_message("❌ Du musst in einem Sprachkanal sein, um Musik abzuspielen.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    guild_id = interaction.guild.id
    if guild_id not in music_queues:
        music_queues[guild_id] = []

    # Voice-Connection herstellen
    if interaction.guild.voice_client:
        vc = interaction.guild.voice_client
        if vc.channel != voice_channel:
            await vc.move_to(voice_channel)
    else:
        vc = await voice_channel.connect()

    # Song laden
    with youtube_dl.YoutubeDL(YDL_OPTIONS) as ydl:
        info = ydl.extract_info(link, download=False)
        url2 = info['url']
        title = info.get('title', 'Unbekannter Titel')

    if not vc.is_playing():
        vc.play(discord.FFmpegPCMAudio(url2, **FFMPEG_OPTIONS), after=lambda e: asyncio.run_coroutine_threadsafe(play_next(interaction), asyncio.get_event_loop()))
        await interaction.followup.send(f"🎶 **Spiele:** {title}")
    else:
        music_queues[guild_id].append((link, title))
        await interaction.followup.send(f"➕ **Zur Warteschlange hinzugefügt:** {title}")

# ===========================
#     /queue COMMAND
# ===========================
@bot.tree.command(name="queue", description="Zeigt die aktuelle Warteschlange.")
async def queue(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    queue = music_queues.get(guild_id, [])

    if not queue:
        await interaction.response.send_message("🎧 Die Warteschlange ist leer.", ephemeral=True)
        return

    embed = discord.Embed(title="🎶 Aktuelle Warteschlange", color=discord.Color.blurple())
    for i, (_, title) in enumerate(queue, start=1):
        embed.add_field(name=f"{i}.", value=title, inline=False)
    await interaction.response.send_message(embed=embed)

# ===========================
#     /skip COMMAND
# ===========================
@bot.tree.command(name="skip", description="Überspringt das aktuelle Lied.")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("❌ Es läuft derzeit keine Musik.", ephemeral=True)
        return

    vc.stop()
    await interaction.response.send_message("⏭️ **Lied übersprungen.**")

# ===========================
#     /stop COMMAND
# ===========================
@bot.tree.command(name="stop", description="Stoppt die Musik und verlässt den Sprachkanal.")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message("❌ Ich bin in keinem Sprachkanal.", ephemeral=True)
        return

    vc.stop()
    await vc.disconnect()
    guild_id = interaction.guild.id
    music_queues[guild_id] = []
    await interaction.response.send_message("🛑 **Musik gestoppt und Kanal verlassen.**")

    
async def dungeon_scheduler():
    while True:
        now = datetime.datetime.now()
        tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        seconds_until_midnight = (tomorrow - now).total_seconds()
        print(f"⏳ Nächster Dungeon in {int(seconds_until_midnight)} Sekunden.")
        await asyncio.sleep(seconds_until_midnight)
        await generate_daily_dungeon()


@bot.event
async def on_ready():
    print(f"✨ Eingeloggt als {bot.user}")
    print(f"🆔 Bot ID: {bot.user.id}")
    print(f"📊 Auf {len(bot.guilds)} Servern aktiv")

    try:
        # Slash-Commands synchronisieren
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} Slash-Commands synchronisiert.")
    except Exception as e:
        print(f"❌ Fehler beim Synchronisieren: {e}")

    # Lade persistente Sessions
    try:
        # 🔄 Aktive Autogambles wiederherstellen
        await load_active_autogambles()

        # 🔄 Aktive Auto-Miner wiederherstellen
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS automine_sessions (
                    user_id INTEGER PRIMARY KEY,
                    active INTEGER
                )
            """)
            await db.commit()

            async with db.execute("SELECT user_id FROM automine_sessions WHERE active = 1") as cur:
                active_users = await cur.fetchall()

        if active_users:
            print(f"🔄 Stelle {len(active_users)} Auto-Miner wieder her...")
            for (user_id,) in active_users:
                user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                if user:
                    try:
                        dm = await user.create_dm()
                        print(f"  ↪ Reaktiviere Auto-Miner für {user.name} (DM)")
                        bot.loop.create_task(automine_restart_task(dm, user_id))
                    except discord.Forbidden:
                        print(f"⚠️ Konnte keine DM an {user_id} senden – überspringe.")
                else:
                    print(f"⚠️ User {user_id} nicht gefunden – überspringe.")
        else:
            print("⛏️ Keine aktiven Auto-Miner gefunden.")

    except Exception as e:
        print(f"❌ Fehler beim Wiederherstellen der Sessions: {e}")

    # 🏰 Dungeon-System starten
    try:
        await generate_daily_dungeon()         # Dungeon für heute generieren
        bot.loop.create_task(dungeon_scheduler())  # Scheduler starten
        print("🏰 Dungeon-System gestartet und täglicher Generator aktiv.")
    except Exception as e:
        print(f"⚠️ Fehler beim Dungeon-System: {e}")

    # ♻️ Statusrotation starten
    try:
        bot.loop.create_task(change_status())
        print("♻️ Status-Rotation gestartet.")
    except Exception as e:
        print(f"⚠️ Konnte Status-Rotation nicht starten: {e}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send('This command is on cooldown, you can use it in {round(error.retry_after, 2)}')

######## Sammel Spiel ########

def calculate_reward(streak: int) -> int:
    return BASE_REWARD + streak * STREAK_BONUS + random.randint(0, 50)
 

async def check_achievements(discord_id: int, db: aiosqlite.Connection, *, coins: Optional[int]=None, streak: Optional[int]=None) -> List[Dict[str, Any]]:
    """
    Prüft und vergibt Achievements für den User (discord_id).
    Nutzt users.id (user_db_id) überall dort, wo Fremdschlüssel referenzieren.
    Gibt eine Liste neu freigeschalteter Achievements zurück: [{id,name,reward_coins}, ...]
    """
    await db.execute("PRAGMA foreign_keys = ON;")

    newly: List[Dict[str, Any]] = []

    # Row-Access per Namen
    db.row_factory = aiosqlite.Row

    # -- Starte eine IMMEDIATE-Transaktion (Race-Conditions vermeiden)
    try:
        await db.execute("BEGIN IMMEDIATE")
        manual_transaction = True
    except Exception:
        # Falls bereits eine Transaktion aktiv ist
        manual_transaction = False

    try:
        # 1) User laden
        async with db.execute("""
            SELECT discord_id, coins, 
                   COALESCE(level, 1) AS level, 
                   COALESCE(mining_level, 1) AS mining_level,
                   COALESCE(streak, 0) AS streak
            FROM users_new WHERE discord_id = ?
        """, (discord_id,)) as c:
            u = await c.fetchone()

        if not u:
            await db.execute("ROLLBACK")
            return []

        user_db_id = u["discord_id"]
        user_coins  = coins if coins is not None else u["coins"]
        user_streak = streak if streak is not None else u["streak"]

        # 2) Bereits freigeschaltete Achievements
        async with db.execute("""
            SELECT achievement_id FROM user_achievements_new WHERE user_id = ?
        """, (user_db_id,)) as c:
            already = {row["achievement_id"] async for row in c}

        # 3) Alle Achievements
        async with db.execute("""
            SELECT id, name, condition, reward_coins
            FROM achievements
        """) as c:
            achs = await c.fetchall()

        if not achs:
            await db.execute("COMMIT")
            return []

        # 4) Hilfsdaten für Bedingungen
        # Inventory / Items
        async with db.execute("""
            SELECT 
                COALESCE(SUM(quantity),0)             AS total_items,
                COALESCE(COUNT(DISTINCT LOWER(item_name)),0) AS distinct_items
            FROM inventory_new
            WHERE user_id = ? AND quantity > 0
        """, (user_db_id,)) as c:
            inv = await c.fetchone()
        total_items = inv["total_items"]
        distinct_items = inv["distinct_items"]

        async with db.execute("""
            SELECT COALESCE(COUNT(*),0) AS cnt
            FROM inventory_new
            WHERE user_id = ? AND item_rarity = 'legendary' AND quantity > 0
        """, (user_db_id,)) as c:
            leg = await c.fetchone()
        has_legendary = leg["cnt"] > 0

        async with db.execute("""
            SELECT COALESCE(COUNT(DISTINCT LOWER(name)),0) AS all_distinct
            FROM shop_items
        """) as c:
            shop_count_row = await c.fetchone()
        all_shop_distinct = shop_count_row["all_distinct"]

        owns_all_items = (distinct_items >= all_shop_distinct and all_shop_distinct > 0)

        # Stats (für shop_open_* und gamble_*)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id INTEGER PRIMARY KEY, 
                shops_opened INTEGER DEFAULT 0,
                gambles_played INTEGER DEFAULT 0,
                gambles_won INTEGER DEFAULT 0,
                jackpots_won INTEGER DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        # Defaults laden (falls noch kein Eintrag existiert → 0)
        async with db.execute("""
            SELECT COALESCE(shops_opened,0) AS shops_opened,
                   COALESCE(gambles_played,0) AS gambles_played,
                   COALESCE(gambles_won,0)    AS gambles_won,
                   COALESCE(jackpots_won,0)   AS jackpots_won
            FROM user_stats WHERE user_id = ?
        """, (user_db_id,)) as c:
            st = await c.fetchone()
        if not st:
            shops_opened = 0
            gambles_played = 0
            gambles_won = 0
            jackpots_won = 0
        else:
            shops_opened  = st["shops_opened"]
            gambles_played= st["gambles_played"]
            gambles_won   = st["gambles_won"]
            jackpots_won  = st["jackpots_won"]

        # Level-Feld (nutze level, fallback mining_level)
        user_level = u["level"] if u["level"] else u["mining_level"]

        # 5) Bedingungen evaluieren
        total_achievements = len(achs)
        unlocked_now_ids = []

        def meets(cond: str) -> bool:
            if not cond or cond == "none":
                return False

            if cond.startswith("daily_"):
                try:
                    n = int(cond.split("_", 1)[1])
                    return user_streak >= n
                except:  # noqa
                    return False

            if cond.startswith("coins_"):
                try:
                    n = int(cond.split("_", 1)[1])
                    return user_coins >= n
                except:
                    return False

            if cond == "buy_1":
                return total_items >= 1
            if cond == "buy_10":
                return total_items >= 10
            if cond == "buy_legendary":
                return has_legendary

            if cond.startswith("shop_open_"):
                try:
                    n = int(cond.split("_", 2)[2])
                    return shops_opened >= n
                except:
                    return False

            if cond.startswith("gamble_"):
                # gamble_1 / gamble_100 / gamble_win_1 / jackpot
                tail = cond.split("_", 1)[1]
                if tail.isdigit():
                    return gambles_played >= int(tail)
                if tail == "win_1":
                    return gambles_won >= 1
                if tail == "jackpot":
                    return jackpots_won >= 1
                return False

            if cond.startswith("level_"):
                try:
                    n = int(cond.split("_", 1)[1])
                    return user_level >= n
                except:
                    return False

            if cond == "own_all_items":
                return owns_all_items

            if cond == "achievements_all":
                # schalte frei, wenn ALLE anderen bereits freigeschaltet sind
                # (diese hier also als letzte übrig ist)
                return len(already) + len(unlocked_now_ids) >= (total_achievements - 1)

            # Unbekannte Condition → False
            return False

        total_reward = 0
        for ach in achs:
            ach_id = ach["id"]
            if ach_id in already:
                continue
            cond = ach["condition"]
            if meets(cond):
                # idempotent eintragen
                await db.execute("""
                    INSERT OR IGNORE INTO user_achievements_new (user_id, achievement_id, achieved_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                """, (user_db_id, ach_id))
                total_reward += int(ach["reward_coins"] or 0)
                unlocked_now_ids.append(ach_id)
                newly.append({
                    "id": ach_id,
                    "name": ach["name"],
                    "reward_coins": int(ach["reward_coins"] or 0),
                })

        # Coins einmalig gutschreiben (falls nötig)
        if total_reward > 0:
            await db.execute("UPDATE users_new SET coins = coins + ? WHERE discord_id = ?", (total_reward, user_db_id))

        if manual_transaction:
            await db.commit()
        return newly

    except Exception:
        if manual_transaction:
            await db.execute("ROLLBACK")
        raise


RARITIES = {
    "common": 0.5,
    "uncommon": 0.25,
    "rare": 0.15,
    "epic": 0.07,
    "legendary": 0.03,
}

DUNGEON_NAMES = {
    "common": "Verlassene Mine",
    "uncommon": "Goblinhöhle",
    "rare": "Verfluchtes Grab",
    "epic": "Drachenhort",
    "legendary": "Tempel der Ewigkeit",
}
async def generate_daily_dungeon():
    async with aiosqlite.connect(DATABASE) as db:
        db.row_factory = aiosqlite.Row
        today = (await (await db.execute("SELECT DATE('now','localtime')")).fetchone())[0]

        # Prüfen, ob schon Dungeon existiert
        async with db.execute("SELECT id FROM dungeons WHERE date = ?", (today,)) as cur:
            existing = await cur.fetchone()
        if existing:
            return  # heutiger Dungeon existiert schon

        rarity = random.choices(list(RARITIES.keys()), weights=RARITIES.values(), k=1)[0]
        dungeon_name = DUNGEON_NAMES[rarity]

        # Items passend zur Rarity auswählen
        async with db.execute(
            "SELECT name FROM shop_items WHERE rarity IN (?, ?) ORDER BY RANDOM() LIMIT ?",
            (rarity, "common", random.randint(2, 5)),
        ) as cur:
            items = [r["name"] for r in await cur.fetchall()]

        required_items = ",".join(items)
        reward_coins = {
            "common": 300,
            "uncommon": 700,
            "rare": 1500,
            "epic": 3000,
            "legendary": 6000,
        }[rarity]

        # Optional seltener Reward
        reward_item = None
        if rarity in ("epic", "legendary"):
            async with db.execute(
                "SELECT name FROM shop_items WHERE rarity = ? ORDER BY RANDOM() LIMIT 1",
                (rarity,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    reward_item = row["name"]

        await db.execute("""
            INSERT INTO dungeons (date, name, rarity, required_items, reward_coins, reward_item)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (today, dungeon_name, rarity, required_items, reward_coins, reward_item))
        await db.commit()
        print(f"🕹️ Neuer Dungeon erstellt: {dungeon_name} ({rarity}) mit Items: {required_items}")
        

@bot.tree.command(name="dungeon_info", description="Zeigt den Dungeon des heutigen Tages an.")
async def dungeon_info(interaction: discord.Interaction):
    async with aiosqlite.connect(DATABASE) as db:
        db.row_factory = aiosqlite.Row
        today = (await (await db.execute("SELECT DATE('now','localtime')")).fetchone())[0]

        async with db.execute("SELECT * FROM dungeons WHERE date = ?", (today,)) as cur:
            dungeon = await cur.fetchone()

        if not dungeon:
            await interaction.response.send_message("Heute gibt es noch keinen Dungeon.", ephemeral=True)
            return
        user_id = interaction.user.id
        # Inventar laden
        async with db.execute(
            "SELECT item_name, quantity, item_rarity FROM inventory_new WHERE user_id = ?",
            (user_id,)
        ) as cur:
            inv = await cur.fetchall()

    # ——— außerhalb der Datenbank weiterrechnen ———
    inv_names = [i["item_name"] for i in inv]
    required_items = [i.strip() for i in dungeon["required_items"].split(",")]

    matched = sum(1 for i in required_items if i in inv_names)
    base_chance = 0.3 + 0.15 * matched  # bis zu 100% bei allen Items
    rarity_factor = {
        "common": 1.0,
        "uncommon": 0.9,
        "rare": 0.8,
        "epic": 0.7,
        "legendary": 0.6
    }[dungeon["rarity"]]
    total_chance = min(base_chance * rarity_factor, 1.0)

    embed = discord.Embed(
        title=f"🏰 {dungeon['name']} ({dungeon['rarity'].capitalize()})",
        description=(
            f"**Benötigte Items:** {dungeon['required_items']}\n"
            f"**Dein Erfolg:** {total_chance * 100:.2f}%\n"
            f"**Belohnung:** {dungeon['reward_coins']} 💰"
        ),
        color=discord.Color.gold()
    )

    if dungeon["reward_item"]:
        embed.add_field(name="🎁 Seltener Drop", value=dungeon["reward_item"], inline=False)

    await interaction.response.send_message(embed=embed)


async def dungeon_scheduler():
    while True:
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        seconds_until_midnight = (tomorrow - now).total_seconds()
        print(f"⏳ Nächster Dungeon in {int(seconds_until_midnight)} Sekunden.")
        await asyncio.sleep(seconds_until_midnight)
        await generate_daily_dungeon()


@bot.tree.command(name="dungeon", description="Betrete den Dungeon des Tages!")
async def dungeon(interaction: discord.Interaction):
    user_id = interaction.user.id
    await interaction.response.defer(thinking=True)

    async with aiosqlite.connect(DATABASE) as db:
        db.row_factory = aiosqlite.Row
        today = (await (await db.execute("SELECT DATE('now','localtime')")).fetchone())[0]

        # Dungeon des Tages laden
        async with db.execute("SELECT * FROM dungeons WHERE date = ?", (today,)) as cur:
            dungeon = await cur.fetchone()

        if not dungeon:
            await interaction.followup.send("🕸️ Heute gibt es keinen Dungeon.", ephemeral=True)
            return

        # prüfen, ob schon versucht
        async with db.execute(
            "SELECT id FROM user_dungeons WHERE user_id = ? AND dungeon_id = ?", (user_id, dungeon["id"])
        ) as cur:
            done = await cur.fetchone()
        if done:
            await interaction.followup.send("⚠️ Du hast den Dungeon heute bereits betreten!", ephemeral=True)
            return

        # Inventar prüfen
        required_items = [i.strip() for i in dungeon["required_items"].split(",")]
        async with db.execute(
            "SELECT item_name, quantity, item_rarity FROM inventory_new WHERE user_id = ?", (user_id,)
        ) as cur:
            inv = await cur.fetchall()
        inv_names = [i["item_name"] for i in inv]

        # Trefferquote (Chance)
        matched = sum(1 for i in required_items if i in inv_names)
        base_chance = 0.3 + 0.15 * matched  # bis zu 100% bei allen Items
        rarity_factor = {"common": 1.0, "uncommon": 0.9, "rare": 0.8, "epic": 0.7, "legendary": 0.6}[dungeon["rarity"]]
        success = random.random() < base_chance * rarity_factor

        # Items zerstören
        for item in required_items:
            await db.execute("DELETE FROM inventory_new WHERE user_id = ? AND item_name = ?", (user_id, item))

        # Erfolg oder Misserfolg speichern
        await db.execute("""
            INSERT INTO user_dungeons (user_id, dungeon_id, completed, success)
            VALUES (?, ?, 1, ?)
        """, (user_id, dungeon["id"], int(success)))

        msg = f"🏰 **{dungeon['name']} ({dungeon['rarity'].capitalize()})**\n"

        if success:
            msg += "🎉 Du hast den Dungeon erfolgreich abgeschlossen!\n"
            msg += f"💰 **Belohnung:** {dungeon['reward_coins']} Münzen"
            await db.execute("UPDATE users_new SET coins = coins + ? WHERE discord_id = ?", (dungeon["reward_coins"], user_id))
            if dungeon["reward_item"]:
                await db.execute("""
                    INSERT INTO inventory_new (user_id, item_name, quantity, item_rarity)
                    VALUES (?, ?, 1, ?)
                """, (user_id, dungeon["reward_item"], dungeon["rarity"]))
                msg += f"\n🎁 Zusätzlich erhalten: **{dungeon['reward_item']}**"
        else:
            msg += "😢 Du bist gescheitert und hast alle eingesetzten Items verloren."

        await db.commit()
        await interaction.followup.send(msg)


# --- Command: /daily ---
@bot.tree.command(name="daily", description="Sammle deine tägliche Belohnung ein!")
async def daily(interaction: discord.Interaction):
    user_id = interaction.user.id
    today = date.today()

    async with aiosqlite.connect(DATABASE) as db:
        # --- Benutzer laden oder anlegen ---
        async with db.execute("SELECT coins, streak, last_daily FROM users_new WHERE discord_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()

        if row is None:
            coins, streak, last_daily = 0, 0, None
            await db.execute(
                "INSERT INTO users_new (discord_id, coins, streak, last_daily) VALUES (?, ?, ?, ?)",
                (user_id, coins, streak, last_daily),
            )
        else:
            coins, streak, last_daily = row

        # --- Daily Check ---
        if last_daily == str(today):
            await interaction.response.send_message("❌ Du hast deine tägliche Belohnung heute schon abgeholt!", ephemeral=True)
            return

        # Streak berechnen
        if last_daily == str(today - timedelta(days=1)):
            streak += 1
        else:
            streak = 1

        reward = calculate_reward(streak)
        coins += reward

        # Datenbank aktualisieren
        await db.execute(
            "UPDATE users_new SET coins = ?, streak = ?, last_daily = ? WHERE discord_id = ?",
            (coins, streak, str(today), user_id),
        )

        # --- Achievement-Check ---
        new_achs = await check_achievements(user_id, db, streak=streak, coins=coins)
        await db.commit()

    # --- Daily Embed ---
    embed = discord.Embed(
        title="🎁 Tägliche Belohnung",
        description=f"Du hast **{reward} Münzen** erhalten!",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Aktueller Streak", value=f"{streak} 🔥", inline=True)
    embed.add_field(name="Gesamtcoins", value=f"{coins} 💰", inline=True)
    embed.set_footer(text="Komm morgen wieder für mehr Belohnung!")

    await interaction.response.send_message(embed=embed)

    # --- Neue Achievements anzeigen ---
    if new_achs:
        # Extrahiere nur die IDs aus den Dicts
        ach_ids = [a["id"] for a in new_achs]

        async with aiosqlite.connect(DATABASE) as db:
            placeholders = ",".join("?" * len(ach_ids))
            query = f"SELECT name FROM achievements WHERE id IN ({placeholders})"
            async with db.execute(query, ach_ids) as cursor:
                names = await cursor.fetchall()

        names = [n[0] for n in names]
        msg = "🏆 **Neue Achievements freigeschaltet!**\n" + "\n".join(f"✨ {name}" for name in names)
        await interaction.followup.send(msg)


async def user_exists(discord_id: int, db: aiosqlite.Connection) -> bool:
    async with db.execute("SELECT 1 FROM users_new WHERE discord_id = ?", (discord_id,)) as cursor:
        row = await cursor.fetchone()
        return row is not None

async def check_user_stats(discord_id: int, db: aiosqlite.Connection) -> int:
    """
    Stellt sicher, dass ein Eintrag in user_stats für den User existiert.
    Gibt die user_db_id zurück.
    """
    async with db.execute("SELECT discord_id FROM users_new WHERE discord_id = ?", (discord_id,)) as cursor:
        row = await cursor.fetchone()
        if not row:
            raise ValueError("User existiert nicht in users_new.")
        user_db_id = row[0]

    async with db.execute("SELECT 1 FROM user_stats WHERE user_id = ?", (user_db_id,)) as cursor:
        exists = await cursor.fetchone()
        if not exists:
            await db.execute("INSERT INTO user_stats (user_id) VALUES (?)", (user_db_id,))

    return user_db_id

# --- Command: /gamble <amount> ---
@bot.tree.command(name="gamble", description="Setze deine Münzen und versuche dein Glück!")
@app_commands.describe(amount="Anzahl der Münzen, die du setzen möchtest")
@commands.cooldown(1, 5, commands.BucketType.user)
async def gamble(interaction: discord.Interaction, amount: int):
    if amount <= 0:
        return await interaction.response.send_message("❌ Bitte setze einen positiven Betrag!", ephemeral=True)

    user_id = interaction.user.id

    # Eine Verbindung für alles
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute("SELECT coins FROM users_new WHERE discord_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            return await interaction.response.send_message(
                "⚠️ Du hast noch kein Profil. Nutze `/daily`, um anzufangen!", ephemeral=True
            )

        coins = row[0]
        if coins <= 0:
            return await interaction.response.send_message("💸 Du hast keine Münzen mehr!", ephemeral=True)

        if amount > coins:
            amount = coins  # All-in, wenn zu hoch gesetzt wird

        # Zufallswurf (eine Zeile, kein unnötiger Overhead)
        roll = random.random()
        
        # Wahrscheinlichkeiten in Reihenfolge (logisch gruppiert)
        if roll <= JACKPOT:  # Jackpot: 1 in 100 million
            multiplier, label, emoji = 0, "💎💎 JACKPOT 💎💎", None
        elif roll < FIFTY_X:
            multiplier, label, emoji = 50, "🔥🔥 ULTRA Gewinn!", "50-fache"
        elif roll < TEN_X:
            multiplier, label, emoji = 10, "🔥 Mega Gewinn!", "10-fache"
        elif roll < THREE_X:
            multiplier, label, emoji = 3, "🎉 Großer Gewinn!", "3-fache"
        elif roll < TWO_X:
            multiplier, label, emoji = 2, "✨ Kleiner Gewinn!", "2-fache"
        else:
            multiplier, label, emoji = 0, "😢 Verloren!", None

        # Münzberechnung in einer Zeile
        winnings = amount * multiplier
        new_coins = max(0, coins - amount + winnings)

        # Loses go to jackpot (total_coins, last_winner_id, last_won_at)

        async with db.execute("SELECT id, total_coins FROM jackpot ORDER BY id DESC LIMIT 1") as cursor:
            jackpot_row = await cursor.fetchone()

        if not jackpot_row:
            # Create jackpot if it doesn't exist
            await db.execute("INSERT INTO jackpot (total_coins) VALUES (0)")
            jackpot_id = db.lastrowid
            jackpot_total = 0
        else:
            jackpot_id, jackpot_total = jackpot_row

        if multiplier == 0:  # Player lost → money goes to jackpot
            jackpot_total += amount
            await db.execute("UPDATE jackpot SET total_coins = ? WHERE id = ?", (jackpot_total, jackpot_id))

        elif roll < 1 / 100_000_000:  # Jackpot win!
            winnings = jackpot_total
            new_coins = coins + winnings
            await db.execute(
                "UPDATE jackpot SET total_coins = 0, last_winner_id = ?, last_won_at = CURRENT_DATE WHERE id = ?",
                (user_id, jackpot_id)
            )
            
            label = f"💎💎 JACKPOT!!! Du hast {winnings:,} Münzen gewonnen! 💎💎"


        # Direktes Update (schneller als neue Verbindung)
        await db.execute("UPDATE users_new SET coins = ? WHERE discord_id = ?", (new_coins, user_id))
        await db.commit()
        await check_achievements(interaction.user.id, db)
    # Embed schnell rendern
    if multiplier > 0:
        result_msg = f"{label} Du hast das **{emoji}** gewonnen! (+{winnings:,} Münzen)"
        color = discord.Color.green()
    elif multiplier == 0 and roll < 1 / 140_000_000:
        result_msg = label
        color = discord.Color.gold()
    else:
        result_msg = f"{label} Du hast **{amount:,} Münzen** verloren."
        color = discord.Color.red()

    embed = discord.Embed(
        title="🎲 Glücksspiel Ergebnis",
        description=result_msg,
        color=color
    )
    embed.add_field(name="💰 Neuer Kontostand", value=f"**{new_coins:,} Münzen**", inline=False)
    embed.add_field(name="💎 Jackpot aktuell", value=f"**{jackpot_total:,} Münzen**", inline=False)

    await interaction.response.send_message(embed=embed)
    

# ======================
#   HILFSFUNKTIONEN
# ======================

async def load_active_autogambles():
    """Lädt beim Bot-Start pausierte Autogambles aus der DB und setzt sie fort."""
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS autogamble_sessions (
                user_id INTEGER PRIMARY KEY,
                amount INTEGER,
                update_interval INTEGER,
                max_retries INTEGER,
                rounds_done INTEGER,
                active INTEGER
            )
        """)
        await db.commit()

        async with db.execute("SELECT user_id, amount, update_interval, max_retries, rounds_done FROM autogamble_sessions WHERE active = 1") as cursor:
            sessions = await cursor.fetchall()

    for user_id, amount, update_interval, max_retries, rounds_done in sessions:
        channel = None
        for guild in bot.guilds:
            channel = discord.utils.get(guild.text_channels, name="casino")  # Passe den Channelnamen ggf. an
            if channel:
                break
        if channel:
            bot.loop.create_task(run_autogamble(channel, user_id, amount, update_interval, max_retries, rounds_done))


async def save_session(user_id, amount, update_interval, max_retries, rounds_done, active):
    """Speichert oder aktualisiert den Sessionstatus in der DB."""
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("""
            INSERT INTO autogamble_sessions (user_id, amount, update_interval, max_retries, rounds_done, active)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                amount=excluded.amount,
                update_interval=excluded.update_interval,
                max_retries=excluded.max_retries,
                rounds_done=excluded.rounds_done,
                active=excluded.active
        """, (user_id, amount, update_interval, max_retries, rounds_done, active))
        await db.commit()


# ======================
#   AUTOGAMBLE LOGIK
# ======================

async def run_autogamble(channel: discord.TextChannel, user_id: int, amount: int, update_interval: int, max_retries: int, start_round=0):
    """Führt den eigentlichen Autogamble-Loop aus."""
    async with db_lock:
        async with aiosqlite.connect(DATABASE) as db:
            async with db.execute("SELECT coins FROM users_new WHERE discord_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await channel.send(f"<@{user_id}> Du hast noch kein Profil! Nutze `/daily`, um anzufangen.")
                return
            coins = row[0]

    message = await channel.send(f"🎰 **Autogamble gestartet für <@{user_id}>!**\nEinsatz: `{amount}` | Update alle `{update_interval}` Runden")

    rounds = start_round
    total_win = 0
    total_loss = 0
    active_autogambles[user_id] = True
    await save_session(user_id, amount, update_interval, max_retries, rounds, 1)

    try:
        while active_autogambles.get(user_id, False) and coins >= amount and (max_retries == 0 or rounds < max_retries):
            rounds += 1
            roll = random.random()

            async with db_lock:
                async with aiosqlite.connect(DATABASE) as db:
                    async with db.execute("SELECT id, total_coins FROM jackpot ORDER BY id DESC LIMIT 1") as cursor:
                        jackpot_row = await cursor.fetchone()

                    if not jackpot_row:
                        await db.execute("INSERT INTO jackpot (total_coins) VALUES (0)")
                        jackpot_id = db.lastrowid
                        jackpot_total = 0
                    else:
                        jackpot_id, jackpot_total = jackpot_row

                    winnings = 0

                    if roll <= JACKPOT:
                        winnings = jackpot_total
                        coins += winnings
                        await db.execute(
                            "UPDATE jackpot SET total_coins = 0, last_winner_id = ?, last_won_at = CURRENT_DATE WHERE id = ?",
                            (user_id, jackpot_id)
                        )
                        total_win += winnings
                        await channel.send(f"🎉 Glückwunsch <@{user_id}>! Du hast den Jackpot gewonnen: `{winnings}` Münzen!")
                        await db.commit()
                    elif roll < FIFTY_X:
                        multiplier = 50
                        winnings = amount * multiplier
                        coins = coins - amount + winnings
                        total_win += winnings - amount
                    elif roll < TEN_X:
                        multiplier = 10
                        winnings = amount * multiplier
                        coins = coins - amount + winnings
                        total_win += winnings - amount
                    elif roll < THREE_X:
                        multiplier = 3
                        winnings = amount * multiplier
                        coins = coins - amount + winnings
                        total_win += winnings - amount
                    elif roll < TWO_X:
                        multiplier = 2
                        winnings = amount * multiplier
                        coins = coins - amount + winnings
                        total_win += winnings - amount
                    else:
                        coins -= amount
                        jackpot_total += amount
                        total_loss += amount
                        await db.execute("UPDATE jackpot SET total_coins = ? WHERE id = ?", (jackpot_total, jackpot_id))

                    await db.execute("UPDATE users_new SET coins = ? WHERE discord_id = ?", (coins, user_id))
                    await db.commit()

            await save_session(user_id, amount, update_interval, max_retries, rounds, 1)

            if rounds % update_interval == 0:
                profit = total_win - total_loss
                await message.edit(content=(
                    f"🎲 **Runde {rounds}**\n"
                    f"💰 Kontostand: `{coins:,}` Münzen\n"
                    f"📈 Gewinn/Verlust: `{profit:+,}` Münzen\n"
                    f"💎 Jackpot: `{jackpot_total:,}` Münzen"
                ))

            await asyncio.sleep(1.0)

        await message.edit(content=(
            f"🛑 **Autogamble beendet für <@{user_id}>!**\n"
            f"Runden: `{rounds}`\n"
            f"💰 Endstand: `{coins:,}` Münzen\n"
            f"📈 Gesamtgewinn: `{total_win - total_loss:+,}` Münzen\n"
            f"💎 Jackpot: `{jackpot_total:,}` Münzen"
        ))

    except Exception as e:
        await channel.send(f"❌ Fehler bei <@{user_id}>: {e}")
    finally:
        active_autogambles[user_id] = False
        await save_session(user_id, amount, update_interval, max_retries, rounds, 0)

# ======================
#   DISCORD COMMANDS
# ======================

@bot.tree.command(name="autogamble", description="Gamble automatisch, bis du abbrichst oder pleite bist!")
@app_commands.describe(
    amount="Einsatz pro Runde",
    update_intervalls="Update Intervall in Runden",
    max_retries="Maximale Anzahl an Runden (0 für unendlich)"
)
async def autogamble(interaction: discord.Interaction, amount: int, update_intervalls: Optional[int] = 5, max_retries: Optional[int] = 0):
    if amount <= 0:
        await interaction.response.send_message("❌ Bitte setze einen positiven Betrag!", ephemeral=True)
        return

    user_id = interaction.user.id
    if user_id in active_autogambles and active_autogambles[user_id]:
        await interaction.response.send_message("⚠️ Du hast bereits ein aktives Autogamble!", ephemeral=True)
        return

    await interaction.response.send_message(
        f"🎰 Autogamble wird gestartet für <@{user_id}>...\n"
        f"Einsatz: `{amount}` | Update alle `{update_intervalls}` Runden | Limit: {'∞' if max_retries == 0 else max_retries} Runden",
        ephemeral=False
    )

    bot.loop.create_task(run_autogamble(interaction.channel, user_id, amount, update_intervalls, max_retries))


@bot.tree.command(name="stop_autogamble", description="Stoppt dein laufendes Autogamble.")
async def stop_autogamble(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id not in active_autogambles or not active_autogambles[user_id]:
        await interaction.response.send_message("⚠️ Du hast kein aktives Autogamble!", ephemeral=True)
        return

    active_autogambles[user_id] = False
    await save_session(user_id, 0, 0, 0, 0, 0)
    await interaction.response.send_message("🛑 Dein Autogamble wurde gestoppt.", ephemeral=False)


class TradeView(View):
    def __init__(self, initiator, target, initiator_offer=None, target_offer=None):
        super().__init__(timeout=120)  # 2 Minuten Timeout
        self.initiator = initiator
        self.target = target
        self.initiator_offer = initiator_offer or {"coins": 0, "items": []}
        self.target_offer = target_offer or {"coins": 0, "items": []}
        self.initiator_ready = False
        self.target_ready = False

    async def update_message(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="💱 Handel zwischen Spielern",
            color=discord.Color.gold(),
            description=f"{self.initiator.mention} ↔ {self.target.mention}"
        )

        def offer_to_text(offer):
            items_text = "\n".join([f"🎒 {i['name']} ×{i['qty']}" for i in offer["items"]]) or "—"
            coins_text = f"{offer['coins']} 💰" if offer["coins"] > 0 else "—"
            return f"💰 Münzen: {coins_text}\n{items_text}"

        embed.add_field(name=f"👤 {self.initiator.display_name}", value=offer_to_text(self.initiator_offer), inline=True)
        embed.add_field(name=f"👤 {self.target.display_name}", value=offer_to_text(self.target_offer), inline=True)

        ready_text = (
            f"{'✅' if self.initiator_ready else '❌'} {self.initiator.display_name}\n"
            f"{'✅' if self.target_ready else '❌'} {self.target.display_name}"
        )
        embed.add_field(name="⚙️ Status", value=ready_text, inline=False)

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="💰 Münzen hinzufügen", style=discord.ButtonStyle.blurple)
    async def add_coins(self, interaction: discord.Interaction, button: Button):
        if interaction.user not in [self.initiator, self.target]:
            return await interaction.response.send_message("Nur Handelspartner können Münzen hinzufügen.", ephemeral=True)

        await interaction.response.send_message("Gib den Betrag ein, den du anbieten möchtest:", ephemeral=True)
        msg = await interaction.client.wait_for("message", check=lambda m: m.author == interaction.user)
        try:
            amount = int(msg.content)
            if amount <= 0:
                raise ValueError
        except ValueError:
            return await interaction.followup.send("Ungültiger Betrag.", ephemeral=True)

        if interaction.user == self.initiator:
            self.initiator_offer["coins"] = amount
            self.initiator_ready = False
        else:
            self.target_offer["coins"] = amount
            self.target_ready = False

        await self.update_message(interaction)

    @discord.ui.button(label="🎒 Item hinzufügen", style=discord.ButtonStyle.secondary)
    async def add_item(self, interaction: discord.Interaction, button: Button):
        if interaction.user not in [self.initiator, self.target]:
            return await interaction.response.send_message("Nur Handelspartner können Items hinzufügen.", ephemeral=True)

        await interaction.response.send_message("Gib den Itemnamen ein, den du handeln willst:", ephemeral=True)
        msg = await interaction.client.wait_for("message", check=lambda m: m.author == interaction.user)
        item_name = msg.content.strip()

        async with aiosqlite.connect(DATABASE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT quantity FROM inventory_new WHERE user_id = (SELECT id FROM users_new WHERE discord_id = ?) AND LOWER(item_name)=LOWER(?)",
                (interaction.user.id, item_name)
            ) as cur:
                row = await cur.fetchone()

        if not row or row["quantity"] <= 0:
            return await interaction.followup.send("Du besitzt dieses Item nicht.", ephemeral=True)

        if interaction.user == self.initiator:
            self.initiator_offer["items"].append({"name": item_name, "qty": 1})
            self.initiator_ready = False
        else:
            self.target_offer["items"].append({"name": item_name, "qty": 1})
            self.target_ready = False

        await self.update_message(interaction)

    @discord.ui.button(label="✅ Bereit", style=discord.ButtonStyle.green)
    async def ready(self, interaction: discord.Interaction, button: Button):
        if interaction.user == self.initiator:
            self.initiator_ready = True
        elif interaction.user == self.target:
            self.target_ready = True
        else:
            return await interaction.response.send_message("Du bist kein Handelspartner.", ephemeral=True)

        await self.update_message(interaction)

        if self.initiator_ready and self.target_ready:
            await self.complete_trade(interaction)

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(content="❌ Handel abgebrochen.", embed=None, view=None)

    async def complete_trade(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DATABASE) as db:
            # Münzen tauschen
            for user, offer in [(self.initiator, self.initiator_offer), (self.target, self.target_offer)]:
                async with db.execute("SELECT coins FROM users_new WHERE discord_id = ?", (user.id,)) as cur:
                    user_coins = (await cur.fetchone())[0]
                if user_coins < offer["coins"]:
                    await interaction.followup.send(f"❌ {user.display_name} hat nicht genug Münzen!", ephemeral=True)
                    return

            # Abzug
            await db.execute("UPDATE users_new SET coins = coins - ? WHERE discord_id = ?", (self.initiator_offer["coins"], self.initiator.id))
            await db.execute("UPDATE users_new SET coins = coins - ? WHERE discord_id = ?", (self.target_offer["coins"], self.target.id))

            # Gutschrift
            await db.execute("UPDATE users_new SET coins = coins + ? WHERE discord_id = ?", (self.target_offer["coins"], self.initiator.id))
            await db.execute("UPDATE users_new SET coins = coins + ? WHERE discord_id = ?", (self.initiator_offer["coins"], self.target.id))

            # Items tauschen
            for item in self.initiator_offer["items"]:
                await db.execute("UPDATE inventory_new SET quantity = quantity - 1 WHERE user_id = (SELECT id FROM users_new WHERE discord_id = ?) AND item_name = ?", (self.initiator.id, item["name"]))
                await db.execute("""
                    INSERT INTO inventory_new (user_id, item_name, quantity, item_rarity)
                    VALUES ((SELECT id FROM users_new WHERE discord_id = ?), ?, 1, (SELECT item_rarity FROM inventory_new WHERE item_name = ? LIMIT 1))
                    ON CONFLICT(user_id, item_name) DO UPDATE SET quantity = quantity + 1
                """, (self.target.id, item["name"], item["name"]))

            for item in self.target_offer["items"]:
                await db.execute("UPDATE inventory_new SET quantity = quantity - 1 WHERE user_id = (SELECT id FROM users_new WHERE discord_id = ?) AND item_name = ?", (self.target.id, item["name"]))
                await db.execute("""
                    INSERT INTO inventory_new (user_id, item_name, quantity, item_rarity)
                    VALUES ((SELECT id FROM users_new WHERE discord_id = ?), ?, 1, (SELECT item_rarity FROM inventory_new WHERE item_name = ? LIMIT 1))
                    ON CONFLICT(user_id, item_name) DO UPDATE SET quantity = quantity + 1
                """, (self.initiator.id, item["name"], item["name"]))

            await db.commit()

        await interaction.response.edit_message(content="✅ Handel abgeschlossen!", embed=None, view=None)


# --- Command: /trade <user> ---
@bot.tree.command(name="trade", description="Starte einen Handel mit einem anderen Spieler.")
@app_commands.describe(user="Der Benutzer, mit dem du handeln möchtest.")
async def trade(interaction: discord.Interaction, user: discord.User):
    if user.id == interaction.user.id:
        await interaction.response.send_message("Du kannst nicht mit dir selbst handeln 😅", ephemeral=True)
        return

    view = TradeView(initiator=interaction.user, target=user)
    embed = discord.Embed(
        title="💱 Neuer Handel gestartet",
        description=f"{interaction.user.mention} möchte mit {user.mention} handeln.\n"
                    "Fügt Münzen oder Items hinzu und bestätigt, wenn ihr bereit seid.",
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed, view=view)



# --- Command: /profile ---
@bot.tree.command(name="profile", description="Zeige dein Spielerprofil an")
async def profile(interaction: discord.Interaction):
    user_id = interaction.user.id

    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT coins, level, streak, last_daily FROM users_new WHERE discord_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()

    if row is None:
        await interaction.response.send_message("Du hast noch kein Profil. Nutze `/daily`, um anzufangen!", ephemeral=True)
        return

    coins, level, streak, last_daily = row

    embed = discord.Embed(
        title=f"📜 Profil von {interaction.user.display_name}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Level", value=level)
    embed.add_field(name="Coins", value=f"{coins} 💰")
    embed.add_field(name="Streak", value=f"{streak} Tage 🔥")
    embed.add_field(name="Letzter Daily", value=last_daily or "Noch nie")

    await interaction.response.send_message(embed=embed)

shop_lock = asyncio.Lock()

RARITY_COLORS = {
    "common": discord.Color.light_grey(),
    "uncommon": discord.Color.green(),
    "rare": discord.Color.blue(),
    "epic": discord.Color.purple(),
    "legendary": discord.Color.gold(),
}

shop_lock = asyncio.Lock()

# --- Command: /shop ---
@bot.tree.command(name="shop", description="Zeigt die verfügbaren Shop-Items des Tages an.")
async def shop(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    async with shop_lock:  # verhindert Race-Conditions bei gleichzeitigen Aufrufen
        async with aiosqlite.connect(DATABASE) as db:
            db.row_factory = aiosqlite.Row

            # Tabellen sicherstellen
            await db.execute("""
                CREATE TABLE IF NOT EXISTS shop_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    rarity TEXT DEFAULT 'common'
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS daily_shop (
                    date TEXT PRIMARY KEY,
                    item_ids TEXT NOT NULL
                )
            """)
            await db.commit()

            # Datum aus SQLite selbst bestimmen (lokale Zeit)
            async with db.execute("SELECT DATE('now','localtime') AS today") as cur:
                today = (await cur.fetchone())["today"]

            # Prüfen, ob für heute bereits ein Shop existiert
            async with db.execute("SELECT item_ids FROM daily_shop WHERE date = ?", (today,)) as cur:
                existing = await cur.fetchone()

            if existing:
                # Shop für heute laden
                item_ids = [int(i) for i in existing["item_ids"].split(",")]
            else:
                # neuen Shop generieren
                async with db.execute("SELECT id FROM shop_items ORDER BY RANDOM() LIMIT 5") as cur:
                    selected = await cur.fetchall()
                if not selected:
                    await interaction.followup.send("🛒 Der Shop ist derzeit leer.", ephemeral=True)
                    return

                item_ids = [row["id"] for row in selected]
                ids_str = ",".join(str(i) for i in item_ids)

                await db.execute(
                    "INSERT OR REPLACE INTO daily_shop (date, item_ids) VALUES (?, ?)",
                    (today, ids_str)
                )
                await db.commit()

            # Items abrufen (ALLE DB-Aufrufe innerhalb des Blocks)
            placeholders = ",".join("?" for _ in item_ids)
            async with db.execute(
                f"SELECT name, description, price, rarity FROM shop_items WHERE id IN ({placeholders})",
                item_ids
            ) as cur:
                items = await cur.fetchall()

            # coins von user abrufen
            async with db.execute(
                "SELECT coins FROM users_new WHERE discord_id = ?", (interaction.user.id,)
            ) as cur:
                user_row = await cur.fetchone()
                user_coins = user_row["coins"] if user_row else 0

    if not items:
        await interaction.followup.send("🛒 Der Shop ist heute leer.", ephemeral=True)
        return

    # Embed bauen
    embed = discord.Embed(
        title=f"🛒 Täglicher Shop – {today}.",
        description=f"Hier sind die heutigen Items! Kaufe sie mit `/buy <item>`.\n*Du hast `{user_coins}` Münzen*",
        color=discord.Color.blurple()
    )

    rarity_order = ["common", "uncommon", "rare", "epic", "legendary"]
    for item in items:
        embed.add_field(
            name=f"**{item['name']}** – {item['rarity'].capitalize()}",
            value=f"{item['description']}\n💰 **Preis:** `{item['price']}`",
            inline=False
        )

    highest_rarity = sorted(items, key=lambda i: rarity_order.index(i["rarity"]))[-1]["rarity"]
    embed.color = RARITY_COLORS.get(highest_rarity, discord.Color.blurple())

    await interaction.followup.send(embed=embed)


# --- Command: /achievements ---
@bot.tree.command(name="achievements", description="Zeige deine freigeschalteten Achievements an")
async def achievements(interaction: discord.Interaction):
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT discord_id FROM users_new WHERE discord_id = ?", (interaction.user.id,)
        ) as cur:
            user = await cur.fetchone()
        if not user:
            await interaction.response.send_message("Du hast noch kein Profil. Nutze `/daily` zuerst.", ephemeral=True)
            return

        user_db_id = interaction.user.id

        async with db.execute(
            "SELECT a.name, a.description, ua.achieved_at FROM achievements a "
            "JOIN user_achievements_new ua ON a.id = ua.achievement_id "
            "WHERE ua.user_id = ?", (user_db_id,)
        ) as cur:
            achievements = await cur.fetchall()
     

    if not achievements:
        await interaction.response.send_message("🏆 Du hast noch keine Achievements freigeschaltet.", ephemeral=True)
        return

    embed = discord.Embed(title=f"🏆 Achievements von {interaction.user.display_name}", color=discord.Color.gold())
    for name, description, achieved_at in achievements:
        embed.add_field(name=name, value=f"{description}\n*Freigeschaltet am {achieved_at}*", inline=False)

    await interaction.response.send_message(embed=embed)

# --- Command: /showachievements ---
@bot.tree.command(name="showachievements", description="Zeige alle verfügbaren Achievements an")
async def showachievements(interaction: discord.Interaction):
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT name, description, condition, reward_coins FROM achievements"
        ) as cur:
            achievements = await cur.fetchall()

    if not achievements:
        await interaction.response.send_message("🏆 Es sind keine Achievements verfügbar.", ephemeral=True)
        return

    embed = discord.Embed(title="🏆 Verfügbare Achievements", color=discord.Color.gold())
    for name, description, condition, reward in achievements:
        embed.add_field(
            name=name,
            value=f"{description}\n**Bedingung:** {condition}\n**Belohnung:** {reward} Münzen",
            inline=False
        )

    await interaction.response.send_message(embed=embed)



# --- Command: /use ---
@bot.tree.command(name="use", description="Benutze ein Item aus deinem Inventar")
@app_commands.describe(item_name="Name des Items, das du benutzen möchtest")
async def use(interaction: discord.Interaction, item_name: str):
    user_id = interaction.user.id
    async with aiosqlite.connect(DATABASE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT discord_id FROM users_new WHERE discord_id = ?", (user_id,)) as cur:
            user = await cur.fetchone()

        if not user:
            await interaction.response.send_message("❌ Du hast noch kein Profil. Nutze `/daily`, um anzufangen.", ephemeral=True)
            return

        # Item im Inventar prüfen
        async with db.execute(
            "SELECT quantity FROM inventory_new WHERE user_id = ? AND item_name = ?",
            (user_id, item_name)
        ) as cur:
            item = await cur.fetchone()

        if not item or item["quantity"] <= 0:
            await interaction.response.send_message(f"❌ Du besitzt kein Item namens **{item_name}**.", ephemeral=True)
            return

        # Item-Effekt (Beispiel: Auto-Miner aktivieren)
        if item_name.lower() == "auto-miner":
            # Logik zum Aktivieren des Auto-Miners hier einfügen
            await interaction.response.send_message("🤖 Du hast den Auto-Miner aktiviert! Er wird jetzt automatisch Ressourcen für dich abbauen.", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ Das Item **{item_name}** kann nicht benutzt werden.", ephemeral=True)
            return
        
        if item_name.lower() == "energydrink":
            # Energie auffüllen
            async with db.execute("SELECT energy, max_energy FROM users_new WHERE id = ?", (user_id,)) as cur:
                user_stats = await cur.fetchone()
            new_energy = min(user_stats["energy"] + 50, user_stats["max_energy"])
            await db.execute(
                "UPDATE users_new SET energy = ? WHERE id = ?",
                (new_energy, user_id)
            )
            await interaction.followup.send(f"⚡ Deine Energie wurde um 50 aufgefüllt! Aktuelle Energie: `{new_energy}/{user_stats['max_energy']}`", ephemeral=True)
        
        # Item verbrauchen
        new_quantity = item["quantity"] - 1
        if new_quantity > 0:
            await db.execute(
                "UPDATE inventory_new SET quantity = ? WHERE user_id = ? AND item_name = ?",
                (new_quantity, user_id, item_name)
            )
        else:
            await db.execute(
                "DELETE FROM inventory_new WHERE user_id = ? AND item_name = ?",
                (user_id, item_name)
            )

        await db.commit()

active_automines = {}

# --- Command: /mine ---
@bot.tree.command(name="mine", description="Mine Ressourcen aus deiner Mine!")
async def mine(interaction: discord.Interaction):
    user_id = interaction.user.id
    async with aiosqlite.connect(DATABASE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users_new WHERE discord_id = ?", (user_id,)) as cur:
            user = await cur.fetchone()

        if not user:
            await interaction.response.send_message("❌ Du hast noch kein Profil. Nutze `/daily`, um anzufangen.", ephemeral=True)
            return

        # Energie auffüllen, falls ein neuer Tag ist
        today = date.today().isoformat()
        energy = user["energy"]
        max_energy = user["max_energy"]
        last_refill = user["last_energy_refill"]

        if last_refill != today:
            energy = max_energy
            await db.execute(
                "UPDATE users_new SET energy = ?, last_energy_refill = ? WHERE discord_id = ?",
                (energy, today, user_id)
            )

        if energy < 10:
            await interaction.response.send_message("💤 Du hast keine Energie mehr! Warte bis morgen, um wieder zu minen.", ephemeral=True)
            return

        # Energieverbrauch
        energy -= 10

        # XP + Level-Up Logik
        mining_level = user["mining_level"]
        mining_xp = user["mining_xp"]
        xp_gain = random.randint(10, 20)
        mining_xp += xp_gain
        xp_needed = mining_level * 100

        leveled_up = False
        if mining_xp >= xp_needed:
            mining_level += 1
            mining_xp -= xp_needed
            max_energy += 5
            leveled_up = True

        # Mining-Ertrag (Coins)
        base = random.randint(50, 200)
        multiplier = 1 + (mining_level - 1) * 0.05
        mined_amount = int(base * multiplier)
        new_coins = user["coins"] + mined_amount

        # 🎁 Item Drop Chance
        # Drop-Pool (mit Gewichtung)
        possible_drops = [
            ("Fackel", "common", 0.10),
            ("Goldbarren", "epic", 0.01),
            ("Edelstein", "rare", 0.005),
            ("Auto-Miner", "legendary", 0.000001),  # 1 zu 1 Million
        ]

        found_item = None
        for name, rarity, chance in possible_drops:
            if random.random() < chance:
                found_item = (name, rarity)
                break

        # Item ins Inventar speichern
        if found_item:
            name, rarity = found_item
            # prüfen, ob der User das Item schon hat
            async with db.execute(
                "SELECT quantity FROM inventory_new WHERE user_id = ? AND item_name = ?",
                (user["discord_id"], name)
            ) as cur:
                row = await cur.fetchone()

            if row:
                await db.execute(
                    "UPDATE inventory_new SET quantity = quantity + 1 WHERE user_id = ? AND item_name = ?",
                    (user["discord_id"], name)
                )
            else:
                await db.execute(
                    "INSERT INTO inventory_new (user_id, item_name, quantity, item_rarity) VALUES (?, ?, ?, ?)",
                    (user["discord_id"], name, 1, rarity)
                )

        # User updaten
        await db.execute("""
            UPDATE users_new 
            SET coins = ?, mining_xp = ?, mining_level = ?, energy = ?, max_energy = ? 
            WHERE discord_id = ?
        """, (new_coins, mining_xp, mining_level, energy, max_energy, user_id))
        await db.commit()
        await check_achievements(interaction.user.id, db)

    # Nachricht zusammenbauen
    msg = (
        f"⛏️ Du hast **{mined_amount} Münzen** aus deiner Mine gewonnen!\n"
        f"⚡ Energie: `{energy}/{max_energy}`\n"
        f"📈 Mining-XP: `{mining_xp}/{xp_needed}`\n"
        f"💎 Level: `{mining_level}`"
    )

    if leveled_up:
        msg += "\n🎉 **Level Up!** Deine maximale Energie wurde erhöht!"

    if found_item:
        name, rarity = found_item
        rarity_emojis = {
            "common": "⚪",
            "uncommon": "🟢",
            "rare": "🔵",
            "epic": "🟣",
            "legendary": "🟡"
        }
        msg += f"\n🎁 Du hast beim Minen ein Item gefunden: **{name}** {rarity_emojis.get(rarity, '')}!"

    await interaction.response.send_message(msg)


# ==============================
#   AUTO-MINE COMMANDS
# ==============================
@bot.tree.command(name="automine", description="Starte automatisches Mining (benötigt Auto-Miner Item).")
async def automine(interaction: discord.Interaction):
    user_id = interaction.user.id

    # Prüfen, ob bereits aktiv
    if user_id in active_automines and active_automines[user_id]["active"]:
        await interaction.response.send_message("⚠️ Dein Auto-Miner läuft bereits!", ephemeral=True)
        return

    async with aiosqlite.connect(DATABASE) as db:
        # Check auf Item-Besitz
        async with db.execute("""
            SELECT quantity FROM inventory_new 
            WHERE user_id = ?
              AND item_name = 'Auto-Miner'
        """, (user_id,)) as cur:
            item_row = await cur.fetchone()

        if not item_row or item_row[0] <= 0:
            await interaction.response.send_message("❌ Du besitzt keinen Auto-Miner! Kaufe ihn im Shop.", ephemeral=True)
            return

    await interaction.response.send_message("✅ Auto-Miner gestartet! Du erhältst alle 10 Minuten Münzen.", ephemeral=False)

    async def auto_mine_task():
        """Dauerhafter Mining-Loop (mit kontrolliertem Sleep)."""
        interval = 600  # 10 Minuten
        check_delay = 5  # alle 5 Sekunden prüfen

        last_mine = asyncio.get_event_loop().time()

        while active_automines.get(user_id, {}).get("active", False):
            await asyncio.sleep(check_delay)
            elapsed = asyncio.get_event_loop().time() - last_mine

            # Wenn 10 Minuten vorbei → Mining ausführen
            if elapsed >= interval:
                last_mine = asyncio.get_event_loop().time()

                mined_amount = random.randint(50, 200)
                async with db_lock:
                    async with aiosqlite.connect(DATABASE) as db:
                        async with db.execute("SELECT discord_id, coins FROM users_new WHERE discord_id = ?", (user_id,)) as cur:
                            user_row = await cur.fetchone()
                        if not user_row:
                            continue

                        user_db_id, coins = user_row
                        coins += mined_amount
                        await db.execute("UPDATE users_new SET coins = ? WHERE discord_id = ?", (coins, user_db_id))
                        await db.commit()

                try:
                    await interaction.user.send(f"⛏️ Dein Auto-Miner hat **{mined_amount} Münzen** gesammelt!\n💰 Neuer Kontostand: `{coins}` Münzen.")
                except discord.Forbidden:
                    await interaction.channel.send(f"⛏️ <@{user_id}>, dein Auto-Miner hat **{mined_amount} Münzen** gesammelt! 💰")


    # Task speichern
    task = bot.loop.create_task(auto_mine_task())
    active_automines[user_id] = {"task": task, "active": True}

    # Optional persistieren
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS automine_sessions (
                user_id INTEGER PRIMARY KEY,
                active INTEGER
            )
        """)
        await db.execute("INSERT OR REPLACE INTO automine_sessions (user_id, active) VALUES (?, ?)", (user_id, 1))
        await db.commit()


@bot.tree.command(name="stop_automine", description="Stoppt deinen laufenden Auto-Miner.")
async def stop_automine(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id not in active_automines or not active_automines[user_id]["active"]:
        await interaction.response.send_message("⚠️ Du hast keinen aktiven Auto-Miner!", ephemeral=True)
        return

    active_automines[user_id]["active"] = False
    active_automines[user_id]["task"].cancel()

    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("UPDATE automine_sessions SET active = 0 WHERE user_id = ?", (user_id,))
        await db.commit()

    await interaction.response.send_message("🛑 Dein Auto-Miner wurde gestoppt.", ephemeral=False)


async def automine_restart_task(channel: discord.TextChannel, user_id: int):
    """Wird beim Neustart aufgerufen, um Auto-Miner-Sessions fortzusetzen."""
    active_automines[user_id] = {"active": True, "task": None}

    async def resumed_task():
        while active_automines.get(user_id, {}).get("active", False):
            await asyncio.sleep(600)
            mined_amount = random.randint(50, 200)
            async with db_lock:
                async with aiosqlite.connect(DATABASE) as db:
                    async with db.execute("SELECT discord_id, coins FROM users_new WHERE discord_id = ?", (user_id,)) as cur:
                        user_row = await cur.fetchone()
                    if not user_row:
                        continue

                    user_db_id, coins = user_row
                    coins += mined_amount
                    await db.execute("UPDATE users_new SET coins = ? WHERE discord_id = ?", (coins, user_db_id))
                    await db.commit()
            await channel.send(f"⛏️ <@{user_id}>, dein Auto-Miner hat **{mined_amount} Münzen** gesammelt! 💰")

    task = bot.loop.create_task(resumed_task())
    active_automines[user_id]["task"] = task


# --- Command: /sync ---
@bot.tree.command(name="sync", description="Synchronisiert alle Slash-Commands (nur für den Bot-Besitzer).")
async def sync(interaction: discord.Interaction):
    # Only allow the bot owner to use it
    app_info = await bot.application_info()
    if interaction.user.id != app_info.owner.id:
        await interaction.response.send_message("❌ Du bist nicht berechtigt, diesen Befehl zu verwenden.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        synced = await bot.tree.sync()
        await interaction.followup.send(f"✅ Erfolgreich {len(synced)} Slash-Commands synchronisiert.")
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler beim Synchronisieren: `{e}`")


# --- Command: /buy <item> ---
@bot.tree.command(name="buy", description="Kaufe ein Item aus dem Shop")
@app_commands.describe(item="Name des Items, das du kaufen willst", quantity="Anzahl der zu kaufenden Items")
async def buy(interaction: discord.Interaction, item: str, quantity: int = 1):
    await interaction.response.defer(thinking=True)
    user_id = interaction.user.id

    item = item.title()  # automatische Kapitalisierung

    async with aiosqlite.connect(DATABASE) as db:
        # Hole Item aus Shop
        async with db.execute(
            "SELECT id, price, rarity FROM shop_items WHERE LOWER(name) = LOWER(?)",
            (item.lower(),)
        ) as cur:
            item_row = await cur.fetchone()

        if not item_row:
            await interaction.followup.send(f"❌ {item} existiert nicht im Shop.", ephemeral=True)
            return


        item_id, price, rarity = item_row

        price *= quantity  # Gesamtpreis berechnen

        # Hole Userdaten
        async with db.execute("SELECT discord_id, coins FROM users_new WHERE discord_id = ?", (user_id,)) as cur:
            user_row = await cur.fetchone()

        if not user_row:
            await interaction.followup.send(
                "Du musst zuerst `/daily` nutzen, um ein Profil zu erstellen.",
                ephemeral=True
            )
            return

        user_db_id, coins = user_row

        if coins < price:
            await interaction.followup.send("💸 Du hast nicht genug Münzen!", ephemeral=True)
            return

        # Münzen abziehen
        new_coins = coins - price
        await db.execute("UPDATE users_new SET coins = ? WHERE discord_id = ?", (new_coins, user_db_id))

        # Item ins Inventar packen
        async with db.execute(
            "SELECT quantity FROM inventory_new WHERE user_id = ? AND item_name = ?",
            (user_db_id, item)
        ) as cur:
            inv_row = await cur.fetchone()

        if inv_row:
            new_quantity = inv_row[0] + quantity
            await db.execute(
                "UPDATE inventory_new SET quantity = ? WHERE user_id = ? AND item_name = ?",
                (new_quantity, user_db_id, item)
            )
        else:
            await db.execute(
                "INSERT INTO inventory_new (user_id, item_name, quantity, item_rarity) VALUES (?, ?, ?, ?)",
                (user_db_id, item, quantity, rarity)
            )

        await db.commit()

    # ✅ Jetzt mit neuer Verbindung Achievements prüfen
    async with aiosqlite.connect(DATABASE) as adb:
        await check_achievements(interaction.user.id, adb)

    await interaction.followup.send(f"✅ Du hast **{quantity}x {item}** für **{price} 💰** gekauft!")


# --- Command: /sell ---
@bot.tree.command(name="sell", description="Verkaufe ein Item aus deinem Inventar")
@app_commands.describe(item="Name des Items, das du verkaufen willst", quantity="Anzahl der zu verkaufenden Items")
async def sell(interaction: discord.Interaction, item: str, quantity: int = 1):
    await interaction.response.defer(thinking=True)
    user_id = interaction.user.id
    item = item.capitalize()

    if quantity <= 0:
        await interaction.followup.send("❌ Die Menge muss mindestens 1 sein.", ephemeral=True)
        return

    async with aiosqlite.connect(DATABASE) as db:
        # Hole Userdaten
        async with db.execute("SELECT discord_id FROM users_new WHERE discord_id = ?", (user_id,)) as cur:
            user_row = await cur.fetchone()

        if not user_row:
            await interaction.followup.send("Du musst zuerst `/daily` nutzen, um ein Profil zu erstellen.", ephemeral=True)
            return

        user_db_id = user_row[0]

        # Hole Item aus Inventar
        async with db.execute("SELECT quantity FROM inventory_new WHERE user_id = ? AND item_name = ?", (user_db_id, item)) as cur:
            inv_row = await cur.fetchone()

        if not inv_row or inv_row[0] < quantity:
            await interaction.followup.send("❌ Du hast nicht genug von diesem Item zum Verkaufen.", ephemeral=True)
            return

        current_quantity = inv_row[0]

        # Hole Itempreis aus Shop
        async with db.execute("SELECT price FROM shop_items WHERE LOWER(name) = LOWER(?)", (item,)) as cur:
            shop_row = await cur.fetchone()

        if not shop_row:
            await interaction.followup.send("❌ Dieses Item kann nicht verkauft werden.", ephemeral=True)
            return

        price = shop_row[0]
        sell_price = price  # Verkaufspreis ist der Kaufpreis (anpassbar)
        total_earnings = sell_price * quantity

        # Update Inventar
        new_quantity = current_quantity - quantity
        if new_quantity > 0:
            await db.execute("UPDATE inventory_new SET quantity = ? WHERE user_id = ? AND item_name = ?", (new_quantity, user_db_id, item))
        else:
            await db.execute("DELETE FROM inventory_new WHERE user_id = ? AND item_name = ?", (user_db_id, item))

        # Update Münzen
        async with db.execute("SELECT coins FROM users_new WHERE discord_id = ?", (user_db_id,)) as cur:
            coins_row = await cur.fetchone()
            new_coins = coins_row[0] + total_earnings

        await db.execute("UPDATE users_new SET coins = ? WHERE discord_id = ?", (new_coins, user_db_id))
        await db.commit()

    await interaction.followup.send(f"✅ Du hast **{quantity}x {item}** für **{total_earnings} 💰** verkauft!")

# --- Command: /inventory ---
@bot.tree.command(name="inventory", description="Zeige dein Inventar oder das eines anderen Spielers an")
@app_commands.describe(user="(Optional) Der Benutzer, dessen Inventar du sehen möchtest")
async def inventory(interaction: discord.Interaction, user: discord.User | None = None):
    target = user or interaction.user  
    target_id = target.id

    async with aiosqlite.connect(DATABASE) as db:
        # Nutzer-ID prüfen
        async with db.execute("SELECT coins FROM users_new WHERE discord_id = ?", (target_id,)) as cur:
            user_row = await cur.fetchone()

        if not user_row:
            msg = f"{target.display_name} hat noch kein Profil." if user else "Du hast noch kein Profil. Nutze `/daily` zuerst."
            await interaction.response.send_message(msg, ephemeral=True)
            return

        coins = user_row[0]

        # Inventar abrufen
        async with db.execute(
            "SELECT item_name, quantity, item_rarity FROM inventory_new WHERE user_id = ?", (target_id,)
        ) as cur:
            items = await cur.fetchall()

        # Preisliste für alle Items laden (einmalig)
        item_prices = {}
        async with db.execute("SELECT LOWER(name), price FROM shop_items") as cur:
            for name, price in await cur.fetchall():
                item_prices[name] = price

    embed = discord.Embed(title=f"🎒 Inventar von {target.display_name}", color=discord.Color.purple())

    # Wenn Inventar leer
    if not items:
        embed.description = "Das Inventar ist leer."

    # Items nach Seltenheit sortieren
    rarity_order = {"legendary": 4, "epic": 3, "rare": 2, "uncommon": 1, "common": 0}
    items.sort(key=lambda x: rarity_order.get(x[2], 0), reverse=True)

    # Raritäts-Block für hübsche Anzeige
    def rarity_block(rarity: str) -> str:
        match rarity:
            case "common": return "```md\n> Common\n```"
            case "uncommon": return "```yaml\nUncommon\n```"
            case "rare": return "```fix\nRare\n```"
            case "epic": return "```asciidoc\n.Epic\n```"
            case "legendary": return "```ml\nLegendary\n```"
            case _: return rarity
    embed.add_field(name="💰 Münzen", value=f"{coins} 💰 ", inline=True)
    embed.add_field(name="💎 Gegenstandswert", value=f"berechne...", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)  # Leerfeld für Layout
    # Einzel-Item-Infos + Gesamtwert
    total_value = 0
    for name, qty, rarity in items:
        price = item_prices.get(name.lower(), 0)
        value = price * qty
        total_value += value

        embed.add_field(
            name=name,
            value=f"Menge: `{qty}`\nWert pro Stück: `{price} 💰`\nGesamt: `{value} 💰`\n{rarity_block(rarity)}",
            inline=True
        )
    # Gesamtwert aktualisieren
    embed.set_field_at(1, name="💎 Gegenstandswert", value=f"{total_value} 💰", inline=True)

    await interaction.response.send_message(embed=embed)


# --- Command: /leaderboard ---
@bot.tree.command(name="leaderboard", description="Zeige die Top-Spieler")
async def leaderboard(interaction: discord.Interaction):
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT discord_id, coins FROM users_new ORDER BY coins DESC LIMIT 10"
        ) as cur:
            top_players = await cur.fetchall()

    if not top_players:
        await interaction.response.send_message("Noch keine Spieler auf der Rangliste!", ephemeral=True)
        return

    embed = discord.Embed(title="🏆 Leaderboard", color=discord.Color.orange())
    for rank, (discord_id, coins) in enumerate(top_players, start=1):
        user = await bot.fetch_user(discord_id)
        embed.add_field(name=f"{rank}. {user.display_name}", value=f"{coins} 💰", inline=False)

    await interaction.response.send_message(embed=embed)


async def change_status():
    statuses = [
        "auto Gamble 🎲",
        "den ultra Dungeon 🏰",
        "Schere-Stein-Papier ✂️🪨📄",
        ]
    
    while True:
        for status in statuses:
            await bot.change_presence(activity=discord.Game(name=status))
            await asyncio.sleep(300)

@bot.event
async def on_member_join(member):
    if member.guild.system_channel:
        embed = discord.Embed(
            title=f"🎉 Willkommen {member.name}!",
            description=f"{member.mention} ist dem Server beigetreten!\n\nDu bist Mitglied #{member.guild.member_count}",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await member.guild.system_channel.send(embed=embed)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    # AFK-Check
    if message.author.id in user_data and user_data[message.author.id].get("afk"):
        user_data[message.author.id]["afk"] = False
        seit = user_data[message.author.id]["seit"]
        dauer = datetime.now() - seit
        minuten = int(dauer.total_seconds() / 60)
        await message.channel.send(f"👋 Willkommen zurück, {message.author.mention}! Du warst {minuten} Minute(n) AFK.")
    
    # Erwähnungen von AFK-Usern
    for mention in message.mentions:
        if mention.id in user_data and user_data[mention.id].get("afk"):
            grund = user_data[mention.id]["grund"]
            await message.channel.send(f"💤 {mention.name} ist gerade AFK: {grund}")
    
    await bot.process_commands(message)


# Bei gewinn kriegt man 5 coins, bei unentschieden 2 coins
@bot.tree.command(name="rps", description="Spiele Schere-Stein-Papier gegen den Bot!")
@app_commands.describe(wahl="Deine Wahl: schere, stein oder papier")
@app_commands.choices(wahl=[
    app_commands.Choice(name="✂️ Schere", value="schere"),
    app_commands.Choice(name="🪨 Stein", value="stein"),
    app_commands.Choice(name="📄 Papier", value="papier")
])
@commands.cooldown(1, 10, commands.BucketType.user)
async def rps(interaction: discord.Interaction, wahl: app_commands.Choice[str]):
    bot_wahl = random.choice(["schere", "stein", "papier"])
    ergebnisse = {
        ("schere", "papier"): "gewonnen",
        ("papier", "stein"): "gewonnen",
        ("stein", "schere"): "gewonnen",
        ("papier", "schere"): "verloren",
        ("stein", "papier"): "verloren",
        ("schere", "stein"): "verloren"
    }
    
    if wahl.value == bot_wahl:
        ergebnis = "unentschieden"
    else:
        ergebnis = ergebnisse.get((wahl.value, bot_wahl), "verloren")
    
    farben = {"gewonnen": discord.Color.green(), "verloren": discord.Color.red(), "unentschieden": discord.Color.gold()}
    nachrichten = {
        "gewonnen": "🎉 Du hast gewonnen! +5 Münzen",
        "verloren": "😢 Du hast verloren! -3 Münzen",
        "unentschieden": "🤝 Unentschieden! +2 Münzen"
    }
    
    embed = discord.Embed(title="🪨 Schere-Stein-Papier", color=farben[ergebnis])
    embed.add_field(name="Deine Wahl", value=wahl.name, inline=True)
    embed.add_field(name="Bot's Wahl", value={"schere": "✂️ Schere", "stein": "🪨 Stein", "papier": "📄 Papier"}[bot_wahl], inline=True)
    embed.add_field(name="Ergebnis", value=nachrichten[ergebnis], inline=False)
    
    await interaction.response.send_message(embed=embed)
    
    if ergebnis in ["gewonnen", "unentschieden"]:
        user_id = interaction.user.id
        async with aiosqlite.connect(DATABASE) as db:
            async with db.execute("SELECT coins FROM users_new WHERE discord_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
            if row:
                coins = row[0]
                if ergebnis == "gewonnen":
                    coins += 5
                elif ergebnis == "unentschieden":
                    coins += 2
                else:
                    coins -= 3
                # check if coins go below 0
                if coins < 0:
                    coins = 0
                
                await db.execute("UPDATE users_new SET coins = ? WHERE discord_id = ?", (coins, user_id))
                await db.commit()


# --- Command: /trivia ---
@bot.tree.command(name="trivia", description="Teste dein Wissen mit einer zufälligen Trivia-Frage!")
async def trivia(interaction: discord.Interaction):
    await interaction.response.defer()

    # Trivia-Frage abrufen
    async with aiohttp.ClientSession() as session:
        async with session.get("https://opentdb.com/api.php?amount=1&type=multiple") as resp:
            data = await resp.json()

    if not data or data["response_code"] != 0:
        await interaction.followup.send("⚠️ Konnte keine Trivia-Frage abrufen. Bitte versuch es später erneut.")
        return

    frage_data = data["results"][0]
    frage_text = html.unescape(frage_data["question"])
    richtige_antwort = html.unescape(frage_data["correct_answer"])
    falsche_antworten = [html.unescape(ans) for ans in frage_data["incorrect_answers"]]

    # Antworten mischen
    optionen = falsche_antworten + [richtige_antwort]
    random.shuffle(optionen)

    # Embed erstellen
    embed = discord.Embed(
        title="❓ Trivia Frage",
        description=frage_text,
        color=discord.Color.blurple()
    )
    embed.set_footer(text=f"Kategorie: {frage_data['category']} | Schwierigkeit: {frage_data['difficulty'].capitalize()}")

    for i, option in enumerate(optionen, start=1):
        embed.add_field(name=f"Option {i}", value=option, inline=False)

    embed.add_field(name="⏰ Zeitlimit", value="20 Sekunden – antworte mit der Nummer deiner Wahl!", inline=False)
    await interaction.followup.send(embed=embed)

    antworten = {}  # user_id -> Antwort

    def check(m):
        content = re.sub(r"\|\|(.+?)\|\|", r"\1", m.content.strip())

        return (
            m.channel == interaction.channel
            and content.isdigit()
            and 1 <= int(content) <= len(optionen)
        )

    end_time = asyncio.get_event_loop().time() + 20

    async def collect_answers():
        while True:
            try:
                msg = await bot.wait_for("message", timeout=end_time - asyncio.get_event_loop().time(), check=check)
                user_id = msg.author.id
                if user_id not in antworten:
                    antworten[user_id] = optionen[int(msg.content) - 1]
            except asyncio.TimeoutError:
                break

    async def send_timers():
        await asyncio.sleep(10)  # nach 10 Sekunden
        await interaction.channel.send("⏳ Nur noch **10 Sekunden**!")
        await asyncio.sleep(5)
        await interaction.channel.send("⚠️ Nur noch **5 Sekunden!** Schnell antworten!")
        await asyncio.sleep(5)  # Zeit vorbei

    # Beide Tasks parallel starten
    await asyncio.gather(collect_answers(), send_timers())


    # Auswertung
    if not antworten:
        await interaction.channel.send(f"⏰ Zeit abgelaufen! Niemand hat geantwortet. Die richtige Antwort war **{richtige_antwort}**.")
        return

    richtiges_user_set = []
    falsches_user_set = []

    async with aiosqlite.connect(DATABASE) as db:
        for user_id, antwort in antworten.items():
            async with db.execute("SELECT coins FROM users_new WHERE discord_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()

            coins = row[0] if row else 0

            if antwort == richtige_antwort:
                coins += 10
                richtiges_user_set.append((user_id, coins))
            else:
                coins -= 5
                falsches_user_set.append((user_id, coins))

            await db.execute("INSERT OR REPLACE INTO users_new (discord_id, coins) VALUES (?, ?)", (user_id, coins))
        await db.commit()

    # Ergebnis-Embed
    ergebnis = discord.Embed(
        title="🧠 Trivia Ergebnis",
        description=f"**Richtige Antwort:** {richtige_antwort}",
        color=discord.Color.green()
    )

    if richtiges_user_set:
        richtig_str = "\n".join(
            [f"✅ <@{uid}> (+10 Münzen) | Neuer Kontostand: {coins}" for uid, coins in richtiges_user_set]
        )
        ergebnis.add_field(name="🎉 Richtige Antworten", value=richtig_str, inline=False)
    else:
        ergebnis.add_field(name="🎉 Richtige Antworten", value="Niemand 😢", inline=False)

    if falsches_user_set:
        falsch_str = "\n".join(
            [f"❌ <@{uid}> (-5 Münzen) | Neuer Kontostand: {coins}" for uid, coins in falsches_user_set]
        )
        ergebnis.add_field(name="💀 Falsche Antworten", value=falsch_str, inline=False)

    await interaction.channel.send(embed=ergebnis)


# --- Command: /guessthenumber ---
@bot.tree.command(name="guessthenumber", description="Rate die Zahl zwischen 1-100!")
async def guessthenumber(interaction: discord.Interaction):
    number = random.randint(1, 100)
    attempts = 0
    max_attempts = 7
    
    embed = discord.Embed(
        title="🔢 Rate die Zahl!",
        description=f"Ich habe mir eine Zahl zwischen 1 und 100 ausgedacht.\nDu hast {max_attempts} Versuche!",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed)
    
    def check(m):
        return m.author == interaction.user and m.channel == interaction.channel and m.content.isdigit()
    
    while attempts < max_attempts:
        try:
            guess_msg = await bot.wait_for('message', timeout=30.0, check=check)
            guess = int(guess_msg.content)
            attempts += 1
            
            if guess == number:
                win_embed = discord.Embed(
                    title="🎉 Gewonnen!",
                    description=f"Glückwunsch! Die Zahl war **{number}**!\nDu hast {attempts} Versuch(e) gebraucht.",
                    color=discord.Color.green()
                )
                await interaction.channel.send(embed=win_embed)
                return
            elif guess < number:
                hint = "📈 Die gesuchte Zahl ist **höher**!"
            else:
                hint = "📉 Die gesuchte Zahl ist **niedriger**!"
            
            remaining = max_attempts - attempts
            if remaining > 0:
                await interaction.channel.send(f"{hint} Noch {remaining} Versuch(e) übrig!")
            
        except asyncio.TimeoutError:
            timeout_embed = discord.Embed(
                title="⏰ Zeit abgelaufen!",
                description=f"Die Zahl war **{number}**. Versuche es nochmal mit `/guessthenumber`!",
                color=discord.Color.red()
            )
            await interaction.channel.send(embed=timeout_embed)
            return
    
    lose_embed = discord.Embed(
        title="😔 Verloren!",
        description=f"Du hast keine Versuche mehr! Die Zahl war **{number}**.",
        color=discord.Color.red()
    )
    await interaction.channel.send(embed=lose_embed)

@bot.tree.command(name="avatar", description="Zeigt deinen oder den Avatar eines anderen Benutzers an.")
@app_commands.describe(user="Der Benutzer, dessen Avatar du sehen möchtest (optional)")
async def avatar(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user  # Wenn kein User angegeben ist, nimm den eigenen
    
    avatar_url = user.avatar.url if user.avatar else user.default_avatar.url

    embed = discord.Embed(
        title=f"🖼️ Avatar von {user.name}",
        color=discord.Color.blurple()
    )
    embed.set_image(url=avatar_url)
    embed.set_footer(text=f"Angefordert von {interaction.user.name}", icon_url=interaction.user.avatar.url)

    await interaction.response.send_message(embed=embed)

# Starte den Bot

bot.run(os.getenv("TOKEN"))