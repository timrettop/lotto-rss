FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/app.py .

# Data directory for cache and ticket records (mount as volume)
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "app.py"]
