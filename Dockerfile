FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
COPY diskwatch.py /app/diskwatch.py
EXPOSE 8099
CMD ["python3", "/app/diskwatch.py"]
