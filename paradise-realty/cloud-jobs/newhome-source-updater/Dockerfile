FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY newhome_source_updater.py agentmgr_worker.py agentmgr_reports.py ./

CMD ["python3", "-u", "newhome_source_updater.py"]
