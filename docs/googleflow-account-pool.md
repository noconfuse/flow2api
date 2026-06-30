# Google Flow 号池部署说明

这份说明面向“100+ Google Flow 账号统一入池，对外只暴露一个 API 服务”的场景。

## 适不适合你的场景

这个仓库已经具备你要的核心能力：

- 多账号统一管理
- 外部统一通过一个 API Key 调用
- 请求自动分发到可用账号
- 账号 AT 过期自动刷新
- 429 账号自动禁用，后续定时自动解禁
- 支持 OpenAI 兼容接口和 Gemini 官方接口
- 支持按账号维度配置并发和打码代理

也就是说，不需要再额外做一层“代理转发服务”，这个项目本身就可以作为你的号池网关。

如果你要按“热池/冷池 + 浏览器 profile + 插件自动回传”的方式长期维护 100+ 账号，继续看：

- [100+ Google Flow 账号热池/冷池运维方案](hot-cold-pool-architecture.md)
- [账号池台账模板](account-pool-registry-template.csv)

## 推荐架构

推荐直接把这个服务部署成唯一对外入口：

1. 外部业务系统只调用 `Flow2API`
2. `Flow2API` 内部维护所有 Google Flow 账号
3. 每次请求由负载均衡器自动选择当前可用账号
4. 管理动作通过后台完成，批量账号可直接走后台 JSON 导入

建议部署形态：

- 单机 Docker 部署起步
- 反向代理放在 Nginx / Caddy / Cloudflare Tunnel 后面
- 只对外开放 API 服务，不直接暴露管理面
- 定期备份数据库和 `tmp` 目录

## 100+ 账号场景的建议配置

### 1. 打码方式

如果你是 100+ 账号长期运行，优先建议：

- `yescaptcha`
- `capsolver`

不建议一开始就用：

- `personal`

原因是 `personal` 更适合少量账号或人工维护浏览器状态的场景；大规模号池通常更适合第三方打码服务。

### 2. 调度模式

项目支持两种调度思路：

- `default`：更偏向负载感知，优先选择当前更空闲的账号
- `polling`：更偏向顺序轮询，适合平均消耗账号

建议：

- 想要更稳的吞吐：用 `default`
- 想要更平均地轮询 100+ 账号：用 `polling`

### 3. 并发设置

100+ 账号时不要把单账号并发开太高，建议先保守：

- `image_concurrency`: `1` 或 `2`
- `video_concurrency`: `1`

如果你没有给单账号单独配置，并发会按系统默认处理。建议先小并发试跑，再逐步提高。

### 4. 代理策略

如果不同账号需要不同出口，建议给 token 单独配置：

- `captcha_proxy_url`

这样可以让不同账号走各自代理，降低集中出口带来的风控关联。

## 快速部署

### Docker 部署

```bash
git clone https://github.com/TheSmallHanCat/flow2api.git
cd flow2api
cp config/setting_example.toml config/setting.toml
docker compose up -d
```

启动后访问：

- 管理后台：`http://localhost:8000`
- API 服务：`http://localhost:8000/v1/chat/completions`

首次默认账号：

- 用户名：`admin`
- 密码：`admin`

首次登录后立即修改后台密码和外部 API Key。

## 推荐配置项

编辑 `config/setting.toml`：

```toml
[global]
api_key = "replace-with-your-api-key"
admin_username = "admin"
admin_password = "replace-with-strong-password"

[call_logic]
call_mode = "polling"

[captcha]
captcha_method = "yescaptcha"
yescaptcha_api_key = "your-yescaptcha-key"

[flow]
image_slot_wait_timeout = 480
video_slot_wait_timeout = 480
image_launch_soft_limit = 20
video_launch_soft_limit = 20
```

说明：

- `api_key` 是外部系统调用你号池服务时使用的统一密钥
- `call_mode = "polling"` 更适合大号池均匀轮询
- `captcha_method` 按你的实际打码方案选择

## 批量导入 100+ 账号

推荐直接使用后台的 JSON 导入功能，适合每个账号单独带代理或路由。

创建 `tokens.json`：

```json
{
  "tokens": [
    {
      "session_token": "__Secure-next-auth.session-token=token-a",
      "captcha_proxy_url": "socks5://127.0.0.1:7891",
      "image_concurrency": 1,
      "video_concurrency": 1
    },
    {
      "session_token": "__Secure-next-auth.session-token=token-b",
      "captcha_proxy_url": "socks5://127.0.0.1:7892",
      "image_concurrency": 1,
      "video_concurrency": 1
    }
  ]
}
```

导入方式：

- 登录管理后台
- 进入 Token 管理页面
- 选择导入 JSON
- 粘贴或上传 `tokens.json`

后台接口是：

- `POST /api/tokens/import`

## 外部如何调用

你的业务系统只需要调用这个服务，不需要关心底层用了哪个 Google Flow 账号。

### OpenAI 兼容调用

```bash
curl -X POST "http://127.0.0.1:8000/v1/chat/completions" \
  -H "Authorization: Bearer replace-with-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_t2v_fast_landscape",
    "messages": [
      {
        "role": "user",
        "content": "一只猫在草地上奔跑"
      }
    ],
    "stream": true
  }'
```

### Gemini 官方格式调用

```bash
curl -X POST "http://127.0.0.1:8000/models/gemini-3.1-flash-image:generateContent" \
  -H "x-goog-api-key: replace-with-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [
          {
            "text": "生成一张海边日落图片"
          }
        ]
      }
    ],
    "generationConfig": {
      "responseModalities": ["IMAGE"]
    }
  }'
```

## 账号池是怎么自动切换的

服务内部会自动跳过以下账号：

- 已禁用账号
- 已过期且无法刷新 AT 的账号
- 当前模型不支持的账号
- 图片或视频能力被关闭的账号
- 命中并发上限的账号
- 因 429 临时封禁的账号

所以你对外只需要持续调用统一入口，系统会自动挑选当前可用账号。

## 运维建议

- 定期查看 `/health`
- 用后台检查 `/api/tokens` 中的过期、429、余额状态
- 给反向代理加访问控制，不要裸露管理后台
- 备份数据库，避免大量 token 配置丢失
- 批量导入前先拿 5 到 10 个账号试运行，确认打码和代理策略稳定

## 结论

如果你的目标是“做一个 Google Flow 号池，并提供统一外部 API”，这个仓库已经基本满足需求。

你接下来只需要完成三件事：

1. 部署服务
2. 批量导入 100+ 个账号
3. 用统一 API Key 对外提供调用

这样外部业务系统就不再直接管理单个 Google Flow 账号，而是统一走这个号池网关。
