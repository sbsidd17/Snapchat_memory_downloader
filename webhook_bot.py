from flask import Flask, request
from telegram import Update
from bot import SnapchatBot
import os

app = Flask(__name__)
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
bot = SnapchatBot(BOT_TOKEN)

@app.route('/webhook/' + BOT_TOKEN, methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(), bot.application.bot)
    bot.application.process_update(update)
    return 'OK'

@app.route('/')
def index():
    return 'Bot is running!'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
