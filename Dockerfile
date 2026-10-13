FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Long-polling mode by default. If WEBHOOK_URL is set in env, bot.py switches
# to webhook server automatically.
CMD ["python3", "bot.py"]
