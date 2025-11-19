# Snapchat_memory_downloader
# Snapchat Memories Telegram Bot Setup

## 1. Create a Telegram Bot
1. Message @BotFather on Telegram
2. Send `/newbot`
3. Follow instructions to get your bot token

## 2. Set up Environment
```bash
# Clone or create project directory
mkdir snapchat-memories-bot
cd snapchat-memories-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set bot token
export TELEGRAM_BOT_TOKEN="your_bot_token_here"
# On Windows: set TELEGRAM_BOT_TOKEN=your_bot_token_here
