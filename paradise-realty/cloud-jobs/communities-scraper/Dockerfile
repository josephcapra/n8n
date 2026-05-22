FROM mcr.microsoft.com/playwright/python:v1.52.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright chromium is pre-installed in the base image
RUN playwright install chromium

COPY update_communities_all.py agentmgr_worker.py ./

CMD ["python3", "-u", "update_communities_all.py"]
