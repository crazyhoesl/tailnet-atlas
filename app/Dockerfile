FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py template.html ./
# Binds 127.0.0.1 only, inside the Tailscale sidecar's network namespace.
# Nothing is exposed except through `tailscale serve` (no Funnel).
CMD ["python", "main.py"]
