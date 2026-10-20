#!/usr/bin/env bash
# KF5 帮助台自动接单一键安装（Linux / systemd --user）
#
#   ./install.sh              # 安装依赖 + 注册服务（不启动）
#   ./install.sh --start      # 安装后立即启动
#
# 幂等：可重复执行用于升级代码。

set -euo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
SERVICE="gaoji-helpdesk.service"
START=0
[ "${1:-}" = "--start" ] && START=1

say() { printf '\033[36m==>\033[0m %s\n' "$1"; }

# ── 1. Python ────────────────────────────────────────────────────────────
PY="$(command -v python3.12 || command -v python3)"
command -v "$PY" >/dev/null || { echo "缺少 python3，请先安装"; exit 1; }
say "Python: $PY ($("$PY" --version 2>&1))"

# ── 2. 虚拟环境 + 依赖 ───────────────────────────────────────────────────
if [ ! -d "$SELF_DIR/venv" ]; then
  say "创建虚拟环境 $SELF_DIR/venv"
  "$PY" -m venv "$SELF_DIR/venv"
fi
"$SELF_DIR/venv/bin/python" -m pip install --quiet --upgrade pip
"$SELF_DIR/venv/bin/python" -m pip install --quiet -r "$SELF_DIR/requirements.txt"
say "依赖安装完成（requests）"

# ── 3. 凭据 ──────────────────────────────────────────────────────────────
if [ ! -f "$SELF_DIR/secrets.env" ]; then
  say "未找到 secrets.env —— 启动配置向导"
  "$SELF_DIR/setup.sh" || { echo "配置未完成，已中止"; exit 1; }
fi
chmod 600 "$SELF_DIR/secrets.env"

# ── 4. systemd 用户服务 ──────────────────────────────────────────────────
# 用真实安装路径替换模板里的 %h/gaoji-helpdesk，
# 这样 clone 到任意目录都能正确工作（写死 $HOME/gaoji-helpdesk 会 203/EXEC）
mkdir -p "$UNIT_DIR"
sed -e "s#%h/gaoji-helpdesk#$SELF_DIR#g" -e "s#%h#$HOME#g" \
    "$SELF_DIR/gaoji-helpdesk.service" > "$UNIT_DIR/$SERVICE"
systemctl --user daemon-reload
say "已安装 $UNIT_DIR/$SERVICE（指向 $SELF_DIR）"

# ── 5. 管理命令 gaoji ────────────────────────────────────────────────────
mkdir -p "$HOME/.local/bin"
ln -sf "$SELF_DIR/gaoji" "$HOME/.local/bin/gaoji"
say "管理命令: gaoji status | check | logs | tail"

# ── 6. 启动 ──────────────────────────────────────────────────────────────
if [ "$START" = 1 ]; then
  systemctl --user enable --now "$SERVICE"
  sleep 3
  "$HOME/.local/bin/gaoji" check || true
else
  echo
  echo "下一步:"
  echo "  1) 编辑 $SELF_DIR/secrets.env 填入账号密码"
  echo "  2) gaoji start      # 启动并设开机自启"
  echo "  3) gaoji check      # 体检（确认真在轮询）"
  echo
  echo "如需 SSH 断开后仍常驻（强烈建议）: sudo loginctl enable-linger $USER"
fi
