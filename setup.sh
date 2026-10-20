#!/usr/bin/env bash
# 交互式配置向导 —— 填好 secrets.env 并验证登录。
#
#   ./setup.sh
#
# 只需要：帮助台地址、服务商 ID、你的账号、你的密码。
# user_id 会在登录后自动探测，不用手查。
#
# 如果 secrets.env 里已有值，会作为默认值直接回车沿用，
# 这样同事只需要输入自己的账号和密码。

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SELF_DIR"
SECRETS="$SELF_DIR/secrets.env"
PY="${PYTHON:-python3}"
FRESH=0

command -v "$PY" >/dev/null 2>&1 || { echo "❌ 找不到 python3，请先安装 Python 3.11+"; exit 1; }

# ── 读取已有配置作为默认值 ────────────────────────────────────────────────
CUR_URL=""; CUR_PID=""; CUR_USER=""; CUR_UID=""
if [ -f "$SECRETS" ]; then
    CUR_URL=$(grep -E '^GAOJI_BASE_URL='  "$SECRETS" | head -1 | cut -d= -f2-)
    CUR_PID=$(grep -E '^GAOJI_PROVIDER_ID=' "$SECRETS" | head -1 | cut -d= -f2-)
    CUR_USER=$(grep -E '^GAOJI_USERNAME='   "$SECRETS" | head -1 | cut -d= -f2-)
    CUR_UID=$(grep -E '^GAOJI_USER_ID='     "$SECRETS" | head -1 | cut -d= -f2-)
else
    FRESH=1
fi

ask() {   # ask <提示> <默认值>
    local prompt="$1" def="${2:-}" ans=""
    if [ -n "$def" ]; then
        read -r -p "$prompt [$def]: " ans
        printf '%s' "${ans:-$def}"
    else
        while [ -z "$ans" ]; do
            read -r -p "$prompt: " ans
            [ -z "$ans" ] && echo "  ⚠️  不能为空" >&2
        done
        printf '%s' "$ans"
    fi
}

echo "════════════════════════════════════════════════"
echo " KF5 帮助台自动接单 —— 配置向导"
echo "════════════════════════════════════════════════"
echo

BASE_URL=$(ask "① 帮助台地址（含 https://）" "$CUR_URL")
PROVIDER_ID=$(ask "② 服务商 provider_id" "$CUR_PID")

if [ -n "$CUR_USER" ]; then
    USERNAME=$(ask "③ 你的登录账号" "$CUR_USER")
else
    USERNAME=$(ask "③ 你的登录账号" "")
fi

printf '④ 你的登录密码（输入时不显示）: '
read -r -s PASSWORD
echo
if [ -z "$PASSWORD" ]; then
    echo "❌ 密码不能为空"
    exit 1
fi

echo
echo "正在验证登录并自动获取 user_id …"

# ── 验证并写入 ────────────────────────────────────────────────────────────
GAOJI_BASE_URL="$BASE_URL" \
GAOJI_PROVIDER_ID="$PROVIDER_ID" \
GAOJI_USERNAME="$USERNAME" \
GAOJI_PASSWORD="$PASSWORD" \
GAOJI_USER_ID="" \
GAOJI_LOG_FILE="/tmp/gaoji_setup_probe.log" \
"$PY" - <<'PYEOF'
import os, sys
sys.path.insert(0, os.getcwd())
try:
    import gaoji_accept as g
    c = g.KF5()
    c.login()
except SystemExit:
    raise
except Exception as exc:
    print(f"❌ 登录失败：{type(exc).__name__}: {exc}")
    print("   请检查地址 / provider_id / 账号 / 密码是否正确。")
    sys.exit(1)

uid = c.detect_user_id()
if uid:
    print(f"✅ 登录成功，自动获取 user_id = {uid}")
    open("/tmp/gaoji_setup_uid", "w").write(str(uid))
else:
    print("✅ 登录成功，但你还没有任何历史工单，无法自动获取 user_id。")
    print("   先处理一张工单后重新运行本向导，或手动填写 GAOJI_USER_ID。")
    open("/tmp/gaoji_setup_uid", "w").write("")
PYEOF

STATUS=$?
rm -f /tmp/gaoji_setup_probe.log
if [ $STATUS -ne 0 ]; then
    rm -f /tmp/gaoji_setup_uid
    echo
    echo "❌ 未写入配置。修正后重新运行 ./setup.sh"
    exit 1
fi

DETECTED_UID=$(cat /tmp/gaoji_setup_uid 2>/dev/null || echo "")
rm -f /tmp/gaoji_setup_uid

# ── 写 secrets.env ────────────────────────────────────────────────────────
umask 077
cat > "$SECRETS" <<EOF
# 由 setup.sh 生成 $(date '+%Y-%m-%d %H:%M:%S')
GAOJI_BASE_URL=$BASE_URL
GAOJI_USERNAME=$USERNAME
GAOJI_PASSWORD=$PASSWORD
GAOJI_PROVIDER_ID=$PROVIDER_ID
GAOJI_USER_ID=${DETECTED_UID:-${CUR_UID:-}}
EOF
chmod 600 "$SECRETS"

echo
echo "✅ 已写入 $SECRETS（权限 600）"
echo
echo "下一步："
if [ "$FRESH" = "1" ]; then
    echo "  ./install.sh --start      # 安装依赖并启动服务"
else
    echo "  gaoji restart             # 重启使新配置生效"
    echo "  gaoji check               # 体检：确认有到帮助台的 ESTAB 连接"
fi
