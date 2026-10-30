#!/usr/bin/env sh
set -e
if [ -z "$ERPNEXT_API_KEY" ] && [ -n "$ETA_KEYS_PATH" ]; then
  i=0
  while [ ! -f "$ETA_KEYS_PATH" ] && [ $i -lt 120 ]; do sleep 2; i=$((i+1)); done
  export ERPNEXT_API_KEY=$(python -c "import json;print(json.load(open('$ETA_KEYS_PATH'))['agent@example.com']['api_key'])")
  export ERPNEXT_API_SECRET=$(python -c "import json;print(json.load(open('$ETA_KEYS_PATH'))['agent@example.com']['api_secret'])")
fi
exec uvicorn eta_agent.api:app --host 0.0.0.0 --port 8000
