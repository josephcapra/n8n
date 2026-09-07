FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir google-cloud-storage requests
COPY refresh.py build_dataset.py build_finder.py build_incentives_page.py incentives.py make_llms.py LLMs.current.txt ./
CMD ["python", "refresh.py"]
