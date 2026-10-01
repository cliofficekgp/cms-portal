#!/bin/bash
set -e

chown -R appuser:appuser /app/data
chmod 600 /home/appuser/.ssh/cms_tunnel_key 2>/dev/null || true
chown appuser:appuser /home/appuser/.ssh/cms_tunnel_key 2>/dev/null || true

# ── Primary SOCKS5 tunnel → office PC (100.124.187.97) on port 1080 ────────
gosu appuser bash -c '
  while true; do
    ssh -D 127.0.0.1:1080 -N \
      -o StrictHostKeyChecking=no \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 \
      -o ExitOnForwardFailure=yes \
      -i /home/appuser/.ssh/cms_tunnel_key \
      kgplo@100.124.187.97
    echo "[Tunnel-1080] Office PC SSH tunnel dropped, retrying in 5s..."
    sleep 5
  done
' &

# ── Fallback SOCKS5 tunnel → laptop (100.85.52.9) on port 1081 ─────────────
gosu appuser bash -c '
  while true; do
    ssh -D 127.0.0.1:1081 -N \
      -o StrictHostKeyChecking=no \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 \
      -o ExitOnForwardFailure=yes \
      -i /home/appuser/.ssh/cms_tunnel_key \
      hp@100.85.52.9
    echo "[Tunnel-1081] Laptop SSH tunnel dropped, retrying in 5s..."
    sleep 5
  done
' &

# ── Fallback SOCKS5 tunnel → office new computer (100.110.163.89) on port 1082 ─────────────
gosu appuser bash -c '
  while true; do
    ssh -D 127.0.0.1:1082 -N \
      -o StrictHostKeyChecking=no \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 \
      -o ExitOnForwardFailure=yes \
      -i /home/appuser/.ssh/cms_tunnel_key \
      admin@100.110.163.89
    echo "[Tunnel-1082] office new computer SSH tunnel dropped, retrying in 5s..."
    sleep 5
  done
' &

# ── Fallback SOCKS5 tunnel → office computer in TLC (100.114.255.7) on port 1082 ─────────────
gosu appuser bash -c '
  while true; do
    ssh -D 127.0.0.1:1083 -N \
      -o StrictHostKeyChecking=no \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 \
      -o ExitOnForwardFailure=yes \
      -i /home/appuser/.ssh/cms_tunnel_key \
      hp@100.114.255.7
    echo "[Tunnel-1083] TLC computer SSH tunnel dropped, retrying in 5s..."
    sleep 5
  done
' &

# ── Fallback SOCKS5 tunnel → CLI HQ Main (100.127.133.72) on port 1084 ─────────────
gosu appuser bash -c '
  while true; do
    ssh -D 127.0.0.1:1084 -N \
      -o StrictHostKeyChecking=no \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 \
      -o ExitOnForwardFailure=yes \
      -i /home/appuser/.ssh/cms_tunnel_key \
      cliof@100.127.133.72
    echo "[Tunnel-1084] CLI HQ Main SSH tunnel dropped, retrying in 5s..."
    sleep 5
  done
' &

# Wait until the SOCKS5 tunnel is actually listening on port 1080 (up to 60s)
echo "Waiting for SOCKS5 tunnel on port 1080..."
for i in $(seq 1 30); do
  if nc -z 127.0.0.1 1080 2>/dev/null; then
    echo "Tunnel ready after ${i}s."
    break
  fi
  sleep 2
done

if ! nc -z 127.0.0.1 1080 2>/dev/null; then
  echo "WARNING: SOCKS5 tunnel not ready after 60s — starting app anyway."
fi

exec gosu appuser gunicorn --config gunicorn.conf.py webapp.wsgi:app