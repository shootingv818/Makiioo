# Worker image for V_2rubby.
# The master clones the repo onto a fresh server and builds this image, then
# runs it as MODE=worker (a headless API node). The SAME image can run the
# master too, but normally the master runs directly with python.
FROM python:3.11-slim

# System deps are only a fallback: cryptography/asyncssh ship prebuilt wheels
# for slim images, so a flaky apt mirror must NOT break the build. We therefore
# make this step best-effort (|| true) and never fail the image on it.
RUN (apt-get update \
    && apt-get install -y --no-install-recommends gcc libffi-dev curl \
    && rm -rf /var/lib/apt/lists/*) || true

WORKDIR /app

# Install Python deps first (better layer caching).
COPY requirements.txt /app/requirements.txt
# Upgrade pip so it always prefers manylinux wheels (no compiler needed), and
# give pip extra retries / a longer timeout for slow or flaky networks.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --retries 10 --timeout 120 \
       -i https://pypi.org/simple -r requirements.txt

# App source.
COPY . /app

# Sessions / db live under /app/data, which is mounted as a host volume so they
# survive container restarts and updates.
#
# The sub-package refactor moved those paths without the volume following:
# db.py now resolves its database to /app/core/data/data.db and
# rubika_client.py its sessions to /app/rubika/data/sessions, while the
# container is still started with `-v <host>:/app/data`. Both therefore lived
# INSIDE the container and every update (which recreates it) wiped the worker's
# sessions — after which /account/verify could not open them, returned "not
# dead" for a session it simply could not find, and the master reported the
# account as healthy.
#
# Symlinking both back onto /app/data restores the pre-refactor layout inside
# the mounted volume, without editing db.py or rubika_client.py (pinned by
# project_notes/CORE_RULES.md).
RUN mkdir -p /app/data \
    && rm -rf /app/core/data /app/rubika/data \
    && ln -sfn /app/data /app/core/data \
    && ln -sfn /app/data /app/rubika/data
ENV MODE=worker
# Unbuffered stdout/stderr so `docker logs` shows output immediately.
ENV PYTHONUNBUFFERED=1

# The worker API listens on this port (loopback-published by the master).
EXPOSE 8765

CMD ["python", "main.py"]
