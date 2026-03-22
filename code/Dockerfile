FROM python:3.11-slim

# Install common dev tools available inside the container
RUN apt-get update && apt-get install -y \
    git curl tree grep findutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY code.py .

# /workspace is mounted from the host at runtime — nothing is baked in
VOLUME ["/workspace"]

EXPOSE 6000

CMD ["python", "code.py"]
