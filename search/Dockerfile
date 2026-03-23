FROM python:3.11-slim

WORKDIR /search

COPY search/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY .env .
COPY search/search.py .

EXPOSE 7000

CMD ["python", "search.py"]
