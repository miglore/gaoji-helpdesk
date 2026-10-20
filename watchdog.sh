#!/usr/bin/env bash
# 看门狗：运行窗口内发现服务没在跑就拉起来。
#
# 为什么需要它：
#   systemd 的 Restart=always 只在进程“异常退出”时生效。
#   一旦有人执行 `systemctl --user stop gaoji-helpdesk`（显式停止），
#   systemd 就认为这是用户意图，不会自动拉起 —— 服务会静默停摆、
#   在服务时段内无声漏单。本脚本补上这一层。
#
# 用法（cron，每 5 分钟一次）：
#   */5 * * * * /home/ubuntu/gaoji-helpdesk/watchdog.sh >> /home/ubuntu/gaoji-helpdesk/watchdog.log 2>&1
#
# 如果配了 MAILTO 或需要通知，可把 $MSG 接到自己的推送命令。

set -u

SERVICE="gaoji-helpdesk.service"
SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${GAOJI_LOG_FILE:-$SELF_DIR/accept.log}"

# 服务时段（与 gaoji_accept.py 的 GAOJI_WINDOW_* 保持一致）
WIN_START="${GAOJI_WINDOW_START:-07:30}"
WIN_END="${GAOJI_WINDOW_END:-22:00}"
S="${WIN_START/:/}"; E="${WIN_END/:/}"; NOW="$(date +%H%M)"

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

# 不在窗口内 → 什么都不做
if [ "$NOW" -lt "$S" ] || [ "$NOW" -gt "$E" ]; then
  exit 0
fi

STATE="$(systemctl --user is-active "$SERVICE" 2>/dev/null)"
if [ "$STATE" = "active" ]; then
  exit 0
fi

echo "[$(stamp)] ⚠️ 服务状态=$STATE，正在拉起…" >> "$LOG_FILE"
systemctl --user start "$SERVICE"
sleep 3
NEW="$(systemctl --user is-active "$SERVICE" 2>/dev/null)"
echo "[$(stamp)] $( [ "$NEW" = active ] && echo '✅ 已恢复' || echo "❌ 拉起失败（$NEW），需人工介入" )" >> "$LOG_FILE"
