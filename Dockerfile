# v4.1.1 FIX: Docker Hub anonymous pull rate-limits Render free builds
# ("Exited with status 1" after ~60s) — AWS ECR public mirror same image
# hai, no rate limit, no auth needed.
FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Long-polling mode by default. If WEBHOOK_URL is set in env, bot.py switches
# to webhook server automatically.
CMD ["python3", "bot.py"]
