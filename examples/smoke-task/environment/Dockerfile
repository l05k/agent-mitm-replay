FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends tmux \
 && rm -rf /var/lib/apt/lists/*

# Deterministic prompt for the login shell Terminus-2 drives inside tmux.
RUN echo "if [ -f /root/.bashrc ]; then . /root/.bashrc; fi" > /root/.bash_profile \
 && echo "export PS1='smoke:/app# '" >> /root/.bash_profile

WORKDIR /app
