# WhatsApp CRM Bridge 部署流程

把这个文件发给 Claude 桌面版，让它一步步带你做。

---

## 当前状态

- [x] 代码已写完（FastAPI + SQLite + Claude/Gemini 分析 + 飞书写入）
- [x] WATI API 已验证通（V1 + V3 都能用，3367 个联系人）
- [x] `.env` 已配好 WATI token + Gemini key + 飞书密钥
- [ ] **需要：一台有公网 IP 的 Linux 服务器**
- [ ] **需要：一个域名（WATI webhook 必须 HTTPS）**

---

## Step 1: 买服务器

任选一个（最便宜的就够）：

| 平台 | 推荐配置 | 价格参考 |
|------|---------|---------|
| 阿里云轻量 | 1C1G, Ubuntu 22.04 | ~30元/月 |
| Vultr | 1C1G, Ubuntu 22.04 | $6/月 |
| DigitalOcean | Basic Droplet | $6/月 |
| Bandwagon | THE CHICKEN | $49/年 |

**要求：**
- 系统：Ubuntu 22.04
- 有公网 IPv4
- 能 SSH 登录（root 或 sudo 用户）

买完后记下：
- 服务器 IP: `_______________`
- SSH 用户: `root`
- SSH 密码/密钥: `_______________`

---

## Step 2: 买域名 / 配子域名

如果已有域名，加一条 A 记录就行：

```
wa-crm.你的域名.com  →  A  →  服务器IP
```

如果没域名，去 Namesilo / Cloudflare / 阿里云买一个（.com 约 60元/年）。

配完后记下：
- 域名: `_______________`

**等 DNS 生效（通常 1-5 分钟）。**

---

## Step 3: 部署到服务器

在本地终端执行：

```bash
cd "~/Nutstore Files/我的坚果云/LuckyOS/LOCA-Factory-Brain/whatsapp-crm-bridge"

# 一键部署
./scripts/deploy.sh root@你的服务器IP
```

这个脚本会自动：
1. 上传代码到服务器 `/opt/whatsapp-crm-bridge/`
2. 安装 Docker + Nginx
3. 构建并启动容器
4. 配置 Nginx 反向代理

---

## Step 4: 配 SSL 证书

SSH 到服务器，执行：

```bash
ssh root@你的服务器IP

# 安装 certbot（如果 deploy.sh 没装的话）
apt install -y certbot python3-certbot-nginx

# 申请 SSL 证书（把域名替换成你的）
certbot --nginx -d wa-crm.你的域名.com
```

验证 HTTPS 是否生效：

```bash
curl https://wa-crm.你的域名.com/health
# 应该返回: {"status":"ok"}
```

---

## Step 5: 同步客户数据

```bash
# 从本地把客户数据库复制到服务器
scp "~/Nutstore Files/我的坚果云/LuckyOS/LOCA-Factory-Brain/Data/crm_customers.json" \
    root@你的服务器IP:/opt/whatsapp-crm-bridge/data/
```

---

## Step 6: 配置 WATI Webhook

1. 登录 WATI Dashboard: https://app.wati.io
2. 左侧菜单 → **Integrations** → **Webhooks**
3. 点 **Add Webhook**
4. 填写：
   - **URL**: `https://wa-crm.你的域名.com/api/v1/webhook`
   - **Events**: 勾选 `message-received`
5. 保存

---

## Step 7: 验证

### 7a. 让别人发一条 WhatsApp 消息给你的业务号

### 7b. 检查消息是否入库

```bash
ssh root@你的服务器IP
cd /opt/whatsapp-crm-bridge
docker compose logs --tail 20
```

应该能看到类似：
```
INBOUND CustomerName (text): hello
Saved inbound message wamid.xxx from 91xxxxxxxxxx
```

### 7c. 检查数据库

```bash
docker compose exec whatsapp-crm python3 -c "
import sqlite3
db = sqlite3.connect('data/whatsapp.db')
for row in db.execute('SELECT phone, direction, content FROM messages ORDER BY id DESC LIMIT 5'):
    print(row)
"
```

### 7d. 手动触发分析（测试完整链路）

```bash
curl -X POST https://wa-crm.你的域名.com/api/v1/analyze/trigger
```

应返回分析结果 JSON + 飞书写入结果。

---

## 完成后的架构

```
客户 WhatsApp ←→ WATI ←→ Webhook → 你的服务器
                                      ├─ SQLite 存消息
                                      ├─ 每晚 23:00 自动分析
                                      ├─ Gemini/Claude 做 CRM 分析
                                      └─ 写入飞书 CRM
                                          ├─ 客户管理CRM（新客户自动建）
                                          └─ 客户跟进记录（每日分析结果）
```

---

## 常用运维命令

```bash
# 查看日志
docker compose logs -f

# 重启服务
docker compose restart

# 查看统计
curl https://wa-crm.你的域名.com/api/v1/stats

# 手动触发分析
curl -X POST https://wa-crm.你的域名.com/api/v1/analyze/trigger

# 发消息（测试）
curl -X POST https://wa-crm.你的域名.com/api/v1/send \
  -H "Content-Type: application/json" \
  -d '{"to":"919876543210","text":"Hello from API"}'

# 更新代码后重新部署
./scripts/deploy.sh root@你的服务器IP
```

---

## 关键配置文件位置

| 文件 | 位置 | 说明 |
|------|------|------|
| 环境变量 | `/opt/whatsapp-crm-bridge/.env` | WATI token, API keys |
| 数据库 | `/opt/whatsapp-crm-bridge/data/whatsapp.db` | 所有消息 |
| 客户数据 | `/opt/whatsapp-crm-bridge/data/crm_customers.json` | 客户匹配库 |
| Nginx | `/etc/nginx/sites-available/whatsapp-crm` | HTTPS 反向代理 |
| Docker | `/opt/whatsapp-crm-bridge/docker-compose.yml` | 容器配置 |
