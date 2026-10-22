# Trajectree

Self-hosted project tracker (Python backend, React frontend).

## Run with Docker

```bash
# Build and start (data persists in ./data; UID/GID are not exported by bash
# by default, so `export UID GID=$(id -g)` first or put them in a .env file)
docker compose up -d --build

# Login URL (open it in a browser; replace 0.0.0.0 with 127.0.0.1)
docker compose logs trajectree | grep ready

# CLI token setup
export TRAJECTREE_TOKEN=$(cat data/token)
```

The container runs as a non-root user with a read-only root filesystem, and
publishes port 8742 on 127.0.0.1 only.
