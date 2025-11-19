FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV TELEGRAM_BOT_TOKEN="8371450363:AAF2pZNfzKml-Sxa4QIuyx7XUeDF8mhg-BU"

CMD ["python", "bot.py"]
