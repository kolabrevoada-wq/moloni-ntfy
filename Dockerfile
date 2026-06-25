FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY moloni_ntfy.py .

# State (tokens + last seen document) lives on a mounted volume.
ENV STATE_FILE=/data/state.json
VOLUME ["/data"]

ENTRYPOINT ["python", "moloni_ntfy.py"]
CMD ["run"]
