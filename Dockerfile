FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wyoming_stt_proxy.py .
COPY rules.yaml .

EXPOSE 10301
CMD ["python", "-u", "wyoming_stt_proxy.py"]
