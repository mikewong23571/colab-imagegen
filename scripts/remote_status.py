import subprocess

cmd = r'''
set -euo pipefail
echo ---bootstrap-log---
tail -n 120 /tmp/colab-imagegen-bootstrap.log || true
echo ---api-log---
tail -n 120 /tmp/colab-imagegen/api.log || true
echo ---cloudflared-log---
tail -n 120 /tmp/colab-imagegen/cloudflared.log || true
echo ---service-state---
cat /tmp/colab-imagegen/service.env || true
echo ---ps---
ps -ef | grep -E "uvicorn|cloudflared|start_service" | grep -v grep || true
'''

out = subprocess.check_output(["bash", "-lc", cmd], text=True)
print(out)
