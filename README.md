# 凝聚态论文日报

基于 GitHub Actions 和 GitHub Pages 的每日摘要导读站点，arXiv 为主要来源，PRB 为可选来源。

## 功能

- 每天抓取 arXiv `cond-mat/recent` 最近若干个发布日的论文
- 生成职责分离的中文核心结论、研究问题与证据、方法、创新性和边界信息
- 静态网页直接展示最新内容
- 可选抓取 APS 官方 PRB RSS，生成摘要片段导读；不下载订阅全文
- 来源切换、关键词检索和分类筛选

## 工作流构造

1. `.github/workflows/daily-update.yml`：定时、push 或手动触发，准备 Python 和摘要缓存。
2. `config/arxiv.json`：设置最近几个 arXiv 发布日、PRB 开关、模型和输出路径。
3. `scripts/fetch_arxiv.py`：从 arXiv recent 页读取论文 ID，每批 50 篇通过 Atom API 获取标题、作者、摘要和分类；向 DeepSeek 请求中文摘要导读。没有密钥或模型调用失败时使用规则摘录。
4. PRB 独立读取 RSS。RSS 通常是截断的摘要片段，页面和模型输入会标注这个限制，不能据此声称阅读全文。
5. 数据验证后原子替换 `site/data/latest.json`，Actions 自动提交数据并部署 `site/`。
6. 浏览器读取 JSON 并筛选展示，不直接访问 arXiv、APS 或模型 API。网页“刷新”只重新读取已发布数据。
7. 定时或手动更新并成功部署后，独立邮件任务可发送日报；普通 push 不群发邮件。通知任务读取本次部署的确切数据，不重新抓取或调用模型。

arXiv 是必需来源。请求重试后仍失败，或返回数据缺失时，任务会报错并保留上一次数据，不部署空列表。PRB 请求失败时，arXiv 仍继续发布；有旧 PRB 数据时保留并标记为未更新，否则标记暂不可用。摘要缓存保存在 `data/.paper_cache.json`，通过 Actions Cache 在每天运行间复用。

## 本地运行

1. 先生成数据：

```powershell
python arxiv-daily/scripts/fetch_arxiv.py
```

从仓库目录运行也可以：

```powershell
python scripts/fetch_arxiv.py --config config/arxiv.json
```

仅抓取 arXiv：

```powershell
python scripts/fetch_arxiv.py --config config/arxiv.json --arxiv-only
```

不调用模型、仅检查抓取（仍可复用已有 AI 摘要）：

```powershell
python scripts/fetch_arxiv.py --config config/arxiv.json --no-llm --output data/diagnostic.json
```

2. 启动本地预览：

```powershell
cd arxiv-daily/site
python -m http.server 8010 --bind 127.0.0.1
```

3. 打开 `http://127.0.0.1:8010/`

如果浏览器缓存导致页面不更新，可以从项目根目录使用不缓存的预览脚本：

```powershell
python arxiv-daily/scripts/serve_site.py
```

## 配置

编辑 `arxiv-daily/config/arxiv.json`：

- `categories`：arXiv 分类
- `listing_days`：读取最近几个 arXiv 发布日
- `recent_list_show`：从 arXiv recent 页面一次显示多少条，默认 1000
- `use_openai_summary`：是否启用 LLM 总结（默认 true，需要 API key）
- `prb.enabled`：设为 `false` 可永久停用 PRB；下次运行后网页自动隐藏 PRB 入口
- `prb.recent_days`：PRB 最近几个自然日，与 arXiv 的发布日窗口不同
- `prb.max_results`：PRB 上限，RSS 自身也有条目数量限制
- `cache_path`：跨运行复用的摘要缓存路径

### LLM 配置（`llm` 字段）

支持 OpenAI、DeepSeek 及任何 OpenAI 兼容 API：

```json
"llm": {
  "provider": "deepseek",
  "model": "deepseek-flash",
  "thinking": "disabled",
  "api_key_env": "DEEPSEEK_API_KEY",
  "base_url": "https://api.deepseek.com/v1",
  "max_concurrent": 3,
  "timeout": 60
}
```

| 字段 | 说明 |
|------|------|
| `provider` | `"openai"` / `"deepseek"` / 任意名称 |
| `model` | 模型名，当前默认 `deepseek-flash` |
| `thinking` | DeepSeek 思考模式，短篇 JSON 导读默认 `disabled` |
| `api_key_env` | API key 所在环境变量名 |
| `base_url` | API 地址（需兼容 `/chat/completions`） |
| `max_concurrent` | 并发 LLM 请求数 |
| `timeout` | 单次请求超时（秒） |

**设置环境变量**（Windows PowerShell）：

```powershell
$env:DEEPSEEK_API_KEY = "sk-your-key-here"
```

或在系统环境变量中永久设置。不要把密钥提交到 Git。默认 `require_ai_summary: true`：没有有效 AI 导读时停止发布并保留旧数据，不把规则摘录当作中文 AI 总结。明确只需原文目录时，可设置 `use_openai_summary: false`。

### 最后接入 API

在 GitHub 仓库 **Settings > Secrets and variables > Actions > Repository secrets** 中新增或更新 `DEEPSEEK_API_KEY`。默认模型和地址已配置，无需把密钥写进配置文件。旧的 `deepseek-chat` 已停用，不要再沿用。

提交并推送代码后，手动运行工作流；首次验证不要勾选发送邮件。工作流先用一篇摘要检查 API 连通性和 JSON 结构，通过后才开始批量处理。此检查会产生一次模型调用，但不改写论文数据。本地可运行：

```powershell
python scripts/fetch_arxiv.py --config config/arxiv.json --check-llm
```

失败日志会区分缺少密钥、HTTP 401（鉴权）、402（余额）、404（模型或地址）以及无效响应，不输出密钥或服务商响应正文。预检失败时，不更新线上日报。

## 自动更新

仓库里已放入 GitHub Actions 定时更新配置，启用 GitHub Pages 后即可每天自动刷新。当前配置使用 DeepSeek，需要在仓库 Secrets 中添加 `DEEPSEEK_API_KEY`。如果改用 OpenAI，应同时修改 `llm.provider`、`model`、`base_url` 和 `api_key_env`，再设置 `OPENAI_API_KEY`；不同服务的密钥不能混用。

计划时间为北京时间 08:00，GitHub 队列可能延迟。手动运行 **Daily arXiv + PRB Update** 时，可勾选 **Update arXiv only (disable PRB)**，本次运行只更新 arXiv。

排查空页面时，先看来源、检索词和页面更新时间，再查看 Actions 的抓取步骤。Actions 成功与数据完整性是两个不同问题，脚本现在会检查所有请求 ID 是否都得到有效摘要。`127.0.0.1:8010` 只展示本地文件，GitHub 每日部署不会同步到本地；本地需要运行抓取脚本或拉取远程更新。

## 验证

```powershell
python -m unittest discover -s tests -v
node --check site/app.js
node --test tests/test_site.cjs
```

## 摘要质量与容错

### 本期综合导读

每次更新为最近三个有论文的日期生成“本期导读”：总览、1 至 5 条研究主线及阅读边界。按 arXiv 发布列表日期分期，PRB 使用对应发布日；跨来源综合导读不随列表搜索或来源筛选改变，页面明确标为“全来源”，点击引用会清除列表筛选并定位论文。选择全部日期时不混合生成跨日总览。

综合导读只依据已有有效 AI 单篇导读，不使用规则摘录冒充证据；每期按分类轮转选择最多 120 篇，页面显示实际覆盖数。主题必须引用本期输入中的论文 ID。它不是全文评审，也不能据此判断整个领域的趋势。

内容和模型不变时复用缓存；综合阶段总计最多 180 秒、6 次请求，失败不影响论文列表发布，也不会把旧日期的导读贴到新一期。用 `daily_brief.enabled: false` 可关闭。API 接入前显示尚未生成，不会在浏览器端暴露密钥或请求模型。

模型必须返回完整 JSON，且“核心结论”不能重复作为“研究问题”或“证据”。页面默认按最新一期展示，支持选择日报日期和仅看 AI 导读。列表直接展示核心结论，研究问题、证据、方法、创新性、适用边界和原始摘要折叠展示。未提供的信息必须标为未明确说明，不能把摘要导读当作全文评审。

没有 AI 导读的论文只展示原始摘要预览，并标为“待生成导读”；不再把启发式规则生成的研究背景、结论等字段作为日报正文。规则判断不代替 AI 翻译或学术评审。

- 摘要缓存检查结构版本、输入摘要和模型配置。旧版重复摘要不会被继续复用，首次升级可能需要重新生成。
- 模型临时错误默认最多尝试 2 次（首次请求加 1 次重试），鉴权等永久错误立即打开熔断，连续 5 次失败也会停止后续调用。
- 默认每次模型阶段预算 900 秒、最多 500 次 HTTP 请求，超过后对未缓存论文使用规则摘录；可以在 `llm` 中调整。
- PRB 摘要采用受限并发；arXiv 网络请求仍串行、至少间隔 3 秒，不以并发请求换取速度。
- 缓存原子写入，Actions 在后续步骤失败时也保存已完成的摘要。运行摘要展示各来源状态、AI/规则数量及模型请求次数。

## QQ 邮件与群发

此前仓库没有 SMTP 脚本或邮件任务。GitHub 自带的 Actions 状态邮件不是论文日报，也不会把论文列表发给指定收件人。

邮件任务使用 Python 标准库，不依赖第三方发信 Action。默认 QQ `smtp.qq.com:465`，使用 SSL 和邮箱 SMTP 授权码，不能填 QQ 登录密码。先在邮箱中开启 SMTP 并取得授权码，再进入仓库 **Settings > Secrets and variables > Actions**。

创建 Repository Secrets：

| 名称 | 内容 |
| --- | --- |
| `SMTP_USERNAME` | 发件邮箱完整地址 |
| `SMTP_PASSWORD` | SMTP 授权码或该服务商的应用密码 |
| `MAIL_TO` | 多个收件地址，以英文逗号、分号或换行分隔 |
| `MAIL_FROM` | 可选；不设置时使用 `SMTP_USERNAME` |

创建 Repository Variable `EMAIL_ENABLED`，值为 `true`。默认没有启用开关时会明确报告 `disabled`，不会偷偷发送邮件。

兼容其他支持密码/授权码登录的标准 SMTP 邮箱。可以覆盖这些 Repository Variables：

| 名称 | 含义 |
| --- | --- |
| `SMTP_HOST` | 邮箱服务商的 SMTP 服务器 |
| `SMTP_PORT` | SSL 通常为 465，STARTTLS 通常为 587；以服务商说明为准 |
| `SMTP_SECURITY` | `ssl` 或 `starttls`；不支持明文连接 |

要求 OAuth 的服务不能直接把普通密码填进来，需要服务商支持的应用密码或另做 OAuth 接入。QQ 邮箱的风控、日发送限制和收件人的垃圾邮件规则仍然适用，不保证无限群发。

每位收件人单独投递，不暴露其他收件人的邮箱；默认最多 50 位，投递间隔 2 秒。邮件节选最多 20 篇，均衡展示 arXiv/PRB，全文列表通过网站链接查看。`email.sources`、`keywords` 和 `max_papers` 可设置来源、关键词和数量。

北京时间同一天已被 SMTP 接受的收件人默认不再投递，失败收件人可以在重跑时继续尝试。记录仅保存地址哈希，放在网站目录之外的 Actions 缓存中，不部署到网站。公共仓库缓存不是秘密存储，因此不保存原始收件地址或凭据，地址哈希也不等于不可逆匿名化。缓存被删除、记录保存失败或人工 `force` 重发仍可能造成重复；SMTP 接受也不等于确认进入收件箱。若在 DATA 阶段断线导致结果不确定，不自动重发，以防刷屏。

### 邮件测试

发布修改后，在 Actions 中运行 **Test Email Digest**：

1. 默认 `dry_run=true`，生成 HTML/文本预览，不联网发信、不改投递记录。结果在 `email-test-results` artifact。
2. 配置 Secrets 后取消 `dry_run` 做真实测试；使用最新数据，脚本拒绝向群组发送超过 36 小时的旧日报。
3. `force` 会绕过当天去重，只有明确要重发时才勾选。这个测试工作流不调用论文抓取或模型。

定时任务北京时间 08:00 开始，成功部署后发信，不保证邮件恰好 08:00 到达。邮件任务失败会在 Actions 中单独报错并上传状态文件，但不会撤回已部署网站。凭据、收件人地址及授权码不写入公共数据或运行日志。

本地仅预览：

```powershell
python scripts/send_digest.py --dry-run --preview-dir data/email-preview
```

## 微信推送方案

当前只做方案评估，未开通或接入微信。建议发送“一条日报标题 + 数量 + 少量精选 + 网站链接”，而不是逐篇发送几百条。

| 方案 | 难度 | 优点 | 缺点和适用场景 |
| --- | --- | --- | --- |
| pushplus 订阅群组 | 低 | 收件人扫码订阅，同一条日报可一对多推送，不需自建服务器 | 发送方需实名认证；依赖第三方和微信通道政策。群组不是普通微信群，而是每人分别收到通知 |
| Server酱 Turbo | 低 | SendKey + HTTP 请求即可，适合自己每天收一条 | 当前免费 5 次/天；免费版不支持群发，多人发送需其他通道或付费配置。不要与独立 App 产品 Server酱³ 混淆 |
| 企业微信群机器人 | 低至中 | 发到现有企微群，团队统一查看，Webhook 接入直接 | 需要企业微信及可添加机器人的群；不是直接发到普通微信群 |
| 自建公众号/企业微信应用 | 高 | 用户与推送策略控制更完整 | 需要注册、用户授权、平台权限、令牌与安全配置；维护成本更高，不推荐作为第一步 |

结合当前的多人日报需求，优先 QQ SMTP；要落到每个人的普通微信通知可评估 pushplus；已经使用企业微信的研究组更适合企微群机器人。第三方渠道会接触推送正文，密钥应放在 Secrets，公开摘要和网站链接可以推送，私人笔记或未公开研究内容不要直接发给第三方平台。

当前额度与能力参考：[pushplus 一对多 API](https://pushplus.plus/doc/guide/api.html)、[实名认证与额度](https://pushplus.plus/doc/guide/use.html)、[Server酱官方说明](https://sct.ftqq.com/docs/getting-started/faq/)、[企业微信官方群机器人文档](https://developer.work.weixin.qq.com/document/path/91770)。实际权限与政策以各平台控制台为准。

参考：[APS 官方 RSS](https://journals.aps.org/feeds)、[arXiv API 访问频率规定](https://info.arxiv.org/help/api/tou.html)。arXiv 请求串行执行，间隔至少 3 秒，并对临时网络错误和不完整的 Atom 响应重试。
