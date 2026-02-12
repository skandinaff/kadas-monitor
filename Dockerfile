FROM python:3.12-slim

WORKDIR /app
COPY server.py index.html /app/

EXPOSE 8088

CMD ["python3", "/app/server.py", "--host", "0.0.0.0", "--port", "8088"]
