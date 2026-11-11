FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends gdal-bin libgdal-dev && rm -rf /var/lib/apt/lists/*
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
CMD ["sh","-c","exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
