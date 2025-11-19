from bot import SnapchatBot
import os

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
bot = SnapchatBot(BOT_TOKEN)

app = bot.application

if __name__ == '__main__':
    bot.run_webhook()
