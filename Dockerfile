FROM python:3.13-slim

WORKDIR /app
COPY requirements.lock .
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY app.py .env.example ./
COPY omni_research ./omni_research

EXPOSE 5000
CMD ["python", "app.py"]
