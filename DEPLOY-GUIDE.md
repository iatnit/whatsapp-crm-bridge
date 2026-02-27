# WhatsApp CRM Bridge 部署流程 / Deployment Guide

把这个文件发给 Claude 桌面版，让它一步步带你做。
Send this file to Claude Desktop and let it guide you step by step.

---

## 当前状态 / Current Status

- [x] 代码已写完 / Code complete（FastAPI + SQLite + Gemini AI 分析 + 飞书写入 Feishu sync）
- [x] WATI API 已验证通 / WATI API verified（V1 + V3 都能用，3367 个联系人 contacts）
- [x] `.env` 已配好 / `.env` configured（WATI token + Gemini key + 飞书密钥 Feishu secrets）
- [x] 服务器已购买 / Server purchased（DigitalOcean SGP1, IP: `143.198.205.91`）
- [x] 域名已配置 / Domain configured（`wa-crm.zhangyun.xyz`）
- [x] SSL 证书已申请 / SSL certificate issued（Let's Encrypt, 自动续期 auto-renew）
- [x] WATI Webhook 已配置 / WATI Webhook configured
- [x] 系统已验证通过 / System verified and working

---

## 第一步 / Step 1: 买服务器 / Purchase a Server

任选一个（最便宜的就够）/ Pick one (cheapest is fine):

| 平台 Platform | 推荐配置 Config | 价格 Price |
|------|---------|---------|
| DigitalOcean ✅ 已选 | 1C1G, Ubuntu 22.04, SGP1 新加坡 Singapore | $6/月 per month |
| 阿里云轻量 Alibaba Cloud | 1C1G, Ubuntu 22.04 | ~30元/月 |
| Vultr | 1C1G, Ubuntu 22.04 | $6/月 per month |

> ⚠️ **建议选海外服务器** / Recommend overseas server
> 原因 Reasons：WATI 在海外（webhook 回调更稳定）、不需要域名备案、Gemini/Claude API 访问更顺畅
> WATI servers are overseas (stable webhook callbacks), no ICP filing needed, better access to Gemini/Claude APIs

**要求 / Requirements：**
- 系统 OS：Ubuntu 22.04
- 有公网 IPv4 / Public IPv4
- 能通过网页控制台登录 / Access via web console (SSH 可能有兼容性问题 may have compatibility issues)

**当前服务器信息 / Current Server Info：**
- 服务器 IP / Server IP: `143.198.205.91`
- 平台 Platform: DigitalOcean
- 区域 Region: SGP1（新加坡 Singapore）
- 登录方式 Access: DigitalOcean 网页 Console / Web Console

---

## 第二步 / Step 2: 配域名 / Configure Domain

在 DNS 管理面板添加一条 A 记录 / Add an A record in your DNS panel:

```
wa-crm.zhangyun.xyz  →  A  →  143.198.205.91
```

**当前配置 / Current Config：**
- 域名 Domain: `zhangyun.xyz`（在 GoDaddy 购买 purchased on GoDaddy）
- DNS 托管 DNS hosting: Cloudflare
- 子域名 Subdomain: `wa-crm.zhangyun.xyz`
- 代理状态 Proxy: 仅 DNS / DNS only（灰色云朵 grey cloud）

> 💡 DNS 通常 1-5 分钟生效 / DNS usually takes 1-5 minutes to propagate

---

## 第三步 / Step 3: 部署到服务器 / Deploy to Server

因为 SSH 不可用，我们通过 GitHub + 网页 Console 部署。
Since SSH is unavailable, we deploy via GitHub + Web Console.

### 3a. 推送代码到 GitHub / Push code to GitHub

在本地 Mac 终端执行 / Run in local Mac terminal:

```bash
cd ~/Nutstore\ Files/我的坚果云/LuckyOS/LOCA-Factory-Brain/whatsapp-crm-bridge
git add -A
git commit -m "update code"
git push
```

### 3b. 在服务器上拉取代码 / Pull code on server

打开 DigitalOcean 网页 Console，执行 / Open DigitalOcean Web Console and run:

```bash
# 安装 Docker / Install Docker
apt-get update && apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# 安装 Nginx / Install Nginx
apt-get install -y nginx

# 拉取代码 / Clone code（仓库需要设为 Public，拉完后改回 Private）
# Repo must be Public to clone; change back to Private after
git clone https://github.com/iatnit/whatsapp-crm-bridge.git /opt/whatsapp-crm-bridge
```

### 3c. 创建环境变量文件 / Create .env file

```bash
cat > /opt/whatsapp-crm-bridge/.env << 'EOF'
# WATI（WhatsApp 消息接口 / WhatsApp messaging API）
WATI_API_ENDPOINT=https://live-mt-server.wati.io
WATI_TENANT_ID=你的租户ID / your tenant ID
WATI_API_TOKEN=你的WATI令牌 / your WATI token

# LLM（AI 分析引擎 / AI analysis engine）
ANTHROPIC_API_KEY=test
GEMINI_API_KEY=你的Gemini密钥 / your Gemini key
LLM_PROVIDER=gemini

# 飞书 / Feishu (Lark)
FEISHU_APP_ID=你的飞书应用ID / your Feishu app ID
FEISHU_APP_SECRET=你的飞书密钥 / your Feishu app secret

# 应用设置 / App Settings
LOG_LEVEL=DEBUG
DAILY_ANALYSIS_HOUR=23
DAILY_ANALYSIS_MINUTE=0
EOF
```

### 3d. 启动服务 / Start the service

```bash
cd /opt/whatsapp-crm-bridge
mkdir -p data/media
docker compose up -d --build
```

---

## 第四步 / Step 4: 配置 Nginx 反向代理 / Configure Nginx Reverse Proxy

Nginx 把外部请求转发给 Docker 容器里的应用。
Nginx forwards external requests to the app running in the Docker container.

```bash
# 创建 Nginx 配置 / Create Nginx config
cat > /etc/nginx/sites-available/whatsapp-crm << 'EOF'
server {
    listen 80;
    server_name wa-crm.zhangyun.xyz;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 50m;
    }
}
EOF

# 启用配置 / Enable config
ln -sf /etc/nginx/sites-available/whatsapp-crm /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

# 测试并重启 / Test and reload
nginx -t && systemctl reload nginx
```

---

## 第五步 / Step 5: 配 SSL 证书 / Set Up SSL Certificate

SSL 证书让网站支持 HTTPS（加密传输），WATI Webhook 必须用 HTTPS。
SSL enables HTTPS (encrypted traffic). WATI Webhook requires HTTPS.

```bash
# 安装 certbot / Install certbot
apt-get install -y certbot python3-certbot-nginx

# 申请证书（自动配置 Nginx）/ Request certificate (auto-configures Nginx)
certbot --nginx -d wa-crm.zhangyun.xyz

# 验证 HTTPS 是否生效 / Verify HTTPS works
curl https://wa-crm.zhangyun.xyz/health
# 应该返回 / Should return: {"status":"ok"}
```

> 💡 证书会自动续期，无需手动操作 / Certificate auto-renews, no manual action needed

---

## 第六步 / Step 6: 配置 WATI Webhook

WATI Webhook 让 WhatsApp 消息自动推送到你的服务器。
WATI Webhook pushes WhatsApp messages to your server automatically.

1. 登录 WATI 后台 / Login to WATI Dashboard: https://app.wati.io
2. 左侧菜单 → **集成 Integrations** → **Webhooks**
3. 点 **添加 Webhook / Add Webhook**
4. 填写 / Fill in：
   - **URL**: `https://wa-crm.zhangyun.xyz/api/v1/webhook`
   - **事件 Events**: 勾选 **收到信息 / message-received**
5. 保存 / Save

---

## 第七步 / Step 7: 验证 / Verification

### 7a. 测试消息接收 / Test message receiving

让别人用 WhatsApp 发一条消息给你的业务号。
Have someone send a WhatsApp message to your business number.

### 7b. 检查日志 / Check logs

```bash
cd /opt/whatsapp-crm-bridge
docker compose logs --tail 20
```

应该能看到类似 / Should see something like:
```
INBOUND CustomerName (text): hello
Saved inbound message wamid.xxx from 91xxxxxxxxxx
```

### 7c. 检查数据库 / Check database

```bash
docker compose exec whatsapp-crm-bridge python3 -c "
import sqlite3
db = sqlite3.connect('data/whatsapp.db')
for row in db.execute('SELECT phone, direction, content FROM messages ORDER BY id DESC LIMIT 5'):
    print(row)
"
```

### 7d. 手动触发 AI 分析 / Manually trigger AI analysis

测试完整链路：WhatsApp 消息 → Gemini AI 分析 → 写入飞书 CRM
Test full pipeline: WhatsApp messages → Gemini AI analysis → write to Feishu CRM

```bash
curl -X POST https://wa-crm.zhangyun.xyz/api/v1/analyze/trigger
```

应返回分析结果 JSON + 飞书写入结果。
Should return analysis JSON + Feishu write results.

---

## 系统架构 / System Architecture

```
客户 WhatsApp ←→ WATI ←→ Webhook → 你的服务器 Your Server
Customer                              (143.198.205.91)
                                      ├─ SQLite 存消息 / Store messages
                                      ├─ 每晚 23:00 自动分析 / Auto-analyze at 23:00 daily
                                      ├─ Gemini AI 做 CRM 分析 / CRM analysis
                                      └─ 写入飞书 CRM / Write to Feishu CRM
                                          ├─ 客户管理 / Customer management（新客户自动建 auto-create）
                                          └─ 客户跟进记录 / Follow-up records（每日分析 daily analysis）
```

---

## 常用运维命令 / Common Operations

```bash
# 查看实时日志 / View live logs
docker compose logs -f

# 重启服务 / Restart service
docker compose restart

# 查看统计数据 / View statistics
curl https://wa-crm.zhangyun.xyz/api/v1/stats

# 手动触发分析 / Manually trigger analysis
curl -X POST https://wa-crm.zhangyun.xyz/api/v1/analyze/trigger

# 发送消息（测试用）/ Send message (for testing)
curl -X POST https://wa-crm.zhangyun.xyz/api/v1/send \
  -H "Content-Type: application/json" \
  -d '{"to":"919876543210","text":"Hello from API"}'

# 更新代码后重新部署 / Redeploy after code update
cd /opt/whatsapp-crm-bridge
git pull
docker compose up -d --build
```

---

## 关键配置文件位置 / Key Config File Locations

| 文件 File | 位置 Location | 说明 Description |
|------|------|------|
| 环境变量 Env vars | `/opt/whatsapp-crm-bridge/.env` | WATI 令牌、API 密钥 / WATI token, API keys |
| 数据库 Database | `/opt/whatsapp-crm-bridge/data/whatsapp.db` | 所有聊天消息 / All chat messages |
| 客户数据 Customer data | `/opt/whatsapp-crm-bridge/data/crm_customers.json` | 客户匹配库 / Customer matching database |
| Nginx 配置 | `/etc/nginx/sites-available/whatsapp-crm` | HTTPS 反向代理 / HTTPS reverse proxy |
| Docker 配置 | `/opt/whatsapp-crm-bridge/docker-compose.yml` | 容器配置 / Container config |

---

## 故障排查 / Troubleshooting

| 问题 Problem | 可能原因 Possible Cause | 解决方法 Solution |
|------|------|------|
| Webhook 返回 403 | 代码版本旧，有签名验证 / Old code with signature verification | 更新代码 `git pull` + 重建 `docker compose up -d --build` |
| SSH 连不上服务器 | OpenSSH 版本不兼容 / OpenSSH version incompatible | 使用 DigitalOcean 网页 Console |
| HTTPS 不生效 | SSL 证书未申请 / SSL cert not issued | 运行 `certbot --nginx -d wa-crm.zhangyun.xyz` |
| 消息收不到 | WATI Webhook 未配置或 URL 错误 / Webhook not configured | 检查 WATI 后台 Webhook 设置 |
| AI 分析失败 | Gemini API Key 无效 / Invalid API key | 检查 `.env` 里的 `GEMINI_API_KEY` |
| 飞书写入失败 | 飞书应用密钥错误 / Feishu credentials wrong | 检查 `.env` 里的 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` |
