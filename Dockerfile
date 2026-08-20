# Welcome to Hell — Discord event bot
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/hell.sqlite3

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Everything the bot loads at runtime. Announcements.py is required — the bot
# reads all of its message text from it and refuses to start without it.
COPY hell/ ./hell/
COPY tools/healthcheck.py ./tools/
COPY Announcements.py bot.py ./

# Event state and logs live on a volume so restarts (and image rebuilds)
# never lose the timer, the leaderboard or the milestone history.
VOLUME ["/data"]

RUN useradd --create-home --uid 10001 hellbot && mkdir -p /data && chown -R hellbot /data /app
USER hellbot

# Unhealthy when an event is RUNNING but the VC has not been observed recently:
# a stalled monitor is worse than a crash, because nobody is watching the channel.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "tools/healthcheck.py", "--max-lag", "120"]

CMD ["python", "bot.py"]
