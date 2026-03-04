import os
import secrets
import subprocess

REPO_URL = os.getenv("REPO_URL", "https://github.com/mikewong23571/colab-imagegen.git")
REPO_REF = os.getenv("REPO_REF", "main")
REPO_DIR = os.getenv("REPO_DIR", "/content/colab-imagegen")
LOG_PATH = os.getenv("BOOTSTRAP_LOG", "/tmp/colab-imagegen-bootstrap.log")
API_BEARER_TOKEN = os.getenv("API_BEARER_TOKEN", secrets.token_urlsafe(24))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/content/.cache/colab-imagegen/outputs")
CORS_ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*")
CORS_ALLOW_CREDENTIALS = os.getenv("CORS_ALLOW_CREDENTIALS", "false")

cmd = f"""
set -euo pipefail
if [ ! -d '{REPO_DIR}/.git' ]; then
  git clone '{REPO_URL}' '{REPO_DIR}'
fi
cd '{REPO_DIR}'
git fetch --all --tags
git checkout '{REPO_REF}'
if git show-ref --verify --quiet 'refs/remotes/origin/{REPO_REF}'; then
  git reset --hard 'origin/{REPO_REF}'
fi

export API_BEARER_TOKEN='{API_BEARER_TOKEN}'
export OUTPUT_DIR='{OUTPUT_DIR}'
export CORS_ALLOW_ORIGINS='{CORS_ALLOW_ORIGINS}'
export CORS_ALLOW_CREDENTIALS='{CORS_ALLOW_CREDENTIALS}'

bash scripts/install_runtime.sh
bash scripts/start_service.sh
"""

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
with open(LOG_PATH, "a", encoding="utf-8") as fp:
    fp.write("\\n===== remote bootstrap launch =====\\n")
    fp.write(f"repo={REPO_URL}\\nref={REPO_REF}\\ndir={REPO_DIR}\\n")
    fp.write(f"token={API_BEARER_TOKEN}\\n")
    fp.write(f"output_dir={OUTPUT_DIR}\\n")
    fp.flush()
    proc = subprocess.Popen(
        ["bash", "-lc", cmd],
        stdout=fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

print(f"Started bootstrap in background. pid={proc.pid} log={LOG_PATH}")
print(f"API_BEARER_TOKEN={API_BEARER_TOKEN}")
