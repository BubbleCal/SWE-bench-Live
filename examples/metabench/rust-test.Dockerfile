FROM rust:1.94.0-slim-bookworm

# Dependency-free Rust fixtures exercise the real worker and persistent Cargo cache.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 gcc libc6-dev util-linux \
    && ln -s /usr/bin/python3 /usr/local/bin/python \
    && rm -rf /var/lib/apt/lists/*
