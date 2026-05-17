FROM python:3.12-slim

# Install restic, rsync, and docker CLI
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        restic rsync curl openssh-client \
        postgresql-client \
        default-mysql-client && \
    rm -rf /var/lib/apt/lists/* && \
    curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-27.5.1.tgz | \
    tar xz --strip-components=1 -C /usr/local/bin docker/docker && \
    chmod +x /usr/local/bin/docker

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
