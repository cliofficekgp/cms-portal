#!/bin/bash
set -e

chown -R appuser:appuser /app/data
chmod 600 /home/appuser/.ssh/cms_tunnel_key 2>/dev/null || true
chown appuser:appuser /home/appuser/.ssh/cms_tunnel_key 2>/dev/null || true

# Start SOCKS5 tunnel in background, auto-retry if it drops
gosu appuser bash -c '
  while true; do
    ssh -D 127.0.0.1:1080 -N \
      -o StrictHostKeyChecking=no \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 \
      -o ExitOnForwardFailure=yes \
      -i /home/appuser/.ssh/cms_tunnel_key \
      cliofficekgp@100.85.52.9
    echo "SSH tunnel dropped, retrying in 5s..."
    sleep 5
  done
' &

# Give the tunnel a moment to establish before the app starts
sleep 3

exec gosu appuser gunicorn --config gunicorn.conf.py webapp.wsgi:app