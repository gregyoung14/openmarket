FROM rust:1.88-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config libssl-dev ca-certificates sqlite3 \
  && rm -rf /var/lib/apt/lists/*

COPY . .
RUN cargo fetch
