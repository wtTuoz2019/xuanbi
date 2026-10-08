#!/usr/bin/env bash
# 在服务器上执行这一条即可：拉取最新代码、保留客户数据库、重新启动。
# 第一次：
#   git clone git@github.com:wtTuoz2019/xuanbi.git
#   cd xuanbi && bash deploy.sh
# 之后每次更新：
#   bash deploy.sh
# 可选：在项目里放 radar.env，例如 PORT=80
set -euo pipefail

REPO="git@github.com:wtTuoz2019/xuanbi.git"
BRANCH="main"

if [ ! -f "${BASH_SOURCE[0]:-}" ]; then
  ROOT="${XUANBI_DIR:-$HOME/xuanbi}"
  if [ ! -d "$ROOT/.git" ]; then
    git clone "$REPO" "$ROOT"
  fi
  exec bash "$ROOT/deploy.sh"
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [ "${1:-}" != "--deploy" ]; then
  if [ -n "$(git status --porcelain)" ]; then
    echo "工作区有未提交改动，已停止，避免覆盖服务器上的修改。"
    git status --porcelain
    exit 1
  fi
  git fetch origin
  git checkout "$BRANCH"
  git pull --ff-only origin "$BRANCH"
  exec bash "$ROOT/deploy.sh" --deploy
fi

if [ -f "$ROOT/radar.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/radar.env"
  set +a
fi
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8787}"

python3 -m py_compile "$ROOT/server.py"
mkdir -p "$ROOT/data"

if [ "$(id -u)" -eq 0 ] && command -v systemctl >/dev/null 2>&1; then
  python_bin="$(command -v python3)"
  cat > /etc/systemd/system/xuanbi.service <<EOF
[Unit]
Description=Strong Coin Radar
After=network.target

[Service]
WorkingDirectory=$ROOT
ExecStart=$python_bin $ROOT/server.py
Restart=on-failure
RestartSec=2
Environment=HOST=$HOST
Environment=PORT=$PORT
EnvironmentFile=-$ROOT/radar.env

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable xuanbi
  systemctl restart xuanbi
else
  if [ -f "$ROOT/data/server.pid" ]; then
    old="$(cat "$ROOT/data/server.pid" || true)"
    if [ -n "${old}" ] && kill -0 "$old" 2>/dev/null; then
      kill "$old" || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$old" 2>/dev/null || break
        sleep 0.2
      done
      kill -9 "$old" 2>/dev/null || true
    fi
  fi
  nohup python3 "$ROOT/server.py" >> "$ROOT/data/server.log" 2>&1 &
  echo $! > "$ROOT/data/server.pid"
fi

ready=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if python3 - "$PORT" <<'PY'
import sys, urllib.request
urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/health", timeout=2).read()
PY
  then
    ready=1
    break
  fi
  sleep 0.3
done

if [ "$ready" -ne 1 ]; then
  echo "服务没有在端口 $PORT 上起来。可查看 data/server.log"
  exit 1
fi

echo "部署完成。客户数据库仍在 data/radar.sqlite"
echo "本机打开 http://127.0.0.1:$PORT/"
echo "域名解析到这台机器后，打开 http://你的域名:$PORT/"
if [ "$PORT" != "80" ]; then
  echo "想去掉端口号时，在 radar.env 里写 PORT=80，再用 root 执行 bash deploy.sh"
fi
