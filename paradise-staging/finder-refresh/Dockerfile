FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir google-cloud-storage requests
COPY refresh.py build_dataset.py build_finder.py ./
CMD ["python", "refresh.py"]
