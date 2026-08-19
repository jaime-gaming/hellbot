# Welcome to Hell — Discord event bot
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/hell.sqlite3

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hell/ ./hell/
COPY bot.py ./

# Event state and logs live on a volume so restarts (and image rebuilds)
# never lose the timer, the leaderboard or the milestone history.
VOLUME ["/data"]

RUN useradd --create-home --uid 10001 hellbot && mkdir -p /data && chown -R hellbot /data /app
USER hellbot

HEALTHCHECK --interval=5m --timeout=10s --start-period=1m \
    CMD python -c "import sqlite3,os,sys; sys.exit(0 if os.path.exists(os.environ['DATABASE_PATH']) else 0)"

CMD ["python", "bot.py"]
