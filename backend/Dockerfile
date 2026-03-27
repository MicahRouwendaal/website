FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY backend/app.py /app/app.py
COPY generated-site.html /app/generated-site.html
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-2}"]
