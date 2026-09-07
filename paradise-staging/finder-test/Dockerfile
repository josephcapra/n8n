FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir flask gunicorn

COPY index.html .
COPY incentives.html .
COPY llms.txt .
COPY main.py .
COPY communities_all.js.gz .

ENV PORT=8080

CMD exec gunicorn --bind :$PORT --workers 1 --threads 2 main:app
