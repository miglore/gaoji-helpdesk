# 实现思路与 API 逆向记录

本文记录 `gaoji-helpdesk` 的实现决策、KF5 接口逆向过程，以及所有踩过的坑。
排错时先读「陷阱清单」一节。

---

## 1. 为什么是"纯 API"而不是浏览器自动化

### 原始问题

帮助台 KF5 是一个重型 Angular SPA：

- 首屏 `div.pre-loading` 遮罩经常 10–60 秒不消失
- 关键元素靠 `li.ng-scope.cursorhand` 这类**编译产物类名**定位，随版本变化
- 一次"打开工单 → 输入 → 提交"的操作占用数十秒

**在抢单场景下，慢就等于漏单。** 浏览器方案无论怎么优化（Cookie 注入、双线程、队列、重试）都无法绕开 SPA 本身的加载成本。

### 转折：从"猜接口"到"抓接口"

早期直接对着 REST 风格猜端点，结果全是坑：

| 猜测 | 结果 |
|------|------|
| `PUT /api/v1/ticket_comment/open` | 返回 `status:0`（**假成功**），工单状态实际未变 |
| `POST /api/v1/ticket/grab.json` | `ticket_is_null` 或类似错误 |

**方法论修正**：用 Playwright 在真实浏览器里操作一次，挂上请求拦截：

```python
page.on("request", lambda req: print(req.method, req.url, req.post_data))
```

一次就抓到了真实端点与完整请求体：

```
POST /api/v1/ticket_comment/receive.json?provider_id=10001&_token=<user_token>
```

> **教训**：真实端点必须从浏览器实际发出的流量里逆向。
> 不要凭文档、命名习惯或直觉猜接口 —— 代价是一轮又一轮的"假成功"。

---

## 2. 认证链路

```
POST /v3/login
  body: username, password, remember_me=true, captcha="",
        provider.id=10001, type=account
        │
        ▼
GET /v3/login-success          ← 必须跟随，user_token 在这一步写入 Cookie
        │
        ▼
Cookie: user_token=<...>       ← 之后所有 API 用 ?_token=<user_token>
```

**要点**

- `_token` **必须**是 `user_token`；用 `sid` 会得到 `status:3002`（鉴权失败）。
- 登录返回本身不带 token，token 由 `login-success` 的 `Set-Cookie` 下发。
- 会话会过期，所以主循环用 `try/except` 包住，异常时**重建 `KF5` 实例重新登录**（而不是整体退出）。

---

## 3. 查询"我的待接单"

```
GET /api/v1/view_tickets
  ?provider_id=10001
  &_token=<user_token>
  &_filter={"$and":[
      {"$or":[{"$eq":{"status":"new"}},{"$eq":{"status":"assigned"}}]},
      {"$eq":{"engineer.id":${current_engineer_id}}}
  ]}
  &_fields=id,status,no,subject,requester.name
  &_count=20&_page=1&_sort=createdAt&_order=desc
```

### 3.1 `engineer.id` 的占位符是核心安全设计

`${current_engineer_id}` **由 KF5 服务端替换**为当前登录工程师的 ID。
这意味着查询结果天然只包含**本人的**待接单 —— 不需要客户端做任何过滤，
也不可能误取到公共抢单池的工单。

这是业务硬约束「只在"我的待接单"接单，不碰公共池」的**技术保证**。

### 3.2 `_fields` 黑名单（静默失败陷阱）

| `_fields` 内容 | 结果 |
|----------------|------|
| `id,status,no,subject,requester.name` | ✅ 正常返回 |
| 含 `engineer` | ❌ **返回 0 条**（不报错！） |
| 含裸 `requester`（不带 `.name`） | ❌ 返回 0 条 |

**这类失败最危险：接口 HTTP 200、`status:0`、结构正常，只是 `tickets` 为空。**
排查时如果怀疑"接不到单"，第一个要检查的就是 `_fields`。

---

## 4. 接单请求

```
POST /api/v1/ticket_comment/receive.json?provider_id=10001&_token=<user_token>
Content-Type: application/json

{
  "ticket":      {"id": 100234567},
  "attachments": [],
  "content":     "<自动回复文案>",
  "open":        true,
  "user":        {"id": 10000001},
  "uId":         "<任意唯一字符串，用 uuid4().hex>"
}
```

成功响应：

```json
{"result": {"success": true, "ticketCommentId": 123}, "status": 0}
```

**字段说明**

| 字段 | 作用 |
|------|------|
| `ticket.id` | 目标工单 ID |
| `open: true` | **决定是否真正接单**，漏了或为 false 就只是留了条备注 |
| `content` | 同时发给门店的自动回复 |
| `user.id` | 以谁的身份回复（工程师 user_id） |
| `uId` | 幂等/去重用的唯一串，任意值即可，不参与校验 |
| `attachments` | 无需附件，固定 `[]` |

---

## 5. 回读验证：不信任"成功"

接单后**必须**再查一次状态：

```
GET /api/v1/tickets/{id}?_token=...    →  {"result":{"status":"open"}}
```

- 状态离开 `new`/`assigned`（通常是 `open`）→ 记 `accepted`，成功
- 状态仍是 `new`/`assigned`（重试 8 次后）→ 记 `accept ...: status stayed ...`，**需要人工关注**

这一步是对"假成功"端点的防御。因为历史上确实遇到过返回 `status:0` 但状态不变的接口。

---

## 6. 主循环结构

```python
while True:                       # 外层：永不退出
    if 不在运行窗口:
        sleep(到下一个窗口开始)     # 默认 07:30–22:00
    try:
        login()
        while 在运行窗口:
            for ticket in pending():
                if ticket.id in seen_ids: continue
                seen_ids.add(ticket.id)
                if accept(...) and 回读确认状态已变:
                    log("accepted ...")
            sleep(POLL_SECONDS)   # 默认 5s
    except Exception as e:
        log(e); client = KF5(); sleep(15)   # 自愈：重建客户端
```

**设计要点**

- **外层 `while True` 保证进程永不主动退出** —— 即使窗口结束也只是 sleep，不会退出让 systemd 重启。
- **`seen_ids` 防止同一工单被重复处理**（同一窗口内）。接单成功后 KF5 会把工单从"待接单"移除，但轮询存在时间差，去重是必要的。
- **异常自愈**：任何异常都只重建客户端并继续，**不允许异常冒泡到进程退出**。
- **日志双写**：文件（`accept.log`）+ stdout（systemd 收进 journal）。文件便于快速 `tail`，journal 便于 `journalctl` 关联。

---

## 7. systemd 托管

```ini
[Service]
Type=simple
WorkingDirectory=%h/gaoji-helpdesk
EnvironmentFile=%h/gaoji-helpdesk/secrets.env   # 凭据不进单元文件
ExecStart=%h/gaoji-helpdesk/venv/bin/python %h/gaoji-helpdesk/gaoji_accept.py
Restart=always
RestartSec=15
RestartPreventExitStatus=78                     # 配置缺失不无限重启
```

### 7.1 为什么凭据用 `EnvironmentFile` 而不是 `Environment=`

`Environment=` 的值会出现在 `systemctl show`、`journalctl`、`ps e` 等输出里，
也会被单元文件本身的备份/同步带出去。`EnvironmentFile=` 指向一个 600 权限的文件，
凭据不进单元文件，也不进 Git。

### 7.2 `Restart=always` 的边界（重要）

`Restart=always` **只在进程异常退出时生效**。当有人显式执行
`systemctl --user stop <unit>` 时，systemd 认为这是用户意图，**不会自动拉起**。

表现：单元 `enabled` 但 `inactive`，进程消失，日志停在最后一单 —— **静默漏单**。
这是 `watchdog.sh` 存在的唯一理由。维护后请务必 `gaoji check`。

### 7.3 `enable-linger`

systemd 用户实例随最后一个登录会话消亡。不执行
`sudo loginctl enable-linger $USER`，SSH 一断开服务就没了。

---

## 8. 陷阱清单（排错先看这里）

1. **`_fields` 黑名单** —— 含 `engineer` / 裸 `requester` → 静默返回 0 条
2. **`_token` ≠ `sid`** —— 用 `sid` 返回 `status:3002`
3. **`PUT /ticket_comment/open` 是假成功** —— 必须用 `POST receive.json`
4. **`open` 字段** —— `true` 才真正接单
5. **不要恢复浏览器方案** —— KF5 SPA 慢且 DOM 不稳，是漏单的根源
6. **改完代码必须重启服务** —— 否则旧进程仍在用旧逻辑/旧回复语
7. **先抓包再写代码** —— 不要猜端点
8. **`Restart=always` 救不了被显式 stop 的服务** —— 装看门狗
9. **没开 linger** —— SSH 断开服务跟着死
10. **`engineer.id` 必须用占位符** —— 客户端侧过滤不可靠，服务端占位符才是保证

---

## 9. 关于"接单通知"

早期版本会在接单时输出一行通知（供微信/QQ 转发）：

```
16:18-示例门店-设备无法启动
```

格式为 `HH:MM-客户名-工单主题`，每个工单一行。

后续业务方要求**不再推送通知**，因此当前版本默认只写日志。
需要恢复时，在 `gaoji_accept.py` 的 `main()` 里取消这一行注释，然后 `gaoji restart`：

```python
# print(notification(ticket), flush=True)
```

`notification()` 函数本身保留，格式未变。
