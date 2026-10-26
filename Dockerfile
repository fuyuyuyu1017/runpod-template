# RunPod用の軽いベースイメージ。
# ComfyUI本体・仮想環境・モデルはNetwork Volume（/workspace）に置き、ここには入れない。
FROM nvidia/cuda:12.8.1-base-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Asia/Tokyo

# Volume上の仮想環境は /usr/bin/python3.12 を参照するので、Python 3.12 は必ずこの場所に置く
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3.12-dev \
        build-essential git git-lfs curl wget ca-certificates \
        ffmpeg sox libsox-fmt-all libgl1 libglib2.0-0 \
        openssh-server tmux htop nano less rsync unzip tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd

# JupyterLab（Volume上の環境とは別にイメージ内に持つ）
RUN python3.12 -m venv /opt/jupyter \
    && /opt/jupyter/bin/pip install "jupyterlab>=4,<5"

# ファイルブラウザ
RUN curl -fsSL https://raw.githubusercontent.com/filebrowser/get/master/get.sh | bash

COPY start.sh /start.sh
COPY scripts/ /opt/runpod-template/scripts/
RUN chmod +x /start.sh /opt/runpod-template/scripts/*.sh

WORKDIR /workspace
CMD ["/start.sh"]
