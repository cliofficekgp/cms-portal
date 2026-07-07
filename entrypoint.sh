#!/bin/bash
set -e

chown -R appuser:appuser /app/data

exec gosu appuser gunicorn --config gunicorn.conf.py webapp.wsgi:app