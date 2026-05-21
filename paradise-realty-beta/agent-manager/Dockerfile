# Single image, two entrypoints (matches the repo's plain-Dockerfile style).
#   * Master Service runs the default CMD below (uvicorn).
#   * Worker Job overrides the container command at deploy time:
#       command: python   args: -m worker.run
# Building once and deploying twice keeps the shared `agentmgr` library
# identical across the Master and every worker.
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agentmgr ./agentmgr
COPY master ./master
COPY worker ./worker
ENV PORT=8080
CMD ["uvicorn", "master.main:build_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
