#!/bin/bash
echo "===== Pulling latest changes ====="
git pull origin main

echo "===== Activating virtual environment ====="
source ../venv/bin/activate

echo "===== Installing dependencies ====="
pip install -r requirements.txt --quiet

echo "===== Applying migrations ====="
python manage.py migrate --noinput

echo "===== Restarting passenger ====="
touch ../tmp/restart.txt

echo "===== DONE ====="
