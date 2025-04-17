import logging
import asyncio

import discord
from discord.ext import commands

import config

# ── 로깅 세팅 ──
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s'
)

# ── Intents 설정 ──
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

# ── Bot 생성 ──
bot = commands.Bot(command_prefix="!", intents=intents)
bot.logger = logging.getLogger("bot")

@bot.event
async def on_ready():
    bot.logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    bot.logger.info(f"Registered commands: {[c.name for c in bot.commands]}")

# ── Async main 함수: extension 로드 & 봇 시작 ──
async def main():
    await bot.load_extension("cogs.role_scheduler")
    await bot.start(config.DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
