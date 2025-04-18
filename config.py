# config.py

import os
from datetime import datetime, timezone
from dotenv import load_dotenv
import pytz

load_dotenv()

DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN")
LOA_API_KEY    = os.getenv("LOA_API_KEY")
GUILD_ID       = int(os.getenv("GUILD_ID"))

LOA_API_URL      = "https://developer-lostark.game.onstove.com/armories/characters/{name}/profiles"

LOA_SIBLINGS_URL = (
    "https://developer-lostark.game.onstove.com"
    "/characters/{name}/siblings"
)

JOIN_THRESHOLD = datetime(2024, 1, 1, tzinfo=timezone.utc)
MIN_ITEM_LEVEL = 1720
ROLE_EXCELLENT = "우수회원"

SCHEDULE_CRON = {
    'hour': '0,3,6,9,12,15,18,21',
    'minute': 0,
    'second': 0,
    'timezone': pytz.timezone("Asia/Seoul")
}
