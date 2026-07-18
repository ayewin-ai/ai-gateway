# Enterprise AI Gateway

企業級 AI Gateway 參考實作：
**統一 API、地端/雲端多模型智慧路由、成本治理、安全檢查與管理 Dashboard**。

## 核心功能（九項產品定位）

| # | 功能 | 實作 |
|---|---------|------|
| 1 | 統一 OpenAI 相容 API | `POST /v1/chat/completions`、`GET /v1/models`（含串流 SSE） |
| 2 | 多模型路由 | `model=auto` 依敏感度/複雜度/可用性智慧路由（`gateway/router.py`） |
| 3 | 身分、權限與 API Key 管理 | API Key＋部門/使用者/應用歸屬、模型白名單、啟用/停用（`gateway/db.py`、Admin API） |
| 4 | Token 與成本治理 | Token 計量、成本換算、每日/每月預算、RPM 限速、80% 預警、超額熔斷、部門 Showback（`gateway/budget.py`） |
| 5 | 提示與回覆安全檢查 | DLP（身分證/信用卡/Email/電話/API Key）遮罩或阻擋、提示注入偵測、輸出遮罩（`gateway/security.py`） |
| 6 | 地端與雲端混合部署 | Ollama（地端）＋OpenAI 相容/Anthropic（雲端）＋Mock（離線 PoC）（`gateway/providers.py`） |
| 7 | 可觀測性與稽核 | 每筆請求 Trace：延遲、首 Token、Token I/O、路由原因、安全命中；稽核日誌（不保存原始敏感提示） |
| 8 | 快取、限速、重試、熔斷與高可用 | Prompt Cache（部門租戶隔離）、RPM 滑動視窗、失敗重試降級、circuit breaker |
| 9 | 硬體 Appliance＋軟體訂閱 | 軟體層：單一設定檔部署、`/admin/api/nodes/heartbeat` 支援多節點註冊監控 |

管理 Dashboard（`/dashboard`）提供六大面板：
**用量與歸屬、成本與配額、效能與可靠性、安全與稽核、模型品質、跨節點管理**，另含請求 Trace 明細。

## 快速開始

```bash
cd enterprise-ai-gateway
pip install fastapi uvicorn httpx pyyaml

# 啟動 Gateway（預設 http://localhost:8080）
python -m gateway.main

# 另開終端機：產生 Demo 流量（使用內建 Mock 模型，無需外網/GPU）
python scripts/demo_traffic.py

# 開啟管理 Dashboard
# http://localhost:8080/dashboard  （Master Key: sk-admin-master-key）
```

## 呼叫方式（OpenAI 相容）

```bash
# 智慧路由：model=auto，Gateway 依敏感度/複雜度自動選地端或雲端
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-gw-demo" \
  -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "什麼是 OEE？"}]}'

# 指定模型（含敏感資料時仍會被政策強制改地端）
curl ... -d '{"model": "gpt-4o", "messages": [...]}'

# 串流
curl ... -d '{"model": "auto", "stream": true, "messages": [...]}'

# 品質回饋（Dashboard 模型品質面板資料來源）
curl http://localhost:8080/v1/feedback \
  -H "Authorization: Bearer sk-gw-demo" -H "Content-Type: application/json" \
  -d '{"request_id": "<回應中的 gateway.request_id>", "model": "auto", "rating": 1}'
```

回應中的 `gateway` 欄位與 `x-gateway-*` headers 提供路由原因、實際模型、成本與預算預警。

## 接上真實模型

編輯 `config.yaml`：

- **地端**：安裝 [Ollama](https://ollama.com) 並 `ollama pull llama3.2`，即可使用 `local-llama3.2`。
- **雲端**：設定環境變數 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`，即可使用 `gpt-4o` / `claude-sonnet`。
- 任何 OpenAI 相容端點（vLLM、OpenRouter、Azure）都可用 `provider: openai` 直接註冊。
- 未設金鑰或模型不可用時，路由器會自動降級到其他候選模型（含 Mock）。

## 路由策略（config.yaml `routing`）

1. **敏感資料**（DLP 命中或敏感關鍵字）→ 強制地端（`local_only`）或拒絕（`block`），即使使用者指定雲端模型。
2. **複雜任務**（長文或命中複雜度關鍵字）→ 優先雲端大模型，失敗降級地端。
3. **簡單任務** → 優先地端小模型（成本 0），失敗升級雲端。
4. 模型連續失敗 3 次 → 熔斷 60 秒，流量自動切到其他候選（故障切換與備援）。

## 專案結構

```
gateway/
  main.py       # FastAPI 入口：/v1/chat/completions、/v1/models、/v1/feedback
  router.py     # 模型路由層：敏感度/複雜度路由、熔斷、降級
  providers.py  # Ollama / OpenAI 相容 / Anthropic / Mock 轉接
  security.py   # 安全治理層：DLP、提示注入、輸出遮罩
  budget.py     # 成本與配額層：成本換算、預算、限速
  cache.py      # 效能層:Prompt Cache（部門租戶隔離）
  db.py         # SQLite:Key、請求、事件、稽核、回饋、節點
  admin.py      # Admin API（Dashboard 資料來源）
static/dashboard.html   # 管理 Dashboard（自包含、離線可用）
scripts/demo_traffic.py # Demo 流量產生器（四類 PoC 場景）
config.yaml             # 模型註冊、路由政策、安全規則、配額預設
```

## 安全注意事項

- 正式環境請以環境變數 `GATEWAY_MASTER_KEY` 覆寫 master key，並置於 TLS 反向代理之後。
- 稽核預設**不保存原始敏感提示**（`security.store_prompt_in_audit: false`），避免稽核本身創造新的敏感資料集中風險。
- 本專案為 PoC 參考實作；正式導入請評估成熟開源方案（LiteLLM／Kong／APISIX／Envoy AI Gateway）與供應鏈安全要求（版本鎖定、SBOM、簽章驗證）。
