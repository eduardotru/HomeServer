FROM python:3.11-slim

WORKDIR /files

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY files.py .

# /data/files is mounted from the host at runtime
VOLUME ["/data/files"]

EXPOSE 9000

CMD ["python", "files.py"]
