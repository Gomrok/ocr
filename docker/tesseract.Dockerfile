FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
COPY tessdata/fas.traineddata /usr/share/tesseract-ocr/5/tessdata/fas.traineddata
