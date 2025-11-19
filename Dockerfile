FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV TELEGRAM_BOT_TOKEN="your_bot_token"

CMD ["python", "bot.py"]
