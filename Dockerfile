FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite db lives in a mounted volume at /data
ENV DB_PATH=/data/philly_bot.db

RUN useradd -r -u 1001 phillybot
RUN mkdir /data && chown phillybot:phillybot /data
USER phillybot

CMD ["python", "bot.py"]
