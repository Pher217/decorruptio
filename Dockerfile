FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .
COPY . .
# Development image for the DRF review API / Django admin. Scheduling is cron +
# management commands, so nothing long-running is started here beyond the server.
EXPOSE 8000
ENV DJANGO_SETTINGS_MODULE=config.settings.dev
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
