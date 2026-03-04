import os
import subprocess

REPO_URL = os.getenv("REPO_URL", "https://github.com/mikewong23571/colab-imagegen.git")
REPO_REF = os.getenv("REPO_REF", "8a4dbe031554e4b6cb3cc11377c687999a73e8c3")
REPO_DIR = os.getenv("REPO_DIR", "/content/colab-imagegen")
LOG_PATH = os.getenv("BOOTSTRAP_LOG", "/tmp/colab-imagegen-bootstrap.log")

cmd = f"""
set -euo pipefail
if [ ! -d '{REPO_DIR}/.git' ]; then
  git clone '{REPO_URL}' '{REPO_DIR}'
fi
cd '{REPO_DIR}'
git fetch --all --tags
git checkout '{REPO_REF}'

bash scripts/install_runtime.sh
bash scripts/start_service.sh
"""

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
with open(LOG_PATH, "a", encoding="utf-8") as fp:
    fp.write("\\n===== remote bootstrap launch =====\\n")
    fp.write(f"repo={REPO_URL}\\nref={REPO_REF}\\ndir={REPO_DIR}\\n")
    fp.flush()
    proc = subprocess.Popen(
        ["bash", "-lc", cmd],
        stdout=fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

print(f"Started bootstrap in background. pid={proc.pid} log={LOG_PATH}")
