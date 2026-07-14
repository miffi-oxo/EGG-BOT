import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import asyncio
import logging

from core.config import BOT_TOKEN
from core.bot_core import create_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

print("🔥 RUNNING FILE:", __file__)


async def main():
    bot = create_bot()
    print("[BOOT] bot start")
    await bot.start(BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())