# Kadas Monitor

Lightweight thermal + health dashboard for Khadas systems.

## Run with Docker (recommended)

```bash
cd /home/khadas/workspace/kadas-monitor
docker compose up -d --build
```

Open:

```text
http://<your-khadas-ip>:8088
```

`docker-compose.yml` uses `restart: unless-stopped`, so it comes back after reboot (as long as Docker starts on boot).

## Useful commands

```bash
cd /home/khadas/workspace/kadas-monitor
docker compose ps
docker compose logs -f
docker compose down
```

## Local non-container mode

```bash
cd /home/khadas/workspace/kadas-monitor
./start.sh 8088
./stop.sh
```
