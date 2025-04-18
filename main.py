# main.py
import logging
import asyncio
import discord
from discord.ext import commands
import config
from datetime import datetime
import os

os.makedirs("logs", exist_ok=True)
log_filename = datetime.now().strftime("logs/role_sync_%Y%m%d_%H%M%S.log")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.logger = logging.getLogger("bot")

@bot.event
async def on_ready():
    bot.logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    bot.logger.info(f"Registered commands: {[c.name for c in bot.commands]}")

async def main():
    await bot.load_extension("cogs.role_scheduler")
    await bot.start(config.DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
