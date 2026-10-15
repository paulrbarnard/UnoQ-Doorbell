# HomeKit companion for UnoQ-Doorbell. Built on the board:
#   cd ~/ArduinoApps/unoq-doorbell/homekit && docker compose up -d --build
FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/hap
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hap_doorbell.py .
CMD ["python", "-u", "hap_doorbell.py"]
