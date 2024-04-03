FROM python:3.12-slim

WORKDIR /usr/src/app

ENV TZ=Europe/Berlin

ADD main.py .
ADD requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

CMD python -B -O main.py
