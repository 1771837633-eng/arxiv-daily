# arXiv Daily

一个最小可用的 arXiv 每日更新站点。

## 功能

- 每天抓取 arXiv `cond-mat/recent` 最近若干个发布日的论文
- 生成中文的摘要概括、文章主要内容、主要方法、关键词和分类信息
- 静态网页直接展示最新内容

## 本地运行

1. 先生成数据：

```powershell
python arxiv-daily/scripts/fetch_arxiv.py
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

### LLM 配置（`llm` 字段）

支持 OpenAI、DeepSeek 及任何 OpenAI 兼容 API：

```json
"llm": {
  "provider": "deepseek",
  "model": "deepseek-chat",
  "api_key_env": "DEEPSEEK_API_KEY",
  "base_url": "https://api.deepseek.com/v1",
  "max_concurrent": 3,
  "timeout": 60
}
```

| 字段 | 说明 |
|------|------|
| `provider` | `"openai"` / `"deepseek"` / 任意名称 |
| `model` | 模型名，如 `deepseek-chat`、`gpt-4.1-mini` |
| `api_key_env` | API key 所在环境变量名 |
| `base_url` | API 地址（需兼容 `/chat/completions`） |
| `max_concurrent` | 并发 LLM 请求数 |
| `timeout` | 单次请求超时（秒） |

**设置环境变量**（Windows PowerShell）：

```powershell
$env:DEEPSEEK_API_KEY = "sk-your-key-here"
```

或在系统环境变量中永久设置。没有 API key 时会自动使用规则引擎生成中文总结。

## 自动更新

仓库里已放入 GitHub Actions 定时更新配置，启用 GitHub Pages 后即可每天自动刷新。若需要 AI 中文总结，在仓库 Secrets 中添加 `DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY`。
