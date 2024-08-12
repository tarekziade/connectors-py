FROM python:3.12-slim-bookworm
# RUN apt update && apt install make
RUN apt update && apt upgrade && apt install make -y
COPY . /app
WORKDIR /app
RUN make clean install
ENTRYPOINT []
