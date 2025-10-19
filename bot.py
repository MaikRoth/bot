import os
import discord
from discord import app_commands
from discord.ext import commands
import random
from datetime import datetime, timedelta
import asyncio
from typing import Optional
import random
from datetime import date, timedelta
import aiosqlite
import aiohttp
from dotenv import load_dotenv
import html
import re


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

# Bot-Setup mit erweiterten Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Für AFK Messages
user_data = {}
server_stats = {}

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send('This command is on cooldown, you can use it in {round(error.retry_after, 2)}')

######## Sammel Spiel ########

def calculate_reward(streak: int) -> int:
    return BASE_REWARD + streak * STREAK_BONUS + random.randint(0, 50)
 
async def check_achievements(user_id, coins, streak, db):
    newly_unlocked = []

    # Vorherige Achievements
    async with db.execute("SELECT achievement_id FROM user_achievements WHERE user_id = ?", (user_id,)) as c:
        already = {row[0] async for row in c}

    # Alle Achievements
    async with db.execute("SELECT id, condition, reward_coins FROM achievements") as c:
         all_achs = await c.fetchall()

    for ach_id, condition, reward_bonus in all_achs:
        if ach_id in already:
            continue

        unlock = False
        if condition == "daily_1" and streak >= 1:
            unlock = True
        elif condition == "daily_3" and streak >= 3:
            unlock = True
        elif condition == "daily_7" and streak >= 7:
            unlock = True
        elif condition == "coins_1000" and coins >= 1000:
            unlock = True
        elif condition == "coins_10000" and coins >= 10000:
            unlock = True
        elif condition == "buy_legendary":
            async with db.execute(
                "SELECT COUNT(*) FROM inventory WHERE user_id = ? AND item_rarity = 'legendary'", (user_id,)
            ) as cur:
                count_row = await cur.fetchone()
                if count_row and count_row[0] > 0:
                    unlock = True

        if unlock:
            await db.execute(
                "INSERT INTO user_achievements (user_id, achievement_id) VALUES (?, ?)",
                (user_id, ach_id),
            )
            await db.execute(
                        "UPDATE users SET coins = coins + ? WHERE discord_id = ?",
                        (reward_bonus, user_id),
                    )
            newly_unlocked.append(ach_id)

    await db.commit()
    return newly_unlocked

# --- Command: /daily ---
@bot.tree.command(name="daily", description="Sammle deine tägliche Belohnung ein!")
async def daily(interaction: discord.Interaction):
    user_id = interaction.user.id
    today = date.today()

    async with aiosqlite.connect(DATABASE) as db:
        # --- Tabellen sicherstellen ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER UNIQUE,
                coins INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                streak INTEGER DEFAULT 0,
                last_daily TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                condition TEXT NOT NULL,
                reward_coins INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_achievements (
                user_id INTEGER NOT NULL,
                achievement_id INTEGER NOT NULL,
                achieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, achievement_id)
            )
        """)

        # --- Benutzer laden oder anlegen ---
        async with db.execute("SELECT coins, streak, last_daily FROM users WHERE discord_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()

        if row is None:
            coins, streak, last_daily = 0, 0, None
            await db.execute(
                "INSERT INTO users (discord_id, coins, streak, last_daily) VALUES (?, ?, ?, ?)",
                (user_id, 0, 0, None),
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
            "UPDATE users SET coins = ?, streak = ?, last_daily = ? WHERE discord_id = ?",
            (coins, streak, str(today), user_id),
        )

        # --- Achievement-Check ---
        new_achs = await check_achievements(user_id, coins, streak, db)
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
        async with aiosqlite.connect(DATABASE) as db:
            async with db.execute(
                f"SELECT name FROM achievements WHERE id IN ({','.join('?' * len(new_achs))})", new_achs
            ) as cursor:
                names = await cursor.fetchall()

        names = [n[0] for n in names]
        msg = "🏆 **Neue Achievements freigeschaltet!**\n" + "\n".join(f"✨ {name}" for name in names)
        await interaction.followup.send(msg)



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
        async with db.execute("SELECT coins FROM users WHERE discord_id = ?", (user_id,)) as cursor:
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
        if roll < 0.005:
            multiplier, label, emoji = 50, "💎 JACKPOT!!!", "50-fache"
        elif roll < 0.025:
            multiplier, label, emoji = 10, "🔥 Mega Gewinn!", "10-fache"
        elif roll < 0.125:
            multiplier, label, emoji = 2, "🎉 Gewinn!", "Doppelte"
        elif roll < 0.275:
            multiplier, label, emoji = 1.5, "✨ Kleiner Gewinn!", "1,5-fache"
        else:
            multiplier, label, emoji = 0, "😢 Verloren!", None




        # Münzberechnung in einer Zeile
        winnings = amount * multiplier
        new_coins = max(0, coins - amount + winnings)

        # Direktes Update (schneller als neue Verbindung)
        await db.execute("UPDATE users SET coins = ? WHERE discord_id = ?", (new_coins, user_id))
        await db.commit()

    # Embed schnell rendern
    if multiplier > 0:
        result_msg = f"{label} Du hast das **{emoji}** gewonnen! (+{winnings:,} Münzen)"
        color = discord.Color.green()
    else:
        result_msg = f"{label} Du hast **{amount:,} Münzen** verloren."
        color = discord.Color.red()

    embed = discord.Embed(
        title="🎲 Glücksspiel Ergebnis",
        description=result_msg,
        color=color
    )
    embed.add_field(name="💰 Neuer Kontostand", value=f"**{new_coins:,} Münzen**", inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="autogamble", description="Gamble automatisch, bis du abbrichst oder pleite bist!")
@app_commands.describe(
    amount="Einsatz pro Runde",
    update_intervalls="Update Intervall in Runden",
    max_retries="Maximale Anzahl an Runden (0 für unendlich)"
)
async def autogamble(
    interaction: discord.Interaction,
    amount: int,
    update_intervalls: Optional[int] = 5,
    max_retries: Optional[int] = 0
):
    if amount <= 0:
        await interaction.response.send_message("❌ Bitte setze einen positiven Betrag!", ephemeral=True)
        return

    user_id = interaction.user.id
    await interaction.response.send_message(
        f"🎰 **Autogamble von {interaction.user.display_name}** gestartet!\n"
        f"Einstellungen:\n- Einsatz pro Runde: {amount}\n- Update Intervall: {update_intervalls} Runden\n"
        f"- Max Runden: {'unbegrenzt' if max_retries == 0 else max_retries}\n`stop`, um abzubrechen.",
        ephemeral=False
    )

    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute("SELECT coins FROM users WHERE discord_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()

        if row is None:
            await interaction.followup.send("Du hast noch kein Profil. Nutze `/daily`, um anzufangen!", ephemeral=True)
            return

        coins = row[0]

    rounds = 0
    total_win = 0
    total_loss = 0
    last_rounds = []

    # --- Check-Handler für Stopp-Nachricht ---
    def check(m):
        return m.author.id == user_id and m.content.lower() == "stop" and m.channel == interaction.channel

    stop_gamble = False

    async def stop_listener():
        nonlocal stop_gamble
        try:
            await bot.wait_for("message", check=check)
            stop_gamble = True
        except asyncio.CancelledError:
            pass

    listener_task = asyncio.create_task(stop_listener())

    try:
        while not stop_gamble and (max_retries == 0 or rounds < max_retries):
            rounds += 1
            roll = random.random()

            if coins < amount:
                await interaction.followup.send(
                    f"💸 Du hast nicht mehr genug Münzen! ({coins} übrig)\nRunden gespielt: **{rounds}**"
                )
                break

            if roll < 0.005:  # 0,5% JACKPOT
                multiplier = 50
                winnings = amount * multiplier
                coins = coins - amount + winnings
                total_win += winnings - amount
            elif roll < 0.025:  # 2% x10 Gewinn
                multiplier = 10
                winnings = amount * multiplier
                coins = coins - amount + winnings
                total_win += winnings - amount
            elif roll < 0.125:  # 10% x2 Gewinn
                multiplier = 2
                winnings = amount * multiplier
                coins = coins - amount + winnings
                total_win += winnings - amount
            elif roll < 0.275:  # 15% x1,5 Gewinn
                multiplier = 1.5
                winnings = int(amount * multiplier)
                coins = coins - amount + winnings
                total_win += winnings - amount
            else:  # 72,5% verlieren
                coins -= amount
                total_loss += amount

            last_rounds.append(total_win - total_loss)

            # Update Datenbank
            async with aiosqlite.connect(DATABASE) as db:
                await db.execute("UPDATE users SET coins = ? WHERE discord_id = ?", (coins, user_id))
                await db.commit()

            # Update-Message senden
            if rounds % update_intervalls == 0:
                actual_profit = sum(last_rounds)
                last_rounds = []  # Reset für nächste Intervalle
                await interaction.followup.send(
                    f"Autogamble von **{interaction.user.display_name}**\n🎲 Runde {rounds}: "
                    f"💰 Gewinn: {actual_profit:+} Münzen\n💰 Kontostand: {coins}",
                    ephemeral=False
                )

            await asyncio.sleep(0.8)

        await interaction.followup.send(
            f"🛑 **Autogamble von {interaction.user.display_name} gestoppt!**\n"
            f"Gesamt-Runden: {rounds}\n💰 Endstand: {coins}\n📈 Gesamtgewinn: {total_win - total_loss:+}",
            ephemeral=False
        )

    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}")
    finally:
        listener_task.cancel()



# --- Command: /profile ---
@bot.tree.command(name="profile", description="Zeige dein Spielerprofil an")
async def profile(interaction: discord.Interaction):
    user_id = interaction.user.id

    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT coins, level, streak, last_daily FROM users WHERE discord_id = ?", (user_id,)
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

# --- Command: /shop ---
@bot.tree.command(name="shop", description="Zeigt die verfügbaren Shop-Items an.")
async def shop(interaction: discord.Interaction):
    async with aiosqlite.connect(DATABASE) as db:  # Changed from "database.db" to DATABASE
        # Create shop_items table if it doesn't exist
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shop_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                price INTEGER NOT NULL,
                rarity TEXT DEFAULT 'common'
            )
        """)
        await db.commit()
        
        async with db.execute(
            "SELECT name, description, price, rarity FROM shop_items ORDER BY RANDOM() LIMIT 5;"
        ) as cursor:
            items = await cursor.fetchall()

    if not items:
        await interaction.response.send_message("🛒 Der Shop ist derzeit leer.", ephemeral=True)
        return

    # Embed erstellen
    embed = discord.Embed(
        title="🛒 Täglicher Shop",
        description="Hier sind die heutigen Items! Kaufe sie mit `/buy <item>`",
        color=discord.Color.blurple()
    )

    for name, description, price, rarity in items:
        embed.add_field(
            name=f"**{name}** – {rarity.capitalize()}",
            value=f"{description}\n💰 **Preis:** {price}",
            inline=False
        )

    # Farbe nach der höchsten Rarity
    rarity_order = ["common", "uncommon", "rare", "epic", "legendary"]
    highest_rarity = sorted(items, key=lambda i: rarity_order.index(i[3]))[-1][3]
    embed.color = RARITY_COLORS.get(highest_rarity, discord.Color.blurple())

    await interaction.response.send_message(embed=embed)

# --- Command: /achievements ---
@bot.tree.command(name="achievements", description="Zeige deine freigeschalteten Achievements an")
async def achievements(interaction: discord.Interaction):
    user_id = interaction.user.id
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT discord_id FROM users WHERE discord_id = ?", (user_id,)
        ) as cur:
            user = await cur.fetchone()
        if not user:
            await interaction.response.send_message("Du hast noch kein Profil. Nutze `/daily` zuerst.", ephemeral=True)
            return

        user_db_id = user[0]
        print( "USER:" )
        print( user )
        print( "USER DB ID:" )
        print( user_db_id )
        async with db.execute(
            "SELECT a.name, a.description, ua.achieved_at FROM achievements a "
            "JOIN user_achievements ua ON a.id = ua.achievement_id "
            "WHERE ua.user_id = ?", (user_db_id,)
        ) as cur:
            achievements = await cur.fetchall()
            print( "ACHIEVEMENTS:" )
            print( achievements )

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

# --- Command: /buy <item> ---
@bot.tree.command(name="buy", description="Kaufe ein Item aus dem Shop")
@app_commands.describe(item="Name des Items, das du kaufen willst")
async def buy(interaction: discord.Interaction, item: str):
    user_id = interaction.user.id
    item = item.capitalize()
    async with aiosqlite.connect(DATABASE) as db:
        # Hole Item aus Shop
        async with db.execute("SELECT id, price, rarity FROM shop_items WHERE LOWER(name) = LOWER(?)", (item,)) as cur:
            item_row = await cur.fetchone()

        if not item_row:
            await interaction.response.send_message("❌ Dieses Item existiert nicht im Shop.", ephemeral=True)
            return

        item_id, price, rarity = item_row

        # Hole Userdaten
        async with db.execute("SELECT id, coins FROM users WHERE discord_id = ?", (user_id,)) as cur:
            user_row = await cur.fetchone()

        if not user_row:
            await interaction.response.send_message("Du musst zuerst `/daily` nutzen, um ein Profil zu erstellen.", ephemeral=True)
            return

        user_db_id, coins = user_row

        if coins < price:
            await interaction.response.send_message("💸 Du hast nicht genug Münzen!", ephemeral=True)
            return

        # Münzen abziehen
        new_coins = coins - price
        await db.execute("UPDATE users SET coins = ? WHERE id = ?", (new_coins, user_db_id))

        # Item ins Inventar packen
        async with db.execute("SELECT quantity FROM inventory WHERE user_id = ? AND item_name = ?", (user_db_id, item)) as cur:
            inv_row = await cur.fetchone()

        if inv_row:
            new_quantity = inv_row[0] + 1
            await db.execute("UPDATE inventory SET quantity = ? WHERE user_id = ? AND item_name = ?", (new_quantity, user_db_id, item))
        else:
            await db.execute("INSERT INTO inventory (user_id, item_name, quantity, item_rarity) VALUES (?, ?, ?, ?)", (user_db_id, item, 1, rarity))

        await db.commit()

    await interaction.response.send_message(f"✅ Du hast **{item}** für **{price} 💰** gekauft!")

# --- Command: /sell ---
@bot.tree.command(name="sell", description="Verkaufe ein Item aus deinem Inventar")
@app_commands.describe(item="Name des Items, das du verkaufen willst", quantity="Anzahl der zu verkaufenden Items")
async def sell(interaction: discord.Interaction, item: str, quantity: int = 1):
    user_id = interaction.user.id
    item = item.capitalize()
    if quantity <= 0:
        await interaction.response.send_message("❌ Die Menge muss mindestens 1 sein.", ephemeral=True)
        return

    async with aiosqlite.connect(DATABASE) as db:
        # Hole Userdaten
        async with db.execute("SELECT id FROM users WHERE discord_id = ?", (user_id,)) as cur:
            user_row = await cur.fetchone()

        if not user_row:
            await interaction.response.send_message("Du musst zuerst `/daily` nutzen, um ein Profil zu erstellen.", ephemeral=True)
            return

        user_db_id = user_row[0]

        # Hole Item aus Inventar
        async with db.execute("SELECT quantity FROM inventory WHERE user_id = ? AND item_name = ?", (user_db_id, item)) as cur:
            inv_row = await cur.fetchone()

        if not inv_row or inv_row[0] < quantity:
            await interaction.response.send_message("❌ Du hast nicht genug von diesem Item zum Verkaufen.", ephemeral=True)
            return

        current_quantity = inv_row[0]

        # Hole Itempreis aus Shop
        async with db.execute("SELECT price FROM shop_items WHERE LOWER(name) = LOWER(?)", (item,)) as cur:
            shop_row = await cur.fetchone()

        if not shop_row:
            await interaction.response.send_message("❌ Dieses Item kann nicht verkauft werden.", ephemeral=True)
            return

        price = shop_row[0]
        sell_price = price  # Verkaufspreis ist der Kaufpreis (Anpassbar)
        total_earnings = sell_price * quantity

        # Update Inventar
        new_quantity = current_quantity - quantity
        if new_quantity > 0:
            await db.execute("UPDATE inventory SET quantity = ? WHERE user_id = ? AND item_name = ?", (new_quantity, user_db_id, item))
        else:
            await db.execute("DELETE FROM inventory WHERE user_id = ? AND item_name = ?", (user_db_id, item))

        # Update Münzen
        async with db.execute("SELECT coins FROM users WHERE id = ?", (user_db_id,)) as cur:
            coins_row = await cur.fetchone()
            new_coins = coins_row[0] + total_earnings

        await db.execute("UPDATE users SET coins = ? WHERE id = ?", (new_coins, user_db_id))   
        await db.commit()
    await interaction.response.send_message(f"✅ Du hast **{quantity}x {item}** für **{total_earnings} 💰** verkauft!")

# --- Command: /inventory ---
@bot.tree.command(name="inventory", description="Zeige dein Inventar oder das eines anderen Spielers an")
@app_commands.describe(user="(Optional) Der Benutzer, dessen Inventar du sehen möchtest")
async def inventory(interaction: discord.Interaction, user: discord.User | None = None):
    target = user or interaction.user  
    target_id = target.id
    print( "TARGET ID:" )
    print( target_id )
    async with aiosqlite.connect(DATABASE) as db:
        # Nutzer-ID in DB prüfen
        async with db.execute("SELECT id, coins FROM users WHERE discord_id = ?", (target_id,)) as cur:
            user_row = await cur.fetchone()
            print( "USER DB ID:" )
            print( user_row[0] )
        if not user_row:
            if user:
                await interaction.response.send_message(f"{target.display_name} hat noch kein Profil.", ephemeral=True)
            else:
                await interaction.response.send_message("Du hast noch kein Profil. Nutze `/daily` zuerst.", ephemeral=True)
            return

        user_db_id, coins = user_row

        # Inventar abrufen
        async with db.execute(
            "SELECT item_name, quantity, item_rarity FROM inventory WHERE user_id = ?", (user_db_id,)
        ) as cur:
            items = await cur.fetchall()

    embed = discord.Embed(title=f"🎒 Inventar von {target.display_name}", color=discord.Color.purple())
    embed.add_field(name="💰 Münzen", value=f"{coins} 💰", inline=False)

    # Wenn Inventar leer
    if not items:
        embed.description = "Das Inventar ist leer."
        await interaction.response.send_message(embed=embed)
        return

    # Items nach Seltenheit sortieren
    rarity_order = {"legendary": 4, "epic": 3, "rare": 2, "uncommon": 1, "common": 0}
    items.sort(key=lambda x: rarity_order.get(x[2], 0), reverse=True)

    # Raritätsfarben-Block (visuell hübsch)
    def rarity_block(rarity: str) -> str:
        match rarity:
            case "common": return "```md\n> Common\n```"
            case "uncommon": return "```yaml\nUncommon\n```"
            case "rare": return "```fix\nRare\n```"
            case "epic": return "```asciidoc\n.Epic\n```"
            case "legendary": return "```ml\nLegendary\n```"
            case _: return rarity

    for name, qty, rarity in items:
        embed.add_field(
            name=name,
            value=f"Menge: {qty} {rarity_block(rarity)}",
            inline=True
        )

    # Gesamtwert berechnen (eine DB-Verbindung reicht)
    total_value = 0
    async with aiosqlite.connect(DATABASE) as db:
        for name, qty, _ in items:
            async with db.execute("SELECT price FROM shop_items WHERE LOWER(name) = LOWER(?)", (name,)) as cur:
                row = await cur.fetchone()
                if row:
                    total_value += row[0] * qty

    embed.add_field(name="💎 Gesamtwert des Inventars", value=f"{total_value} 💰", inline=False)

    await interaction.response.send_message(embed=embed)


# --- Command: /leaderboard ---
@bot.tree.command(name="leaderboard", description="Zeige die Top-Spieler")
async def leaderboard(interaction: discord.Interaction):
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT discord_id, coins FROM users ORDER BY coins DESC LIMIT 10"
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



# --- Bot-Events ---
@bot.event
async def on_ready():
    print(f"✨ Eingeloggt als {bot.user}")
    print(f"🆔 Bot ID: {bot.user.id}")
    print(f"📊 Auf {len(bot.guilds)} Servern aktiv")
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} Slash-Commands synchronisiert.")
    except Exception as e:
        print(f"❌ Fehler beim Synchronisieren: {e}")
    
    # Status-Rotation
    bot.loop.create_task(change_status())

async def change_status():
    statuses = [
        "mit Discord.py 🎮",
        "auf euren Befehlen 👀",
        "/hilfe für Befehle 📋",
        f"auf {len(bot.guilds)} Servern 🌍"
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

# ==================== SPASS & SPIELE ====================
@bot.tree.command(name="brettspiel", description="Wer hat Lust auf Brettspiele? 🎲")
async def brettspiel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎲 Brettspiel-Umfrage",
        description="Wer hat heute Lust auf Brettspiele?",
        color=discord.Color.blue()
    )
    embed.add_field(name="✅", value="Ja, ich bin dabei!", inline=True)
    embed.add_field(name="❌", value="Leider nicht", inline=True)
    embed.add_field(name="🤔", value="Vielleicht später", inline=True)
    
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    await msg.add_reaction("✅")
    await msg.add_reaction("❌")
    await msg.add_reaction("🤔")

@bot.tree.command(name="münze", description="Wirft eine Münze.")
@app_commands.describe(anzahl="Wie oft soll die Münze geworfen werden? (1-10)")
async def muenze(interaction: discord.Interaction, anzahl: int = 1):
    if anzahl < 1 or anzahl > 10:
        await interaction.response.send_message("❌ Bitte zwischen 1 und 10 Würfen wählen!")
        return
    
    ergebnisse = [random.choice(["Kopf", "Zahl"]) for _ in range(anzahl)]
    kopf_count = ergebnisse.count("Kopf")
    zahl_count = ergebnisse.count("Zahl")
    
    if anzahl == 1:
        await interaction.response.send_message(f"🪙 {ergebnisse[0]}!")
    else:
        embed = discord.Embed(title="🪙 Münzwurf-Ergebnis", color=discord.Color.gold())
        embed.add_field(name="Würfe", value=anzahl, inline=True)
        embed.add_field(name="Kopf", value=f"{kopf_count}x", inline=True)
        embed.add_field(name="Zahl", value=f"{zahl_count}x", inline=True)
        embed.description = " | ".join(ergebnisse)
        await interaction.response.send_message(embed=embed)

@bot.tree.command(name="muschel", description="Frage die allwissende Miesmuschel.")
@app_commands.describe(frage="Deine Ja/Nein-Frage")
async def muschel(interaction: discord.Interaction, frage: str = ""):
    antworten = {
        "positiv": ["Ja!", "Auf jeden Fall!", "Absolut!", "Zweifellos!", "Das sehe ich so!", "Die Zeichen stehen gut!"],
        "negativ": ["Nein!", "Niemals!", "Besser nicht!", "Ich bezweifle es stark!", "Vergiss es!"],
        "neutral": ["Vielleicht", "Könnte sein...", "Ungewiss", "Konzentriere dich und frag nochmal", "Darauf kann ich jetzt nicht antworten"]
    }
    
    kategorie = random.choice(["positiv", "negativ", "neutral"])
    antwort = random.choice(antworten[kategorie])
    
    color_map = {"positiv": discord.Color.green(), "negativ": discord.Color.red(), "neutral": discord.Color.gold()}
    
    embed = discord.Embed(
        title="🐚 Die magische Miesmuschel",
        description=f"**Frage:** {frage if frage else 'Keine Frage gestellt'}\n\n**Antwort:** {antwort}",
        color=color_map[kategorie]
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="würfel", description="Würfle Würfel (z.B. 2d20 für 2 zwanzigseitige Würfel).")
@app_commands.describe(anzahl="Anzahl der Würfel (1-20)", seiten="Anzahl der Seiten (2-100)")
async def wuerfel(interaction: discord.Interaction, anzahl: int = 1, seiten: int = 6):
    if anzahl < 1 or anzahl > 20:
        await interaction.response.send_message("❌ Bitte zwischen 1 und 20 Würfel wählen!")
        return
    if seiten < 2 or seiten > 100:
        await interaction.response.send_message("❌ Bitte zwischen 2 und 100 Seiten wählen!")
        return
    
    ergebnisse = [random.randint(1, seiten) for _ in range(anzahl)]
    summe = sum(ergebnisse)
    
    embed = discord.Embed(title=f"🎲 Würfelwurf: {anzahl}W{seiten}", color=discord.Color.purple())
    
    if anzahl == 1:
        embed.description = f"**Ergebnis:** {ergebnisse[0]}"
    else:
        embed.add_field(name="Einzelne Würfe", value=" + ".join(map(str, ergebnisse)), inline=False)
        embed.add_field(name="Summe", value=f"**{summe}**", inline=False)
        embed.add_field(name="Durchschnitt", value=f"{summe/anzahl:.1f}", inline=True)
        embed.add_field(name="Höchster Wurf", value=max(ergebnisse), inline=True)
        embed.add_field(name="Niedrigster Wurf", value=min(ergebnisse), inline=True)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="wähle", description="Lass den Bot für dich entscheiden!")
@app_commands.describe(optionen="Optionen durch Kommas getrennt")
async def waehle(interaction: discord.Interaction, optionen: str):
    choices = [x.strip() for x in optionen.split(",") if x.strip()]
    if len(choices) < 2:
        await interaction.response.send_message("❌ Gib mindestens 2 Optionen an!\nBeispiel: `/wähle Pizza, Pasta, Burger`")
        return
    
    await interaction.response.send_message("🤔 Lass mich nachdenken...")
    await asyncio.sleep(1)
    
    wahl = random.choice(choices)
    embed = discord.Embed(
        title="✨ Meine Wahl!",
        description=f"Ich wähle: **{wahl}**",
        color=discord.Color.gold()
    )
    embed.add_field(name="Optionen", value=", ".join(choices), inline=False)
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="8ball", description="Stelle dem magischen 8-Ball eine Frage.")
@app_commands.describe(frage="Deine Frage an den 8-Ball")
async def eightball(interaction: discord.Interaction, frage: str):
    responses = [
        "Es ist sicher.", "Ohne Zweifel.", "Ja - definitiv.", "Du kannst dich darauf verlassen.",
        "So wie ich es sehe, ja.", "Sehr wahrscheinlich.", "Die Aussichten sind gut.",
        "Ja.", "Die Zeichen deuten auf ja.", "Antwort unklar, versuche es erneut.",
        "Frag später nochmal.", "Besser jetzt nicht verraten.", "Kann ich jetzt nicht vorhersagen.",
        "Konzentriere dich und frage erneut.", "Verlasse dich nicht darauf.",
        "Meine Antwort ist nein.", "Meine Quellen sagen nein.", "Die Aussichten sind nicht so gut.",
        "Sehr zweifelhaft."
    ]
    
    embed = discord.Embed(title="🎱 Magic 8-Ball", color=discord.Color.dark_purple())
    embed.add_field(name="Frage", value=frage, inline=False)
    embed.add_field(name="Antwort", value=random.choice(responses), inline=False)
    embed.set_footer(text=f"Gefragt von {interaction.user.name}")
    
    await interaction.response.send_message(embed=embed)

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
            async with db.execute("SELECT coins FROM users WHERE discord_id = ?", (user_id,)) as cursor:
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
                
                await db.execute("UPDATE users SET coins = ? WHERE discord_id = ?", (coins, user_id))
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
            async with db.execute("SELECT coins FROM users WHERE discord_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()

            coins = row[0] if row else 0

            if antwort == richtige_antwort:
                coins += 10
                richtiges_user_set.append((user_id, coins))
            else:
                coins -= 5
                falsches_user_set.append((user_id, coins))

            await db.execute("INSERT OR REPLACE INTO users (discord_id, coins) VALUES (?, ?)", (user_id, coins))
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

# ==================== UMFRAGEN & ABSTIMMUNGEN ====================
@bot.tree.command(name="umfrage", description="Erstelle eine Umfrage mit bis zu 10 Optionen.")
@app_commands.describe(frage="Die Umfragefrage", optionen="Optionen durch Kommas getrennt", dauer="Dauer in Minuten (optional, 1-60)")
async def umfrage(interaction: discord.Interaction, frage: str, optionen: str, dauer: Optional[int] = None):
    choices = [x.strip() for x in optionen.split(",") if x.strip()]
    if len(choices) < 2 or len(choices) > 10:
        await interaction.response.send_message("❌ Gib 2-10 Optionen an!")
        return
    
    if dauer and (dauer < 1 or dauer > 60):
        await interaction.response.send_message("❌ Dauer muss zwischen 1-60 Minuten liegen!")
        return
    
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    
    embed = discord.Embed(title="📊 Umfrage", description=f"**{frage}**\n", color=discord.Color.blue())
    
    for i, choice in enumerate(choices):
        embed.add_field(name=f"{emojis[i]} Option {i+1}", value=choice, inline=False)
    
    if dauer:
        embed.set_footer(text=f"⏰ Umfrage läuft für {dauer} Minuten")
    
    embed.timestamp = datetime.now()
    
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    
    for i in range(len(choices)):
        await msg.add_reaction(emojis[i])
    
    if dauer:
        await asyncio.sleep(dauer * 60)
        msg = await interaction.channel.fetch_message(msg.id)
        
        results_embed = discord.Embed(title="📊 Umfrage Ergebnisse", description=f"**{frage}**\n", color=discord.Color.green())
        
        for i, choice in enumerate(choices):
            reaction = discord.utils.get(msg.reactions, emoji=emojis[i])
            count = reaction.count - 1 if reaction else 0
            results_embed.add_field(name=f"{emojis[i]} {choice}", value=f"{count} Stimme(n)", inline=False)
        
        await interaction.followup.send(embed=results_embed)

@bot.tree.command(name="schnellfrage", description="Einfache Ja/Nein-Umfrage")
@app_commands.describe(frage="Die Ja/Nein-Frage")
async def schnellfrage(interaction: discord.Interaction, frage: str):
    embed = discord.Embed(
        title="❓ Schnellabstimmung",
        description=frage,
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    await msg.add_reaction("👍")
    await msg.add_reaction("👎")
    await msg.add_reaction("🤷")

# ==================== UTILITIES ====================
@bot.tree.command(name="timer", description="Stellt einen Timer mit Erinnerung.")
@app_commands.describe(minuten="Minuten (1-120)", nachricht="Optionale Erinnerungsnachricht")
async def timer(interaction: discord.Interaction, minuten: int, nachricht: str = "⏰ Timer abgelaufen!"):
    if minuten < 1 or minuten > 120:
        await interaction.response.send_message("❌ Bitte zwischen 1 und 120 Minuten wählen!")
        return
    
    end_time = datetime.now() + timedelta(minutes=minuten)
    embed = discord.Embed(
        title="⏰ Timer gesetzt",
        description=f"Timer für **{minuten} Minute(n)** gestartet!",
        color=discord.Color.blue()
    )
    embed.add_field(name="Endet um", value=end_time.strftime("%H:%M:%S Uhr"))
    embed.set_footer(text=f"Erinnerung: {nachricht}")
    
    await interaction.response.send_message(embed=embed)
    await asyncio.sleep(minuten * 60)
    
    reminder_embed = discord.Embed(
        title="⏰ Timer abgelaufen!",
        description=f"{interaction.user.mention}\n\n{nachricht}",
        color=discord.Color.red()
    )
    await interaction.followup.send(embed=reminder_embed)

@bot.tree.command(name="erinnerung", description="Setzt eine Erinnerung mit Datum/Zeit.")
@app_commands.describe(minuten="In wie vielen Minuten?", nachricht="Woran möchtest du erinnert werden?")
async def erinnerung(interaction: discord.Interaction, minuten: int, nachricht: str):
    if minuten < 1 or minuten > 1440:
        await interaction.response.send_message("❌ Bitte zwischen 1 Minute und 24 Stunden wählen!")
        return
    
    await interaction.response.send_message(f"✅ Erinnerung gesetzt für **{minuten}** Minute(n)!")
    await asyncio.sleep(minuten * 60)
    
    await interaction.followup.send(f"🔔 {interaction.user.mention} Erinnerung: **{nachricht}**")

@bot.tree.command(name="rechner", description="Führt eine mathematische Berechnung durch.")
@app_commands.describe(ausdruck="Mathematischer Ausdruck (z.B. 5 + 3 * 2)")
async def rechner(interaction: discord.Interaction, ausdruck: str):
    try:
        erlaubte_zeichen = set("0123456789+-*/.()")
        if not all(c in erlaubte_zeichen or c.isspace() for c in ausdruck):
            await interaction.response.send_message("❌ Nur grundlegende Rechenoperationen erlaubt!")
            return
        
        ergebnis = eval(ausdruck)
        embed = discord.Embed(title="🧮 Rechner", color=discord.Color.blue())
        embed.add_field(name="Eingabe", value=f"`{ausdruck}`", inline=False)
        embed.add_field(name="Ergebnis", value=f"**{ergebnis}**", inline=False)
        await interaction.response.send_message(embed=embed)
    except:
        await interaction.response.send_message("❌ Ungültiger mathematischer Ausdruck!")

@bot.tree.command(name="zufallszahl", description="Generiert Zufallszahlen.")
@app_commands.describe(von="Startwert", bis="Endwert", anzahl="Wie viele Zahlen? (1-20)")
async def zufallszahl(interaction: discord.Interaction, von: int = 1, bis: int = 100, anzahl: int = 1):
    if von >= bis:
        await interaction.response.send_message("❌ Startwert muss kleiner als Endwert sein!")
        return
    if anzahl < 1 or anzahl > 20:
        await interaction.response.send_message("❌ Anzahl muss zwischen 1 und 20 liegen!")
        return
    
    zahlen = [random.randint(von, bis) for _ in range(anzahl)]
    
    embed = discord.Embed(title="🎲 Zufallszahlen", color=discord.Color.purple())
    embed.add_field(name="Bereich", value=f"{von} - {bis}", inline=True)
    embed.add_field(name="Anzahl", value=anzahl, inline=True)
    
    if anzahl == 1:
        embed.add_field(name="Ergebnis", value=f"**{zahlen[0]}**", inline=False)
    else:
        embed.add_field(name="Ergebnisse", value=", ".join(map(str, zahlen)), inline=False)
        embed.add_field(name="Summe", value=sum(zahlen), inline=True)
        embed.add_field(name="Durchschnitt", value=f"{sum(zahlen)/anzahl:.2f}", inline=True)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="afk", description="Setzt deinen AFK-Status.")
@app_commands.describe(grund="Warum bist du AFK?")
async def afk(interaction: discord.Interaction, grund: str = "AFK"):
    user_data[interaction.user.id] = {
        "afk": True,
        "grund": grund,
        "seit": datetime.now()
    }
    
    embed = discord.Embed(
        title="💤 AFK gesetzt",
        description=f"Du bist jetzt AFK: **{grund}**",
        color=discord.Color.orange()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ping", description="Zeigt die Bot-Latenz.")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    
    if latency < 100:
        color = discord.Color.green()
        status = "Ausgezeichnet! 🟢"
    elif latency < 200:
        color = discord.Color.gold()
        status = "Gut 🟡"
    else:
        color = discord.Color.red()
        status = "Langsam 🔴"
    
    embed = discord.Embed(title="🏓 Pong!", color=color)
    embed.add_field(name="Latenz", value=f"{latency}ms")
    embed.add_field(name="Status", value=status)
    
    await interaction.response.send_message(embed=embed)

# ==================== SOZIALES ====================
@bot.tree.command(name="kompliment", description="Gibt jemandem ein Kompliment! 💖")
@app_commands.describe(person="Die Person, die ein Kompliment bekommen soll")
async def kompliment(interaction: discord.Interaction, person: Optional[discord.Member] = None):
    komplimente = [
        "ist einfach großartig! ⭐",
        "hat heute richtig gute Laune! 😄",
        "ist ein echter Freund! 🤗",
        "hat ein tolles Lächeln! 😊",
        "ist super talentiert! 🎨",
        "hat einen großartigen Sinn für Humor! 😂"
    ]

    if person is None:
        person = interaction.user

    kompliment = random.choice(komplimente)
    embed = discord.Embed(
        title="💖 Kompliment",
        description=f"{person.mention} {kompliment}",
        color=discord.Color.pink()
    )
    await interaction.response.send_message(embed=embed)


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
