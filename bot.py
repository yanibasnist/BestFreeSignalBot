from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
import logging

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Define a command handler. Here, we just send back the same message.
async def echo(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text(update.message.text)

# Define the main function to start the bot
def main() -> None:
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("YOUR_TOKEN_HERE").build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", echo))
    application.add_handler(CommandHandler("help", echo))

    # on non command i.e message - echo the message on Telegram
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Run the bot until the user presses Ctrl-C
    application.run_polling()

if __name__ == "__main__":
    main()
