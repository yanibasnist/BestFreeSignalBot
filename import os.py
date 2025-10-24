import os
from telegram.ext import Application

# (بخش بارگذاری توکن — جایگزین کنید)
bot_token = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
if not bot_token or bot_token.strip() == "" or bot_token == "8242464667:AAGetBSMJmYBARUDt6QBtm5DTVLi75gpgHsOUR_TOKEN_HERE":
    raise SystemExit(
        "Invalid Telegram token. Set the TELEGRAM_TOKEN environment variable to your bot token "
        "(do NOT leave '8242464667:AAGetBSMJmYBARUDt6QBtm5DTVLi75gpgHsOUR_TOKEN_HERE'). If token was exposed, regenerate it in BotFather."
    )

# (اطمینان از اینکه Application از bot_token استفاده می‌کند)
application = Application.builder().token(bot_token).build()

# ...existing code...