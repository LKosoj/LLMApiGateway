# Fault-Tolerant Personal LLM Gateway

[Русская версия](README.md)

---
<div align="center" style="text-align: center;">
 <img alt="LLM Gateway" src="https://img.shields.io/badge/LLM-Gateway-blue?style=flat" />&nbsp;
 <a href="https://openrouter.ai"><img alt="OpenRouter" src="https://img.shields.io/badge/OpenRouter-AI-blue?style=flat" /></a>
 &nbsp;
 <a href="https://www.paypal.com/donate/?business=G47L9N4UW8C2C&no_recurring=1&item_name=Thank+you+%21%21%21&currency_code=USD"><img alt="Download" src="https://img.shields.io/badge/Donate-😊-yellow?style=flat" /></a>
 <br>
 <a href="https://cline.bot/"><img alt="Cline" src="https://img.shields.io/badge/Cline-AI Coder-blue?style=flat" /></a>
 <a href="https://roocode.com/"><img alt="RooCode" src="https://img.shields.io/badge/RooCode-AI Coder-blue?style=flat" /></a>
  
</div>
<br>
 
This project is a personal LLM Gateway that allows developers to use LLMs from different LLM providers with features like fault tolerance, model load balancing, customized model requests, call retries, and more.
The LLM Gateway works locally as an OpenAI-compatible LLM API provider with advanced fallback support for models in case of response failures.
Use it with code agents like Cline, RooCode, or even with your applications as a regular OpenAI API-compatible LLM provider.

## Features

- **Fault Tolerance**: Automatically falls back to alternative models if the primary model fails.
- **OpenRouter Provider Ordering**: Define the order of the providers routed by OpenRouter.
- **Model Rotation**: Optionally rotates through available models for each API key, distributing load and cost across providers.
- **Flexible Configuration**: Configure fallback sequences and rotation settings per model.
- **Custom LLM Parameters**: Configure custom request parameters for any LLM model.
- **Usage Statistics**: Track the cost and consumption of tokens per hour, day, week, or month. A separate `cost_saved` column aggregates prompt-caching savings — if upstream reports `usage.cost_saved`/`saved_cost` the value is used verbatim; otherwise the gateway computes a conservative estimate from `cached_tokens` and the implied per-prompt-token rate.
- **Token estimation on stream interruption**: when a provider does not return a terminal `usage` block (stream cancelled client-side, timeout, etc.), the gateway estimates `prompt_tokens`/`completion_tokens`/`reasoning_tokens` locally via `tiktoken` and stamps the DB record with `is_estimated=1`. In the UI, estimated records show a `≈` marker in the `Estimated` column.
- **Retry-After + jitter**: retry logic honors the downstream `Retry-After` header (delta-seconds or HTTP-date, clamped to 120s); otherwise it applies ±25% jitter to the configured `retry_delay` to smooth out thundering-herd effects during simultaneous failures.
- **Temporary upstream-key cooldown**: if a chat fallback-chain model hits a temporary availability failure (`429`, `5xx`, timeout/connect, `overloaded`, `rate_limit`, `try again later`), the gateway marks the concrete upstream API key for that `provider/model` pair in an in-memory cooldown and chooses another available key on later requests without making a wasted downstream call. If the failure is rate-limit-class (`429`, daily quota, an observed `remaining == 0` from `x-ratelimit-*` headers) and the provider has several keys configured, the gateway switches to the next available key immediately — still inside the current request, on the same rule and with no pause between attempts. Such switches do not consume the model's `retry_count` (they are capped at the number of candidate keys), every per-key attempt produces its own `attempts[]` entry, and the chain-wide `max_total_attempts` budget still applies on top. Most temporary failures use a fixed 10-minute cooldown; `429`s and daily-quota errors follow a separate escalation schedule instead (see below). The marker is kept in process memory and resets on restart.
- **Cooldown escalation on 429s and daily quotas**: a single `429` gets a short 90-second transient cooldown; repeated "exhaustions" of a key within a rolling 24-hour window escalate along a ladder of 120s → 600s → 3600s → 86400s. The provider's `Retry-After` acts as a floor on this branch and never shortens the cooldown. Daily-quota errors (markers such as `daily`/`quota`/`exhausted` in the error text) block the key until UTC midnight; there, the provider's `Retry-After` is an override instead. Limits mentioned inside an error body (rpm/tpm/rpd/tpd) are learned and remembered per upstream key.
- **Upstream routing state**: the gateway keeps in-memory state per `(provider, model, upstream key)`: read-only health (`healthy`/`invalid`/`error`), last check, last error, cooldown, RPM/RPD/TPM/TPD ledger, and fast success/latency/error aggregates. This does not change the public `/health`; the data is exposed to master users through API and the statistics UI. It also reads `x-ratelimit-*` response headers from Groq, Cerebras, and OpenRouter and writes the observed limit/remaining/reset into the same registry keyed by `(provider, model, key)`; once `remaining` reaches 0 the key is auto-blocked until reset and auto-unblocked afterward. The values are visible via `GET /v1/api/upstream-status` as `observed_limits` and `cooldown_escalation_level`.
- **Client-safe errors and an attempts trail**: upstream error text that reaches the client in the final response is always scrubbed of secrets (`Bearer`/`Basic`/`Token`/`ApiKey` tokens, `sk-`/`gsk_`/`lgk_`/`AIza...`-style keys, JWTs, URLs with credentials) and truncated to 240 characters. The final `503` on fallback-chain exhaustion additionally carries `attempts[]` — provider, model, an anonymized upstream-key alias (`key1`/`key2`…), `error_class`, and `http_status` per attempt — and `retry_after_seconds`: the real minimum remaining cooldown across the attempted upstream keys (the same value is also duplicated in the `Retry-After` header).
- **Failover on a "degenerate" HTTP-200 response**: if a non-streaming provider response is technically successful but empty (`empty_completion`) or ignores `response_format: json_object`/`json_schema` (`format_ignored`), the gateway immediately moves to the next model in the chain — without any cooldown or key penalty, since this is diagnosed as a model-behavior failure, not a key/provider failure. The class is visible in `attempts[].error_class`. Responses that only have `reasoning_content`/`thinking` with no `content` are not treated as empty — except when `response_format: json_object`/`json_schema` was requested: the client still needs JSON in `content`, so such a response is still flagged as `empty_completion`.
- **Tool-call rescue**: an opt-in (disabled by default) `tool_call_rescue` flag on a fallback rule recognizes 4 text-based tool-call dialects — Kimi sections (`<|tool_calls_section_begin|>`), `<function=NAME>{json}</function>`, `<tool_call>{json}</tool_call>`, and bare/```json-fenced JSON that strictly matches a declared tool schema — and converts them into structured `tool_calls` with `finish_reason="tool_calls"`, repairing the arguments against the tool's JSON Schema along the way (unwraps double-JSON-encoding, coerces a string to an array/object only when the schema requires it). An unrecognized dialect triggers failover (`unparsed_tool_call_dialect`) in non-streaming mode; in streaming mode there is a hold window of up to 256 characters that synthesizes `delta.tool_calls` on the fly, and on a parse failure the raw text is passed through as-is (failover is impossible there since headers were already sent). This is never applied to native Anthropic providers. Enabled via a checkbox in the Fallback Rules web editor.
- **Capability-based fallback-candidate filtering**: the fallback-rule fields `supports_vision`, `supports_tools` (`true`/`false`/unset), and `context_window` (> 0 if set) pre-filter clearly incompatible chain candidates — for example, a request with images or tools skips models without that support, and a request whose estimated size (token estimate + response reserve) exceeds `context_window` skips models that are too small. If no candidate remains, the gateway returns `422` with `code=no_capable_model` and the list of filtered-out candidates with a reason. The gateway auto-fills unset fields on its own: first from the provider's own `/models` catalog, otherwise from the public OpenRouter catalog — and materializes the values directly into `models_fallback_rules.json`, marking them with the service field `capabilities_autofilled`. Manually-set values are never overwritten; manual input fields appear in the Fallback Rules web editor only where auto-fill did not succeed, while auto-filled fields render as a read-only "value + source" badge with an "Edit" button.
- **Chain-wide retry budget**: a fallback rule can define `max_total_attempts` as a hard cap on the total number of attempts across the entire fallback chain, not just the per-model `retry_count`. When the budget is exhausted, the chain stops even if later fallback models were not tried. The field is exposed in the editor as `Max Total Attempts (chain budget)`.
- **Safe fallback diagnostics**: dynamic penalty after `429`/`5xx` is enabled only explicitly per fallback rule and does not silently change route order. Diagnostic headers `X-Routed-Via` and `X-Fallback-Attempts` are disabled by default and enabled with `ROUTING_DIAGNOSTIC_HEADERS=true`.
- **Startup model verification**: on startup the gateway can optionally hit each provider's `/models` endpoint from `providers.json` and diff the response against what is referenced in fallback/operation rules. The mode is controlled by `VERIFY_MODELS_ON_STARTUP=off|warn|strict`: `warn` (default) logs missing models as warnings, `strict` aborts startup, `off` disables the check entirely.
- **OpenRouter free-model ranking**: if the official `openrouter` provider and its API key are configured, the gateway analyzes free OpenRouter text models every 8 hours. For scoring, you can provide multiple OpenRouter keys separated by commas; each OpenRouter request selects the next non-empty key by round-robin. When the eligible model list changes, it runs a full score for every eligible model without a quantity cap; lite eval is skipped only for models that fail the health probe. When the list is unchanged, it refreshes health/latency for every model and catches up lite eval for models that recovered from a previously failing health probe. The final score is `round((metadataScore + healthScore + latencyScore - instabilityPenalty) * 0.8 + liteEvalScore * 1.6)` — eval tests count twice as much as the other metrics, so a model with a huge context but failing tests can no longer outrank a model with real working capabilities. Lite eval checks instruction following, tool-call JSON, Python code with unit tests, parameterized arithmetic, and factual QA. The result is shown in the read-only **OpenRouter Free** tab in `/v1/ui/rules-editor`; a compact scoring-metric guide is shown above the ranking. The `Run Full Eval` button (or `POST /v1/openrouter/free-models/run`) triggers a background full re-evaluation of every eligible model without waiting for the next scheduled cycle; if a manual refresh is already in progress, the endpoint returns `409`. Model routing is not changed automatically yet.
- **Manual fallback-model eval**: the **Fallback Eval** tab can run the same lite eval on demand for unique `provider/model` targets from configured `fallback_models` and `context_overflow_fallback`. Duplicate targets are collapsed and show every gateway model that references them. If the official `openrouter` provider and API key are configured, the gateway loads the OpenRouter catalog and matches metadata by model basename: `openai/gpt-oss-120b:free` matches `gpt-oss-120b`; if metadata is missing for some targets but other targets have known OpenRouter scores, the missing `metadataScore` uses the median known score. The final score is `round((metadataScore + healthScore + latencyScore - instabilityPenalty) * 0.8 + liteEvalScore * 1.6)` — eval tests count twice as much as the other metrics, so a model with a large context but failing tests cannot outrank a model with real working capabilities; if OpenRouter is not configured or no metadata is known in the current run, `metadataScore` stays at 0. Lite eval runs for every available target without a quantity cap. OpenAI-compatible fallback routes are evaluated through `/chat/completions`, native Anthropic routes through `/v1/messages`; the eval does not change routing rules.
- **Virtual API keys**: on top of the master `GATEWAY_API_KEY` you can now provision per-user virtual keys via the `/v1/ui/api-keys` page (master-only). Every key is stored plaintext in `db/api_keys.db` and carries its own USD budget, RPM/TPM limits, and an optional `allowed_models` whitelist. When the budget is exhausted the gateway replies `429`; when a model is not allowed it replies `403`. Virtual keys authenticate via `Authorization: Bearer <lgk_...>` or `X-Api-Key`; their UI session exposes only **Docs**, **Usage Statistics**, and **Quota**, not administration pages. The master key sees the global usage set across all requests. An unset `budget_usd` disables budget tracking for that key.
- **Tool-schema compatibility shim**: before forwarding requests with `tools` to downstream providers, the gateway rewrites the JSON Schema dialect used in function parameters (flattens `["string","null"]`, collapses `anyOf/oneOf` nullable variants into `nullable: true`, converts draft-04 boolean `exclusiveMaximum/exclusiveMinimum` into draft-07 numeric bounds, and strips `$schema`/`$id`/`$comment`). This removes common 400 errors from strict OpenAI-compatible providers and gRPC backends.
- **Fusion ensembles**: a dedicated gateway model (e.g. `llmgateway/fusion-quality`) that internally runs a "panel" of several models in parallel, then a judge model produces a structured breakdown (agreements, disputes, per-model insights, blind spots), and a main model compiles one final answer from the panel answers and breakdown instead of selecting a single winner. From the outside it is a regular `/v1/chat/completions` call. The panel and roles (`main_model`, `judge_model`) are specified as `provider/model` pairs, so the panel can mix models from any configured provider directly, as well as the gateway itself registered as a regular provider. Configured in the **Fusion** tab of the configuration editor or in `models_fusion_rules.json`.
- **Router models**: a dedicated gateway model (for example `llmgateway/router`) that first calls a configured selector model from `models_fallback_rules.json`, then chooses one target from an explicit allowlist. A target can be a full gateway chat model with its normal fallback chain, or a specific entry inside a fallback chain after which the gateway continues with the remaining entries. It is configured in the **Router** tab of the configuration editor and stored in `models_router_rules.json`; Router never selects models outside the config and never silently substitutes another target when the selector output is invalid.


## Gateway endpoints

  - `/v1/models` - Like v1, returns the list of available models. For gateway models defined in chat/embeddings/rerank/images/audio_speech/audio_transcriptions/pdf/web rules, the response also includes merged `capabilities` plus normalized `type` and `architecture` metadata from the matching sections. When the request is served in Anthropic format, the endpoint keeps the Anthropic-compatible `type: "model"` field and exposes the gateway task marker separately as `gateway_type`, together with the same `capabilities` and `architecture`.
  - `/v1/chat/completions` - OpenAI compatible API that routes calls to other providers with fallback in case of call failure. Playground includes a simple Chat mode for this endpoint: it keeps local context up to 20 messages and can clear the history with the `Reset chat` button. The model dropdown also lists Fusion ensembles (marked `· Fusion`) and Router models from `models_router_rules.json`. Fusion models show an `include_details` checkbox and render an expandable block with the judge breakdown and panel answers under the assistant message; Router responses additionally include a JSON `router` field with the selected target.
  - `POST /v1/embeddings` - OpenAI Embeddings-compatible API. It accepts `model`, `input`, `encoding_format`, and `dimensions`, proxies the request to a downstream provider according to `models_operation_rules.json`; multiple routes for the same gateway model are used as an ordered fallback chain. Each route first applies its own `retry_count`/`retry_delay`; after a retryable `502`/`503` or a route-specific accounting validation error, the endpoint tries the next route before terminal commit begins. Rotation is not supported.
  - `POST /v1/rerank` - Rerank API. It accepts `model`, `query`, `documents` (`list[str]`), `top_n`, and `return_documents`, maps `query -> text_1` and `documents -> text_2`, sends the request to `target_path` (default `"/score"`); multiple routes for the same gateway model are used as an ordered fallback chain. Each route first applies its own `retry_count`/`retry_delay`; the next route is tried after a retryable `502`/`503`, an invalid downstream rerank response, or a route-specific accounting validation error before terminal commit begins. By default, successful downstream responses are normalized to `{"data": [{"index": int, "score": float, "document"?: str}]}`. If the route field `response_output_format` is set to `jina_results`, the gateway returns the Jina-style shape `{"results": [{"index": int, "relevance_score": float, "document"?: str}]}`. If `target_path` is an absolute `http(s)` URL, the gateway uses it as the full downstream URL instead of joining it with `provider.baseUrl`. For NVIDIA rerank routes, the gateway automatically switches to the native `{"query":{"text": ...}, "passages":[{"text": ...}]}` payload. For rerank services that expect `{"query": ..., "texts": [...]}` and return `{"scores": [...]}`, configure the route with `request_format: "query_texts"` and `response_format: "scores"`; the gateway maps each score back to the source document index and sorts results by score. The `top_n` parameter additionally truncates the result list, and `return_documents=true` reinserts the caller's original documents by `index` or position.
  - `POST /v1/images` and `POST /v1/images/generations` - OpenAI Images generation-compatible endpoints. They accept JSON payloads with `model`, `prompt`, and generation parameters; multiple routes for the same gateway model are used as an ordered fallback chain. Each route first applies its own `retry_count`/`retry_delay`, and the next route is tried only after a gateway-level `503`, including downstream error statuses, network errors, invalid JSON, or invalid image responses. Rotation and streaming are not supported. By default a route behaves as an OpenAI-compatible passthrough, but it can switch to a mapping-based adapter through `request_format`, `response_format`, `request_mapping`, and `response_mapping`.
  - `POST /v1/images/edits` - OpenAI Images edits-compatible endpoint. It supports both JSON bodies with `images` and `multipart/form-data` uploads with `image` / `image[]` and `mask`, rewrites `model` to the configured route model; multiple routes for the same gateway model are used as an ordered fallback chain after a gateway-level `503` or downstream multipart `413 Payload Too Large`. Rotation is not supported, and `stream=true` explicitly returns an error. By default, caller JSON is sent downstream as JSON and caller multipart is sent downstream as multipart; if an edit route sets `request_format: "openai_images_multipart"`, the caller JSON body is converted to downstream `multipart/form-data` for models that do not accept image edit JSON. Edit routes can also use mapping-based adapters for downstream APIs with a different wire format, such as NVIDIA image generation/edit endpoints, without changing the public OpenAI-compatible gateway API.
  - `POST /v1/audio/speech` - OpenAI Audio Speech-compatible endpoint. It accepts JSON with `model`, `input`, `voice`, `response_format`, `speed`, and similar OpenAI-style parameters, rewrites `model` to the configured route model from `audio_speech`, and returns raw downstream audio bytes with the downstream `content-type`. Fallback/rotation are not supported; a single route can still use `retry_count` and `retry_delay`, and `stream=true` returns an explicit `400`.
  - `GET /v1/audio/voices` - TTS voice catalog. With a `model` query parameter it returns voices for one gateway model; without `model` it returns a common JSON object shaped as `gateway_model -> voices[]` for all available `audio_speech` models, filtered by virtual-key `allowed_models`. The gateway normalizes downstream `voices` / `preset_voices` / `custom_voices`; when downstream voices include a `model` field, the gateway filters the catalog by the selected route model. It adds known `gender` and `language` metadata for built-in Silero voices, and leaves unknown voices as plain `id`, `name`, and `source`.
  - `POST /v1/audio/transcriptions` - OpenAI Audio Transcriptions-compatible endpoint. It accepts `multipart/form-data` with OpenAI-style fields (`file`, `model`, `language`, `prompt`, `response_format`, `temperature`, `timestamp_granularities[]`, and similar), rewrites `model` to the configured route model from `models_operation_rules.json`; multiple routes for the same gateway model are used as an ordered fallback chain. Each route first applies its own `retry_count`/`retry_delay`, and the next route is tried only after a gateway-level `503`. Rotation is not supported, and `stream=true` explicitly returns an error. By default the route behaves as an OpenAI-compatible multipart passthrough, but `request_format: "nvidia_riva_grpc"` switches only the downstream transport: the gateway still accepts OpenAI-style multipart and forwards it to NVIDIA Riva gRPC / API Catalog under the hood. For NVIDIA routes, unsupported OpenAI client fields are ignored instead of failing the request. JSON downstream responses are returned as JSON, while plain text / srt / vtt-like responses are proxied as-is for official OpenAI SDK compatibility.
  - `POST /v1/pdf/convert`, `POST /v1/pdf/jobs`, `GET /v1/pdf/jobs/{job_id}`, `GET /v1/pdf/jobs/{job_id}/result`, `GET /v1/pdf/jobs/{job_id}/download/{artifact}` - gateway API for the PDF converter. `convert` and `jobs` accept `multipart/form-data` with `file`, `model`, and converter parameters, including `target_language` for translation before DOCX/MD assembly; `model` is used only to select the gateway route and is not forwarded downstream. Job status/result/download requests require `model` in the query string so virtual-key access is checked against the same gateway model.
  - `POST /v1/web/search` - Web search service. It accepts `model`, `query`, optional `max_results`, `num_queries`, `language`, and Tavily-compatible fields `include_raw_content`, `read_model`, `include_domains`, `exclude_domains`, `include_images`. The response keeps the current `data[]` shape; when `include_raw_content` is enabled, each result gets `raw_content` downloaded through the local `/v1/web/read` pipeline including paid adapter fallback, and when `include_images` is enabled, each result gets `images[]` from the search adapter and/or read pipeline (empty when the pipeline did not extract images). Virtual-key access is checked against the search model and, when raw content is requested, against `read_model`. The `web_search` section now contains only the service name (`gateway_model_name`) and an optional `query_model` (gateway chat LLM used to expand the user query into several search queries). The actual search always goes through four built-in adapters in a fixed order: Proxy → Tavily → Jina → Z.AI. Each adapter is enabled only if the corresponding environment variable is set (`PROXY_URL`, `TAVILY_API_KEY`, `JINA_API_KEY`, `ZAI_API_KEY`). Adapters are tried in order; the first non-empty response wins, but when domain filters are set the adapter must still have at least one result after filtering.
  - `POST /v1/web/read` - URL reading service. It accepts `model`, `url`, and optional `format`; virtual-key access is checked only against the read-service `model`. The `web_read` section contains only the service name. The gateway first attempts a keyless direct HTTP fetch: the URL and every redirect hop are resolved fresh, every resolved address must be public, and the TCP connection is pinned to the validated IP (no API key required: Medium URLs are tried through the Freedium mirror first, then the original URL; `trafilatura` preserves links/images → built-in HTML parser fallback, PDF extraction via `pdfminer`, YouTube subtitles via `youtube_transcript_api`). The direct pipeline best-effort collects images from markdown, HTML, `srcset`, OpenGraph/Twitter metadata, and JSON-LD into `images[]`, but it does not append missing images to markdown content and does not fail when only part of the images can be extracted. If the direct fetch returned empty or failed and `WEB_READ_CLOAKBROWSER_ENABLED=true`, the gateway makes one more local attempt: it renders the page with CloakBrowser and extracts Markdown with `trafilatura` while keeping image links inline in the text. Local CloakBrowser rendering is disabled by default because the browser engine performs its own navigation and does not use the direct-fetch IP-pinning layer. Only after that does it try the same four paid/external built-in adapters in order: Proxy → Tavily → Jina → Z.AI, each enabled only when its env var is set; Tavily Extract is called with `include_images` and its separate image list is appended inline into the markdown content, Jina/Z.AI retain images in markdown, and all adapter responses are additionally parsed for markdown image links.
  - `POST /v1/tavily/search` and `POST /v1/tavily/extract` - Tavily-compatible endpoints for clients that expect `results[]`, `failed_results[]`, `raw_content`, `images[]`, `response_time`, `usage`, and `request_id`. They use the same gateway models and the same search/read pipeline, but return Tavily-style shapes instead of `data[]` / `content`.
  - `POST /v1/web/research` - Research service. It accepts `model`, `query`, optional `max_results_per_lang` (alias: `max_results`), `max_articles`, `num_queries`, `language`, `output_language`, and `format`; virtual-key access is checked only against the external service `model`, then the gateway uses configured `search_model`, `read_model`, `rerank_model`, and `analysis_model` internally. If parameters are omitted, search runs across three languages at once: `ru` (2 queries), `en` (3 queries), and `zh` (3 queries), downloads search candidates, reranks the downloaded articles by title and full text, takes up to 8 best articles per language, and analyzes them in parallel. The final `output` is additionally synthesized through `analysis_model` into one coherent report in `output_language` (default `ru`). For selection, comparison, recommendation, shortlist, and due-diligence queries, the gateway automatically builds an `evidence_matrix`: `analysis_model` identifies required criteria and extracts evidence from found sources, while deterministic code passes only candidates with confirmed required criteria. When the mode applies, the response includes `evidence_matrix`; for ordinary research queries, the field is omitted.
  - `POST /v1/web/deep-research` - Deep research service through the external `gpt-researcher` package. It accepts `model`, `query`, and optional `max_words`, `breadth`, `depth`, `concurrency`, `language`, `format`, `image_generation`; `concurrency` defaults to 6, while `language` selects the final report language, not the search language. Virtual-key access is checked only against the `web_deep_research` service model, while GPT Researcher receives its search/read, LLM/embedding/image models from the `web_deep_research` section and calls the gateway as an OpenAI-compatible API.
  - For `/v1/web/research` and `/v1/web/deep-research`, the gateway explicitly watches for client disconnects after the request body is read: the active research phase is cancelled, and deep research also signals the worker thread for cooperative cancellation.
  - `/v1/responses` - OpenAI Responses-compatible API. It accepts `input` / `instructions` / `text.format` / `tools` style payloads, translates them into the internal OpenAI Chat payload used by the routing layer, and converts the downstream response back into Responses-format.
  - `/v1/messages` - Anthropic Messages compatible API. It accepts Anthropic-format input, translates it into the existing OpenAI-style payload for the routing layer, and converts the response back into Anthropic-format. It is intended to work with the official `anthropic` SDK in `messages.create(...)`, `messages.stream(...)`, and `messages.count_tokens()` mode.

  Most app-level API errors (`HTTPException`) and unexpected 500 responses keep the compatible `detail` field and additionally return a structured `error` object with `message`, `type`, `code`, and, when available, `request_id`. The same `request_id` is duplicated at the top level and in the `X-Request-ID` header so client errors can be correlated with logs without exposing internal exceptions. The low-level body-limit `413` and legacy auth `401/403` responses keep their previous `{"detail": ...}` body shape for compatibility.

  The `capabilities` field in `/v1/models` is returned only for gateway models defined in chat rules and/or operation routes. Typical values are `["chat"]`, `["embeddings"]`, `["rerank"]`, `["images"]`, `["audio_speech"]`, `["audio_transcription"]`, `["pdf_conversion"]`, `["web_search"]`, `["web_read"]`, `["web_research"]`, and `["web_deep_research"]`; if the same gateway model exists in multiple sections, the gateway returns the merged list, for example `["chat", "embeddings"]`. For gateway models, the endpoint also adds a top-level `type` field (`text`, `embeddings`, `rerank`, `image`, `audio`, `document`, `web`) and an `architecture` block with `input_modalities` / `output_modalities`; when a model has multiple capabilities at once, `type` is omitted instead of pretending there is a single task. For image models, the gateway also returns `image_operations` (`["generation"]`, `["edit"]`, or both), and for image models with an edit route `architecture.input_modalities` also includes `image`. For web models, the gateway also returns `web_operations` (`["search"]`, `["read"]`, `["research"]`, or `["deep_research"]`). For audio speech models, input is `text` and output is `audio`; for audio transcription models, input is `audio` and output is `text`; for PDF conversion models, input and output are `document`. In the Anthropic-compatible response, the `type` field stays `"model"` to match the Anthropic schema, so the normalized gateway task type is exposed as `gateway_type`, while `capabilities`, `architecture`, `image_operations`, and `web_operations` are preserved. Raw fallback-provider models keep the existing behavior and may be returned without `capabilities`, `type`, `gateway_type`, `architecture`, `image_operations`, or `web_operations`.

  
  **HOT FEATURE (Chat Completions):**: The `/v1/chat/completions` endpoint allows you to create a sequence of fallback models to be called in case of failure, with support for retries. For example, if a model response fails, the gateway can retry the same model or automatically move to the next model in the fallback sequence, and so on. The model's sequence can consist of different models and different providers. For instance, the first model in the sequence could be deepseek-chat from OpenRouter, and the gateway can be configured to fall back to gpt-4o from OpenAI in case of failure. This fallback sequence can be of any size and must be configured in the file `models_fallback_rules.json` (either manually or using the new web editor).

  **OpenAI Responses compatibility:** The `/v1/responses` endpoint is compatible with OpenAI Responses-style clients that send `input`, `instructions`, `tools`, `tool_choice`, `text.format`, `max_output_tokens`, and `stream` instead of classic `messages`. The gateway translates `input` message items and `function_call` / `function_call_output` items into the internal OpenAI Chat payload, then routes the request through the same fallback/routing mechanism used by `/v1/chat/completions`. Regular JSON responses and SSE streams are converted back into Responses-format, including `function_call` items and `response.function_call_arguments.delta` stream events for tool-calling flows.

  **Anthropic Messages compatibility:** The `/v1/messages` endpoint is compatible with the official `anthropic` SDK for `messages.create(...)`, `messages.stream(...)`, and `messages.count_tokens()`. The gateway accepts the standard `X-Api-Key: <GATEWAY_API_KEY>` and `anthropic-version` headers, and it still accepts `Authorization: Bearer <GATEWAY_API_KEY>` for direct calls. The `messages`, `system`, `max_tokens`, `temperature`, `top_p`, `top_k`, `metadata`, `stop_sequences`, `stream`, `tools`, `tool_choice`, `cache_control`, `container`, `inference_geo`, `output_config`, `service_tier`, and `thinking` fields are translated into the internal OpenAI-style payload, then routed through the same fallback/routing logic used by `/v1/chat/completions`. Assistant `text`, `thinking`, `redacted_thinking`, and `tool_use` blocks, user `text`, `image`, `document`, and `tool_result` blocks are supported on input. On output, regular JSON responses and SSE streams are converted back into Anthropic-compatible format, including `tool_use` events. For regular JSON responses, unsigned downstream reasoning is not exposed as Anthropic `thinking`, because Anthropic requires `signature` / `signature_delta` fields for those blocks. `messages.count_tokens()` returns a local gateway-side `input_tokens` estimate for the already translated payload and does not perform a separate network call to the provider.

  For chat requests with `response_format: {"type": "json_object"}`, the gateway additionally strips accidental markdown wrappers such as ```` ```json ... ``` ```` or a leading `json` prefix when the inner payload is a valid JSON object. If the model emits reasoning blocks inside `<think>...</think>` tags before the JSON payload, the gateway removes them as well. For regular JSON responses these cleanups are applied to `message.content` as a whole. For SSE streams the gateway removes `<think>...</think>` blocks incrementally from text-delta chunks, then strips the opening ` ```json`/`json` prefix and trims the trailing ` ``` ` fence from the end of the stream without buffering the full response. This applies to `/v1/chat/completions` and also to the compatible `/v1/responses` and `/v1/messages` endpoints that reuse the same routing layer.
  For regular text chat replies and SSE streams, literal `<think>...</think>` blocks can be controlled per gateway model through the fallback-rule field `strip_think_tags`. When it is `true`, the gateway removes those blocks from `message.content` and streaming `delta.content`; when it is `false`, the text is preserved as returned by the provider. For native Anthropic replies (`/v1/messages` through an `anthropic`-type provider, when the response is forwarded in Anthropic shape without the OpenAI round-trip) the stripping is applied in their own shape: from the `content` text blocks of a non-stream reply and from `content_block_delta` (text-delta) SSE events. The JSON-object sanitization above remains mandatory so the JSON contract stays valid.


## Configuration

### Edit providers and fallback rules
On desktop, the button at the top of the shared sidebar collapses it from 240px to a 72px icon-only rail and expands it again. The selected mode persists across pages; compact item names remain available as tooltips, while language, theme, and sign-out remain accessible as icons. The mobile drawer always shows full labels.

Before starting to use LLMGateway, you need to fill in your providers and models with their fallback rules by accessing the configuration page with your web browser at http://localhost:9000/v1/ui/rules-editor. Refer to the following sections to learn how to structure these rules.
Browser sign-in now goes through a dedicated `/auth/login` page. Opening `/` without authentication redirects to `/auth/login`; opening `/` with valid authentication redirects to `/v1/ui/overview` — the landing page after login for both roles. After a successful login the server creates a persistent signed HttpOnly cookie with `SameSite=Lax`; by default the cookie also gets the `Secure` flag, and local HTTP environments must explicitly opt out with `SESSION_COOKIE_SECURE=false`. The browser UI uses that cookie instead of `sessionStorage`; the cookie survives browser restarts and gateway restarts until you explicitly log out or change `GATEWAY_API_KEY`. The cookie is signed with a separate random secret that the gateway creates on first start and keeps in `db/session_secret.json` (the directory is set by `GATEWAY_DB_DIR`); `GATEWAY_API_KEY` itself never enters the signature, so an issued cookie cannot be used to grind the master key offline. The file sits next to the rest of the state in `db/` and survives restarts along with it; changing `GATEWAY_API_KEY` reissues the secret automatically, which is exactly how a key change logs everyone out. On the first start after an upgrade the secret does not exist yet, so previously issued cookies become invalid once. SDK clients and direct API calls can still use the header `Authorization: Bearer <GATEWAY_API_KEY>`, and `/v1/messages` also accepts `X-Api-Key: <GATEWAY_API_KEY>` together with the standard `anthropic-version` header used by the official SDK. The gateway parses the Bearer header strictly as `scheme + token` separated by whitespace, accepts `Bearer` case-insensitively, returns `401` for malformed headers or missing authentication, and returns `403` only when the header format is correct but the token value is wrong. The only paths available without an API key are `/health`, `/auth/login`, `/` as a redirect entrypoint, and static assets under `/static/`; `POST /auth/login` still participates in the brute-force block described below. HTML pages without a valid cookie or Bearer token are redirected to login, while API endpoints without valid authentication continue to return `401/403`. Unregistered routes still return the standard `404`.

The shared navigation is built after one current-role request. On screens 1024px and wider it renders as a permanent left sidebar about 240px wide: the master role sees twelve UI sections (**Overview**, **Activity**, **Docs**, **Analytics (legacy)**, **Quota**, **Rules Editor**, **Playground**, **API Keys**, **Rejections**, **Translator Check**, **Pricing**, **Free LLM Catalog**), while a virtual key sees only five: **Overview**, **Activity**, **Docs**, **Quota**, and **Free LLM Catalog**. At the bottom of the sidebar, above the identity, a shared settings row contains a language pictogram, the locale selector, and the light/dark/system theme button; there is no separate panel in the top-right corner. Below it the sidebar shows the current role (`Master`/`User`) and, for a virtual key, the key's name from `/auth/me`, plus a logout button that calls `POST /auth/logout` and returns to the login page. On screens narrower than 1024px the same panel becomes a slide-in drawer: a 44×44 hamburger button in the header opens it over the page content; it closes on `Escape`, a click outside the panel, or a click on any nav item, and focus is trapped inside the drawer while it is open. The section list supports `↑`/`↓` arrow keys as well as `Home`/`End`. Russian or English can be selected on the login page before authentication. After the locale is ready, the theme-toggle label and product version rerender in the selected language without another `/auth/me` or `/health` request. `POST /auth/login` errors also include a stable `error.code` (`auth_invalid_api_key`, `auth_invalid_request`, `auth_rate_limited`, or `auth_unavailable`) while preserving the previous HTTP status and top-level `detail`. Tab groups support arrow keys, `Home`/`End`, and manual activation with `Enter`/`Space`.
The entire first-party web UI supports full strict i18n for Russian and English. The registry supports up to 20 locales; every added locale must provide the complete catalog set, and there is no automatic language fallback. Switching languages rerenders already loaded state without another API request or loss of form data, the active tab, focus, or scroll position. Unknown server messages and technical details are not translated and are shown separately as raw text.
The single public readiness endpoint is available as `GET` and `HEAD /health`. `GET` returns a local report: status `200` means the runtime, configuration, accounting/writer, and mandatory SQLite stores are ready; before readiness or on failure the endpoint returns `503`. `HEAD` returns the same status code and headers without a body. The check does not call LLM providers, so it is suitable for Docker and load balancers. Responses always disable caching and add `X-LLMGateway-Build-Version` with the canonical `__version__` value; `X-LLMGateway-Build-Sha`, `X-LLMGateway-Build-Date`, and `X-LLMGateway-Build-Ref` are added only when `LLMGATEWAY_BUILD_SHA`, `LLMGATEWAY_BUILD_DATE`, and `LLMGATEWAY_BUILD_REF`, respectively, are set.
The Configuration Editor is entity-first: a left panel always lists every entity (Providers, Fallback, Operation, Fusion, Router, Pricing) with a search box on top, and picking an entity in that panel switches the same set of tabs. The `Preview Changes`/`Save Configuration` buttons and the unsaved-changes status are pinned to a sticky footer at the bottom, so they stay visible without scrolling even when the open entity holds many cards.
The Configuration Editor includes the main tabs:
- **Fallback Rules**: A structured form for managing chat model fallback chains.
- **Embeddings**: A structured form for managing `/v1/embeddings` routes.
- **Rerank**: A structured form for managing `/v1/rerank` routes.
- **Images**: A structured form for managing `/v1/images`, `/v1/images/generations`, and `/v1/images/edits` routes.
- **Audio**: A structured form for managing `/v1/audio/speech` and `/v1/audio/transcriptions` routes; the `pdf_conversions` section is preserved by the structured API but is still edited through JSON.
- **Web**: A structured form for managing `/v1/web/search`, `/v1/web/read`, `/v1/web/research`, and `/v1/web/deep-research` services.
- **Model Rules**: A raw JSON editor for `models_model_rules.json`, used for model alias/prefix/exclude/pool policy.
- **OpenRouter Free**: A read-only ranking of free OpenRouter text models. Lite eval checks instruction following, tool-call JSON, Python code with unit tests, parameterized arithmetic, and factual QA. The tab is shown only if the official `openrouter` provider and its API key are configured; it does not automatically change routing rules. The `Run Full Eval` button triggers a background full re-evaluation (`POST /v1/openrouter/free-models/run`) without waiting for the next scheduled cycle.
- **Fallback Eval**: A read-only manual eval for unique fallback targets from `models_fallback_rules.json`. It runs by button, shows health/latency/lite-eval score, and does not save configuration.
- **Providers**: A structured form for managing providers: name, `baseUrl`, `apikey`, API type (`openai`/`anthropic`), optional `proxy`, and the JSON `models`, `auth`, `routing`, and `upstream_key_pools` fields.
- **Fusion**: A structured form for managing Fusion ensembles (`models_fusion_rules.json`): the gateway model name, the main model, an optional judge model, and a panel of 1–8 models. Each role is selected as a `provider/model` pair; for each you can additionally set `temperature`, `max_completion_tokens`, and `reasoning` (JSON). You can optionally set a reserve list `reserve` (also up to 8 models) that refills a failed panel slot once. An optional **Web tools** block gives panel models `web_search`/`web_fetch` tools with tool-call and iteration budgets.
- **Router**: A structured form for managing Router models (`models_router_rules.json`): the gateway model name, a selector model from already configured chat fallback models, and an explicit target list. Each target is selected as either a full gateway chat model or a specific entry in that model's fallback chain.

The Fallback Rules, Embeddings, Rerank, Images, Audio, Web, Providers, Fusion, and Router tabs use a structured form instead of raw JSON. The Model Rules tab edits raw JSON with validation before write. The OpenRouter Free and Fallback Eval tabs only show current snapshots and do not save configuration.
The Fallback Rules tab includes read-only `Preview Changes` and `Suggest Eval Order` actions: they show the diff or the eval-based suggested order, but do not save or reorder routes automatically. Each chat rule can opt in to `dynamic_penalty`, allowing the gateway to temporarily prefer less-penalized keys/routes after transient upstream errors.
The model list for a selected provider is loaded lazily, sorted alphabetically with natural numeric order, and cached for 15 minutes. In the Fallback Rules tab, manual model entry is not allowed. In the Embeddings, Rerank, Images, and Audio tabs, you can either choose a model from the list or enter it manually if needed.
**Configured fallback-model scoring** lives in the **Fallback Eval** tab. It runs only when you press `Run Eval`, reads every `fallback_models` entry and `context_overflow_fallback` from `models_fallback_rules.json`, collapses duplicates into unique `provider/model` pairs, and shows which gateway models reference each target. If the official `openrouter` provider and API key are configured, the eval loads `{openrouter.baseUrl}/models` once, takes the OpenRouter card by model basename without the provider prefix and without the suffix after `:`, for example `openai/gpt-oss-120b:free` -> `gpt-oss-120b`, and copies `metadataScore`, context, max completion tokens, pricing, and support flags from it. If a target has no OpenRouter metadata match but other fallback targets in the same run have known metadata scores, the missing `metadataScore` is estimated as the median of those known scores; detailed OpenRouter fields such as context and pricing stay empty for that target. For OpenAI-compatible providers, the eval calls `{baseUrl}/chat/completions` directly and respects `custom_headers` except security headers, `custom_body_params`, `providers_order`, provider-specific proxy clients, and round-robin API-key selection when `apikey` contains comma-separated keys. For providers with `type: "anthropic"`, the eval sends native requests to `{baseUrl}/v1/messages` with `x-api-key` and `anthropic-version`, then converts the response into the internal shape used by the same checks.
The Fallback Eval final score is `round((metadataScore + healthScore + latencyScore - instabilityPenalty) * 0.8 + liteEvalScore * 1.6)` — eval tests are weighted twice as much as the other signals so that a model with a large context but failing tests does not outrank a model whose skills actually work; if OpenRouter is not configured or no known metadata score exists in the run, `metadataScore` remains 0. `healthScore` comes from a small `Reply with exactly OK` probe, `latencyScore` comes from that probe latency, and `liteEvalScore` covers instruction following, tool-call JSON, a Python unit test, parameterized arithmetic, and factual QA. Lite eval runs for every available fallback target without a quantity cap; unavailable models get a health/error status and do not run lite eval. The result is kept as a runtime snapshot, is exposed via `GET /v1/fallback-model-evals`, is started via `POST /v1/fallback-model-evals/run`, and never changes routing rules automatically.

### Model Eval Tests

OpenRouter Free and Fallback Eval use the same text-only checks. Evaluation starts with a health probe: the model receives a short `Reply with exactly OK` request with a small response limit. If the response contains `OK`, the target gets `healthStatus=passed` and 400 health points; if the HTTP call succeeds but the response does not match the expected text, the target gets `healthStatus=imperfect` and 250 health points. HTTP 429 is treated as a temporary rate limit: the target gets `healthStatus=http_429`, 100 health points, and `instabilityPenalty=25`, but lite eval is not started for it so the gateway does not amplify the rate limit. Lite eval runs only for `passed` and `imperfect`; other HTTP errors, missing providers, or network errors get `not_evaluated`.

Lite eval is worth up to 750 points and contains five tasks:

- `instruction_following_lite` — up to 200 points. The model must return exactly 4 lines: `STATUS: READY`, a line where `ROUTER` appears exactly twice, JSON `{"mode":"eval","count":3}`, and `DONE`, with no markdown or extra text.
- `tool_call_lite` — up to 200 points. The model receives a list of available tools and a request to create a bug ticket. The checker expects JSON with the `create_ticket` tool, high priority, assignee `Ana`, due date `2026-05-12`, and the correct title meaning.
- `code_unit_lite` — up to 200 points. The model must return JSON containing Python code for `sum_even_squares(nums: list[int]) -> int`. The code is checked for presence, a safe AST without imports or dangerous constructs, and then run through local unit tests.
- `symbolic_math_lite` — up to 100 points. The model solves a parameterized arithmetic problem about remaining notebook pages and must return only the integer answer.
- `simpleqa_lite` — up to 50 points. The model answers factual QA: who wrote `The Left Hand of Darkness`. The correct answer `Ursula K. Le Guin` gives 50 points, explicit `UNKNOWN` gives 20 points, and a wrong answer gives 0.

In the Images tab, advanced options now expose `request_format` / `response_format` plus `request_mapping` / `response_mapping`. This keeps OpenAI format as the default, lets you enable `openai_images_multipart` for JSON-to-multipart image edit routes, or configure vendor-specific image routes such as NVIDIA image generation/edit endpoints without provider-specific hardcoding.
In the Audio tab, you can set `provider`, `model`, `target_path`, `retry_count`, `retry_delay`, `custom_headers`, and `custom_body_params` for TTS routes and OpenAI-compatible transcription routes. TTS routes also expose `voices_target_path`, which is used by `/v1/audio/voices`; the default downstream speech path is `"/audio/speech"`, and the voice catalog uses `"/voices"` when the field is left empty. Transcription routes additionally expose `request_format`; their default `target_path` is `"/audio/transcriptions"`.
For NVIDIA downstreams, set `request_format: "nvidia_riva_grpc"`. The public gateway API still remains OpenAI-compatible at `/v1/audio/transcriptions`, but the downstream call is sent to NVIDIA Riva gRPC. For NVIDIA API Catalog (`build.nvidia.com`), the gateway uses the official gRPC endpoint `grpc.nvcf.nvidia.com:443`; such routes must pass `function-id` through `custom_headers`. NVIDIA-specific settings such as `language`, `enable_automatic_punctuation`, `verbatim_transcripts`, `max_alternatives`, `custom_configuration`, `boosted_lm_words`, `boosted_lm_score`, and `speaker_diarization` can be configured through `custom_body_params`. If the client omits `language`, the gateway tries to derive a safe provider-driven default from upstream capabilities: it currently auto-fills only aggregate codes such as `multi` or `indic` when the NVIDIA API Catalog function itself advertises them; otherwise the endpoint returns an explicit error with the supported language codes. For API Catalog the gateway also chooses the transport mode from upstream capabilities: if the function advertises offline inference, the gateway uses the offline RPC; otherwise it keeps the streaming RPC. If a NVIDIA route receives non-WAV audio, the gateway converts it to WAV locally via `ffmpeg`, then extracts raw PCM and sends it to Riva with `RecognitionConfig.encoding = LINEAR_PCM`.
If one of the already saved fallback models is no longer returned by its provider, the Fallback Rules tab still opens the rule and highlights that specific model as unavailable. Saving remains blocked until you pick an available model from the current provider list.
The Fallback Rules tab uses the Structured API `/v1/config/models-rules/structured`. Each gateway model there also exposes a `Strip <think> tags from replies` toggle that controls literal `<think>...</think>` stripping for regular and streaming chat responses. The Embeddings, Rerank, Images, Audio, and Web tabs use the Structured API `/v1/config/model-operations/structured`, which reads and saves the `embeddings`, `rerank`, `images_generations`, `images_edits`, `audio_speech`, `audio_transcriptions`, `pdf_conversions`, `web_search`, `web_read`, `web_research`, and `web_deep_research` sections without restarting the server. The UI edits Embeddings, Rerank, Images, Audio Speech, Audio Transcriptions, and Web sections; `pdf_conversions` is preserved during those saves but remains JSON-only. The Providers tab uses the Structured API `/v1/config/providers/structured`; on save it still validates `${VAR}` references, rejects provider deletes/renames that would break fallback or operation rules, refreshes runtime provider config, and clears the provider model-list cache. The Router tab uses the Structured API `/v1/config/router-rules/structured`: selector and targets are selected only from already configured chat gateway models and their fallback chains, without raw JSON input. The Web tab configures four services: search, page reading, research, and deep research with GPT Researcher, embedding, and image-generation models. When saving one operation tab, the editor sends the complete payload based on the loaded operation-document revision and supplies that revision through `If-Match`; it does not perform a pre-save GET. If another client has already saved a newer revision, the server returns `409`, the form keeps the local edits, and the latest document is loaded only after explicit user confirmation. `retry_delay` is preserved as a fractional number when you enter a value such as `0.5`.
After loading saved data, Embeddings, Rerank, Images, Audio, Web, and Providers cards are shown as collapsed accordions by default; newly added cards are expanded automatically for editing.

Two kinds of `409` are distinguished. When another client has already saved a newer revision through the API, that is an ordinary revision conflict: the notice offers to reload the document, and the save succeeds after the reload. When any of the six configuration files changed on disk outside the UI (a manual edit, a deploy, a script) — including the one you are editing — the server answers `409` with the code `config_sources_out_of_sync` and lists the drifted files: saving any document is blocked, because validation depends on all six files at once. Reloading the page does not help here — the `GET` serves the same in-memory snapshot. In that case the conflict block in **Rules Editor** and **Pricing** offers "Reload configuration from disk", and its button calls `POST /v1/config/resync` (master-only): the gateway re-reads all six files, validates them, and publishes a new runtime generation without writing anything to disk. If the files on disk are invalid, the resync answers `400` and the current configuration keeps running. After a successful resync, saving from the editor works again — no service restart needed.

When you run the gateway with `docker-compose.yml`, all six configuration files live in one writable `./config` directory mounted at `/app/config`. A shared directory is required for the atomic `rename` used by web-editor saves. Before the first start, run `sudo python3 scripts/init_docker_config.py --source-dir . --target-dir ./config`: the initializer copies existing root configs byte-for-byte, never overwrites a target, and prepares the directory for the fixed container UID/GID `10001:10001`. Startup fails explicitly before the application starts if the directory is missing, is not writable/searchable, or a default-file publication remains uncertain. Compose runs the container with a read-only root filesystem: the host config/DB directories and project-scoped `gateway_outputs` named volume persist data, while `logs` and `/tmp` remain ephemeral. The one-shot `outputs-init` service prepares the volume for runtime UID/GID `10001:10001`; illustrations survive restart, force-recreate, and ordinary `docker compose down`/`up`.

![Config example](./images/config-example.png)

### Fusion ensembles (`models_fusion_rules.json`)

A Fusion model is a regular gateway model that internally runs an ensemble of several models. From the outside, the client calls it like any other model via `/v1/chat/completions` (`model: "llmgateway/fusion-quality"`), and the gateway:

1. **Fan-out**: sends the original messages to every panel model (`panel`, 1 to 8 models) in parallel.
2. **Judge**: passes the panel answers to a judge model (`judge_model`; if omitted, `main_model` is used), which returns a strict JSON breakdown with `agreements`, `disputes`, `per_model_insights`, and `blind_spots`. The judge call is auxiliary: if it fails, the main request still completes.
3. **Fan-in**: the main model (`main_model`) compiles one final answer from the panel answers and the judge breakdown instead of selecting one answer as the winner.
4. **Reserve (optional)**: if a panel member's call fails (except for accounting errors, which are never refilled from reserve), the freed slot is refilled in one additional wave by the next candidate from `reserve` — a list with the same shape as `panel`. Each reserve candidate is used at most once, in list order. A slot for which a reserve refill was attempted is marked in the response as `filled_from_reserve: true`, regardless of whether the refill call itself succeeded.

The response is returned as a standard `chat.completion` and additionally carries a `fusion` field with the judge breakdown and (when details are enabled) the full panel answers; with `include_details_default: true`, a "Fusion panel analysis" section is appended to the answer text. `usage` aggregates the tokens of all internal calls.

Each role (`main_model`, `judge_model`, and every `panel` entry) is specified as a `provider`/`model` pair — just like a fallback-rule target — and optionally accepts `temperature` (0–2), `max_completion_tokens`, and `reasoning` (an arbitrary JSON object). To put one of the gateway's own models in the panel, register the gateway as a regular provider (a loopback to its own `baseUrl`) and reference its models.

**Base context.** Every member (panel, judge, main model) gets the current date and time in the server's local timezone prepended to its system prompt (e.g. `Current date and time: 2026-06-14T15:30 MSK (Saturday).`), so models reason correctly about "today / now / latest version / deadline". When `web_tools` are enabled, *panel* models also receive a hint: if the answer depends on recent information, call `web_search` first.

**Web tools for the panel (optional).** When the Fusion config sets a `web_tools` block, every *panel* model runs in an agentic loop before answering: it is given a `web_search` tool (and, when `read_model` is set, a `web_fetch` tool), whose calls are executed inside the gateway via the same web-search/web-read operations (`models_operation_rules.json`). The judge and main models never receive tools. Fields: `search_model` (required — the gateway web-search model name), `read_model` (optional — the gateway web-read model name, enables `web_fetch`), `max_tool_calls` (tool-call budget per panel model, default 6), `max_iterations` (max loop turns per model, default 4), `max_results` (results per search, default 5). When a budget is exhausted, the panel model is forced to answer in text using the information already gathered. If the referenced web model is not configured in `models_operation_rules.json`, the tool returns an explicit error (no silent degradation).

The panel (`panel`) and reserve (`reserve`) are independent lists: `panel` is required and holds 1 to 8 models, `reserve` is optional and is also capped at 8 models; there is no combined limit across both lists.

Constraints: Fusion models do not support `stream: true` (a `400` is returned) and cannot be nested — calling a Fusion model from within another Fusion panel is rejected (`400`). The file is set via the `FUSION_RULES_FILENAME` environment variable (default `models_fusion_rules.json`). A missing optional file means "no Fusion models"; an existing file must contain a valid JSON array, using `[]` for an empty configuration. The tab's structured API is `GET`/`POST /v1/config/fusion-rules/structured`.

```json
[
  {
    "gateway_model_name": "llmgateway/fusion-quality",
    "panel": [
      {"provider": "openrouter", "model": "deepseek/deepseek-chat"},
      {"provider": "openai", "model": "gpt-4o", "temperature": 0.4},
      {"provider": "anthropic", "model": "claude-sonnet-4-6", "reasoning": {"effort": "medium"}}
    ],
    "reserve": [
      {"provider": "openrouter", "model": "qwen/qwen3-max"}
    ],
    "main_model": {"provider": "openai", "model": "gpt-4o"},
    "judge_model": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    "include_details_default": true,
    "web_tools": {
      "search_model": "llmgateway/web-search",
      "read_model": "llmgateway/web-read",
      "max_tool_calls": 6,
      "max_iterations": 4,
      "max_results": 5
    }
  }
]
```

### Router models (`models_router_rules.json`)

A Router model is a regular chat gateway model that asks a separate selector model to choose one target from a predefined list before the main request is dispatched. `selector_model` must be a gateway chat model from `models_fallback_rules.json`, so the selector itself keeps the normal fallback chain. The internal selector call is not checked against a virtual key's `allowed_models`, but external access to the Router model and the final selected gateway target are checked like normal chat models.

Targets have two forms:

- `{"type": "gateway_model", "model": "llmgateway/high"}` — Router forwards the original request to this gateway model and uses its full fallback chain.
- `{"type": "fallback_entry", "gateway_model": "llmgateway/coder", "index": 1}` — Router starts the referenced gateway model's fallback chain at `index` and continues with the following entries on failure. Rotation is disabled for this forced-start call so the selector-chosen entry is really tried first.

If the selector returns invalid JSON or a `candidate_id` that is not in the allowlist, the gateway returns an explicit `502` error and does not choose a backup target silently. Router models do not support `stream: true` (a `400` is returned). Configuration is available in the **Router** tab of the configuration editor through the `GET`/`POST /v1/config/router-rules/structured` Structured API. The file is set via `ROUTER_RULES_FILENAME` (default `models_router_rules.json`). A missing optional file means "no Router models"; an existing file must contain a valid JSON array, using `[]` for an empty configuration.

```json
[
  {
    "gateway_model_name": "llmgateway/router",
    "selector_model": "llmgateway/light_model",
    "targets": [
      {"type": "gateway_model", "model": "llmgateway/high"},
      {"type": "fallback_entry", "gateway_model": "llmgateway/coder", "index": 1}
    ]
  }
]
```

## Usage Statistics

### Overview
The page http://localhost:9000/v1/ui/overview is the landing page after login for both roles (`/` redirects here). It shows: a gateway status banner (`GET /health` — phase `Starting`/`Ready`/`Stopping`/`Failed` and version; the separate "Status unavailable" text is shown only when `/health` is truly unreachable, e.g. a network error, not for a plain `503` response that still carries a valid report body — that case renders as "Degraded"), 6 priority KPIs (requests, cost, p95 duration, active requests, fallback errors, rejections), one trend chart (requests or cost over time), and a top-models-by-request-count table. Data comes from the already existing `GET /v1/api/analytics-dashboard` with the `range` filter (`24h`/`7d`/`30d`/`90d`); no dedicated API was added for this page. If there is no data for the selected range, the page shows an explicit empty state with a reason instead of the content.

### Activity
The page http://localhost:9000/v1/ui/activity is the gateway's unified request journal, available to both roles. The list is paginated through `GET /v1/api/usage-records`; filters: status (`All` — 2xx/active, `Rejected (4xx)` — master only, backed by `GET /v1/admin/rejections`, `Server errors (5xx)` — see below), `Since` (UTC, applies only to the Rejected view), Provider, Key ID (master only), and Request ID — Provider/Key ID/Request ID filter only the already-loaded page rather than issuing a new request. Row columns: time, request ID (copy on click), key (master only), operation, model, provider, status, latency, cost. The **Server errors (5xx)** view is a placeholder: the gateway does not currently persist a separate journal of upstream 5xx errors, so the page shows a "No data source for 5xx" disclosure immediately instead of calling the API. Clicking a row opens a side drawer with the request's details: Request ID with a **Copy** button (copies via `navigator.clipboard.writeText` with a `document.execCommand('copy')` fallback and a visible "Copied!" confirmation), time in UTC and in the local time zone, status (Success/Running/Rejected, color-coded), virtual key — id and name (master only, the name is lazily resolved via `GET /v1/admin/api-keys`), operation, the requested model and the actually resolved model, provider, prompt/completion tokens, cost, and duration. The drawer is a reusable component (`static/request-drawer.js` + `static/request-drawer.css`) with a focus trap and `Escape`-to-close (built on the existing `createDialog`); focus returns to the row once the drawer closes. The **Fallback timeline** section and the **TTFT** field currently show an honest disclosure instead of data: `GET /v1/api/usage-records` does not yet return `ttft_ms` or a per-request fallback attempt log, so this data is not backfilled or fabricated. All filters and the current page are serialized into the query string (`?status=...&provider=...&page=...`), so reloading the tab or sharing the link restores the same view; if a request fails after a successful load (network/5xx), the table is not cleared — it keeps showing the last loaded snapshot (cached in `sessionStorage`) with a "Stale · N min ago" badge next to the summary.

### Analytics (legacy)
The page http://localhost:9000/v1/ui/usage-stats (master only) shows detailed usage statistics; the nav label reads "Analytics (legacy)" because **Overview** and **Activity** above are now the primary landing page and request journal, and this page is kept as a more detailed (but temporary) alternative.

The **Analytics Dashboard** tab shows a full dashboard for the selected period: KPIs for requests, tokens, cost, `cost_saved`, average duration, p50/p95 request duration, average/p50/p95 TTFT (time to first token — the delay until the first response chunk, tracked for streaming responses), active requests, fallback errors, and access rejections; a token trend chart; a provider-cost chart; and tables for resolved targets, `X-Title`, reliability, virtual keys, and recent activity. The dashboard is powered by `GET /v1/api/analytics-dashboard` and supports `range` (`24h`, `7d`, `14d`, `30d`, `90d`, `180d`, `365d`, `12m`), `bucket` (`hour`, `day`, `week`, `month`), `api_key_scope=unattributed`, `operation`, `gateway_model`, `provider`, `model`, `x_title`, `upstream_key_fingerprint`, `usage_source`, and `estimated` filters. Historical usage rows with incomplete `gateway_model`/`provider`/`model` are not dropped from this dashboard; they are grouped as `unknown`.

The dashboard also exposes extended analytics:
- **Tokens/s** — generation throughput (completion tokens per second over rows with a known duration) in the KPIs, in the resolved-target table, and in the per-provider breakdowns.
- **Pin Honored** — the share of chat requests served without a single failed upstream attempt (based on the `fallback_depth` field stored on each usage row). It is computed only over requests whose fallback depth is known; operations without an attempt trail are excluded from the denominator.
- **Provider Performance** — a dedicated table with per-provider generation throughput, average TTFT, p95 duration, and attempt/error/success-rate statistics merged from fallback events; the `Fallback Errors` and `Success Rate` columns are also added to the resolved-target table.
- **All-time counters** (master only) — requests, tokens, and cost since the first recorded event. They live in the dedicated `usage_hourly`/`usage_lifetime` tables, are updated in the same transaction as the usage row, are backfilled once from existing rows on the first start after the upgrade, and survive the raw-record auto-cleanup. The series chart without active filters is built from the hourly aggregates, so long ranges (`365d`, `12m`) show history beyond the raw-row retention; with any active filter the series falls back to raw rows.
- **Client IP and User-Agent** — every chat usage row stores the peer IP and the client `User-Agent` header (truncated to 256 characters); both columns are visible in Recent Activity.

**Note**: The usage statistics page now includes successful `/v1/chat/completions`, `/v1/responses`, `/v1/messages` (Anthropic), `/v1/embeddings`, `/v1/rerank`, `/v1/images`, `/v1/images/generations`, `/v1/images/edits`, `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/pdf/convert`, `/v1/pdf/jobs`, `/v1/web/search`, `/v1/web/read`, `/v1/web/research`, and `/v1/web/deep-research` calls. For embeddings, rerank, image, audio, PDF, and web operations, the gateway also writes a statistics record when the downstream response does not include a `usage` block; in that case the token fields remain zero, but the request count and route metadata are still visible. Token and cost values can still remain empty or zero for providers whose APIs do not return token usage or return it in an incompatible shape. The local `/v1/messages/count_tokens` endpoint is excluded from usage statistics.
Usage statistics are collected independently from `LOG_CHAT_ENABLED`. The `LOG_CHAT_ENABLED` flag controls only whether detailed chat log files are written to the `logs/` directory.
In usage statistics, the first `Gateway Model` column shows the original gateway model, the adjacent `Resolved Model` column shows the full resolved target in the `provider/model` format, for example `devbox/zai.glm-5`, and the `Operation` column shows the request type (`chat` for OpenAI `/v1/chat/completions` and `/v1/responses`, `messages` for Anthropic `/v1/messages`, `embeddings`, `rerank`, `images_generation`, `images_edit`, `audio_speech`, `audio_transcription`, `pdf_conversion`, `web_search`, `web_read`, `web_research`, `web_deep_research`). Historical records created before this change may still show `N/A` in `Gateway Model` or `Operation` if `gateway_model` and `operation` were not stored for them yet.
For fallback errors with `response_format: {"type": "json_object"}`, the gateway now also appends `system_preview` to `error_message` — the first 100 characters of the first `system` message. This makes it easier to diagnose cases where the downstream provider expects an explicit JSON mention in the instructions while the full prompt remains hidden.
In the **Latest Usage Records** tab, the `Timestamp` column shows the request date and time in the same format as **Fallback Chains**: `YYYY-MM-DDTHH:MM:SS`, without microseconds. The `X-Title` column shows the incoming client HTTP header of the same name, for example a value sent through OpenAI SDK `extra_headers`. Running requests are shown at the top of the table with a colored row highlight; after completion they are replaced by normal usage records. For a running chat request (`/chat/completions`, `/responses`, `/messages`) the `Prompt Tokens` and `Total Tokens` columns show a preliminary request-token estimate (tiktoken, marked with `≈` in the `Estimated` column); exact provider-reported values appear once the request completes. The running row also fills in `Gateway Model`, `Operation`, `Resolved Model`, `X-Title` and the `Upstream Key Fingerprint` of the current attempt as they become known.
The same table also includes a `Duration (ms)` column with the full gateway-side request processing time in milliseconds. Older records created before this field existed may show `N/A`.
Usage statistics are cleaned automatically once a day, deleting records older than 90 days.

### Virtual API keys
The page http://localhost:9000/v1/ui/api-keys (available only under the master `GATEWAY_API_KEY`) lets you create, edit and delete virtual API keys prefixed with `lgk_`. Each key can define:

- `budget_usd` — USD spend cap. `null` = no limit. Only finite non-negative values are accepted; `NaN`, `Inf`, and negative values are rejected with `400` before anything is written to the DB. Once `spent_usd >= budget_usd`, the gateway replies `429` for any LLM request made with that key. For every exactly matched billable route, the durable accounting service atomically reserves budget and RPM/TPM before upstream dispatch. A successful terminal owner commits usage, outbox, and actual spend exactly once; errors, cancellation, and requests without terminal usage release the reservation. This closes the concurrent/streaming race without duplicate endpoint checks.
- `rpm` / `tpm` — 60-second sliding window limits kept in-process. Exceeding either returns `429` with a message indicating which limit was hit.
- `allowed_models` — whitelist of gateway model names. Empty list = any model allowed; non-empty list = only the listed models. This list also filters `/v1/models` and `GET /v1/models/{id}`: a virtual key only sees the models it is allowed to use, and requesting a single disallowed model returns `403`. The master `GATEWAY_API_KEY` is not subject to this restriction and always sees the full catalog. The Web UI exposes an explicit **"All configured models"** / **"Only selected models"** toggle instead of relying on the implicit meaning of an empty list.
- `disabled` — instantly blocks the key without deleting it. Disabled keys return a uniform `403` with `detail="api_key_disabled"` for both Bearer/`X-Api-Key` and UI-session callers (no login redirect for sessions), so API and browser behavior stay symmetric.
- `metadata` — free-form JSON object, handy for storing owner/team/tag. The serialized value is capped at 16 KB; exceeding that limit returns `400` and the change is not persisted.
- `budget_period` — periodic reset of the `spent_usd` counter. Accepted values:
  - `none` (default) — cumulative mode; `spent_usd` only grows (historical behaviour, unchanged).
  - `daily` — `spent_usd` is reset to 0 every day at 00:00 UTC.
  - `monthly` — `spent_usd` is reset to 0 on the first day of each month at 00:00 UTC.
  The next reset moment is stored in the `budget_reset_at` field (ISO-8601 UTC; computed automatically, `null` when `budget_period` is `none`). The actual reset is performed by a background task that checks the boundary roughly once per minute, so it fires within one minute of the scheduled UTC moment. Changing `budget_period` automatically recalculates `budget_reset_at`. Configurable via the **Budget reset period** dropdown in the «API Keys» Web UI and through the Admin API.

Virtual-key holders that log in via the UI see **Docs**, **Usage Statistics** filtered to their own requests, and **Quota**; **Fallback Analytics**, **Rules Editor**, **API Keys**, and all other master-only sections are hidden, and the corresponding master-only endpoints return `403`. The `api_key_id` query parameter on usage endpoints does not restrict the master key: the master always sees the global usage set, while virtual-key sessions are always forced to their own key. In **Analytics Dashboard**, the master can still inspect unattributed rows with `api_key_scope=unattributed`. Spend tracking is linked to the request's `api_key_id` and is written through the same shared `WriteBatcher` used by the usage table.

On `/v1/ui/api-keys`, the master admin manages virtual keys and their limits. The key table loads independently from the model catalog: `/v1/models` is requested only when Create/Edit is opened for the first time and is cached until the page is reloaded. If loading fails, the dialog shows an explicit retry action, while saved `allowed_models` entries that are no longer present in the catalog remain editable and are not lost on save. The key table can be filtered by name/ID with the search field and sorted by name, creation date, or last used. Master usage statistics are available as a global overview on `/v1/ui/usage-stats`; the master per-key usage filter is not applied.

CRUD over virtual keys is available through the master-only Admin API:
- `GET /v1/admin/api-keys` — list every key together with its `api_key`, `spent_usd`, `last_used_at`, `budget_period`, `budget_reset_at`.
- `POST /v1/admin/api-keys` — create a new key; accepts the optional `budget_period` field (`none`/`daily`/`monthly`); the response contains `api_key`, `budget_period`, and `budget_reset_at`. In the Web UI, the key list stays masked, while the edit modal shows the full key to the master admin.
- `PATCH /v1/admin/api-keys/{id}` — partial update, including `reset_spent: true` to reset the spend counter and `budget_period` to change the reset schedule; the response contains the updated `budget_period` and `budget_reset_at`. Supports an optional `If-Match` header carrying the `ETag` returned by `GET /v1/admin/api-keys/{id}`: a mismatch returns `412 Precondition Failed` and applies no change (guards against lost edits when the same key is edited in two tabs at once); omitting `If-Match` keeps the prior behavior unchanged — fully backward compatible.
- `DELETE /v1/admin/api-keys/{id}` — remove a key.
- `GET /v1/admin/rejections` — list gateway governance rejections (master key only). Query parameters:
  - `api_key_id` (int, optional) — filter by virtual key ID.
  - `category` (string, optional) — filter by rejection category: `auth_invalid`, `key_disabled`, `model_not_allowed`, `budget_exhausted`, `rate_limited`, `master_only`, `unauthorized`, `ip_blocked`.
  - `since` (ISO 8601 string, optional) — return only records at or after this timestamp.
  - `limit` (int, default 50, max 200) — maximum records to return.
  - `offset` (int, default 0) — pagination offset (used by the UI page).
  Response: `{"items": [...], "total": <count matching the filter>}`. Each item contains: `id`, `timestamp`, `request_id`, `api_key_id`, `path`, `method`, `client_ip`, `status_code`, `category`, `reason`, `auth_source`, `x_title`.

### Rejections Audit UI
The page http://localhost:9000/v1/ui/rejections (master only, under `GATEWAY_API_KEY`) shows the gateway's governance-rejection log on top of `GET /v1/admin/rejections`. The table displays the time, category (color badge), HTTP status, method, path, key ID, incoming `X-Title`, client IP, and reason. It offers filters by category, key ID, and start time (`since`), a page-size selector (25/50/100/200), and Newer/Older pagination via `offset`. Its purpose is to surface key brute-forcing and probing, diagnose why a client is blocked (`model_not_allowed`/`budget_exhausted`/`rate_limited`/`ip_blocked`), and audit the gateway's own access decisions independently of provider logs. The **Rejections** item in the top navigation is visible only to the master key. On mobile widths (<768px) the table shows only Time/Category/Status — the remaining fields (method, path, key, X-Title, client IP, reason) open in a bottom sheet when you tap a row. Filters and the page number are synced to the query string, and if a request fails after a successful load, the page keeps showing the previous snapshot (persisted in `sessionStorage`) with a "Stale · N min ago" badge instead of clearing the table.

### IP blocking on key brute-forcing (brute-force protection)
The gateway automatically and temporarily blocks any client IP that brute-forces API keys. It counts **consecutive** failed authentications (the `auth_invalid` category — an invalid or missing key); a successful authentication from the same IP resets the counter. RPM/budget limits, disabled keys, and other rejections do not count toward this counter.

By default, after **5** consecutive failures an IP is blocked for **20 minutes**. While the block is active, any request from that IP to protected endpoints gets `429 Too Many Requests` with a `Retry-After` header before the key is even checked; `POST /auth/login` also returns `429` to block further browser sign-in attempts. Public GET paths (`/health`, `GET /auth/login`, `/static/*`) remain available. The moment a block is triggered is recorded once in the Rejections Audit under the `ip_blocked` category (subsequent blocked requests are not logged, to avoid flooding the audit) and in the application log. Block state is held in-memory and is cleared on gateway restart.

Settings (environment variables):
- `IP_BLOCK_ENABLED` (default `true`) — enable/disable the mechanism.
- `IP_BLOCK_MAX_FAILURES` (default `5`) — number of consecutive failures before a block.
- `IP_BLOCK_DURATION_MINUTES` (default `20`) — block duration in minutes.

### Gateway Documentation UI
The page http://localhost:9000/v1/ui/docs is available to any authenticated user and documents how to call gateway models through the public APIs. It shows the current gateway models from `models_fallback_rules.json`, `models_router_rules.json`, and `models_operation_rules.json`, groups them by capabilities (`chat`, `embeddings`, `rerank`, `images`, `audio_speech`, `audio_transcription`, `pdf_conversion`, `web_*`), and does not make network calls to downstream providers. For a virtual key with `allowed_models`, the catalog shows only the gateway models allowed for that key.

At the top of the page, a **quickstart** block with four cards covers the first steps: where to get an API key (`GATEWAY_API_KEY` or a virtual key via the **API Keys** page), a first request example with `curl`, a Python SDK example (an `openai` client pointed at the gateway `base_url`), and a link to the usage page.

The page gives focused call-parameter details for `/v1/rerank`, `/v1/embeddings`, `/v1/images*`, `/v1/audio/speech`, `/v1/audio/voices`, `/v1/audio/transcriptions`, `/v1/pdf/*`, and the web services `/v1/web/search`, `/v1/web/read`, `/v1/tavily/search`, `/v1/tavily/extract`, `/v1/web/research`, `/v1/web/deep-research`.
All eleven code examples now have a copy button (the icon in the block's top-right corner briefly switches to "Copied!") and use the current page origin; the section table of contents becomes a sticky right-hand column (`position: sticky`) on screens 1024px and wider and highlights the current section while scrolling, staying a plain link list at the top of the page on narrower screens; when Docs is opened over public HTTP, the UI warns that API keys and requests are not protected in transit.
The separate **Free-tier Providers** tab on the same page loads Markdown for the current language from `static/locales/<locale>/free-tier-providers.md` and builds a safe DOM without `innerHTML`. `static/locales/registry.json` defines the supported languages; a missing localized file fails visibly without a language fallback. The legacy `GET /v1/ui/docs/free-tier-providers.md` route and `examples/free-tier-providers.md` remain available only for compatibility.

### Playground (admin testing)
The page http://localhost:9000/v1/ui/playground (available only under the master `GATEWAY_API_KEY`) provides an admin UI for manually exercising operation endpoints. The old `/v1/ui/web-playground` path remains as a compatible alias. Playground is split into **Chat**, **Web**, **Speech to Text**, **Text to Speech**, **Image Generation**, **Image Editing**, and **PDF Conversion** sections; model dropdowns are populated from the configured `models_operation_rules.json` sections `web_search`, `web_read`, `web_research`, `web_deep_research`, `audio_speech`, `audio_transcriptions`, `images_generations`, `images_edits`, and `pdf_conversions`.

Action buttons use the shared primary/secondary states in both light and dark themes. At widths up to 480 px, field groups fill the form without the false 160-pixel flex height, preserving vertical rhythm across every section.

The **Chat** section uses a chat-first layout: the message feed comes first, the send form is pinned below it, and optional parameters (`top_p`, `max_tokens`) are tucked into a collapsible "Advanced settings" block. Regular (non-Fusion) models are sent with `stream: true`, and the assistant bubble fills in progressively as SSE `data:` chunks arrive; while streaming, `Send` is disabled and a **Cancel** button appears next to it, aborting the request via `AbortController` and marking the bubble "(cancelled)" while keeping whatever partial text already rendered. In the message field, `Enter` sends the chat and `Shift+Enter` inserts a newline. The **Chat** section's model selector lists regular fallback models, Fusion ensembles (`models_fusion_rules.json`), and Router models (`models_router_rules.json`); Fusion models are marked with a `· Fusion` suffix and, since the gateway rejects `stream: true` for Fusion, are always sent as a regular buffered request. Selecting a Fusion model reveals an **include_details** checkbox (on by default) that is sent in the request as `fusion.include_details`. When the response contains a `fusion` field, an expandable "Fusion ensemble" block is rendered below the assistant text with main/judge attribution, the judge's structured breakdown (agreements/disputes/per-model insights/blind spots), and the panel answers (panel content is shown only when `include_details` is enabled; failed panel members are flagged separately). Router models are called like regular chat models, and selection details are returned in the JSON response's `router` field.

The **Web** section keeps the existing forms for `/v1/web/search`, `/v1/web/read`, `/v1/tavily/search`, `/v1/tavily/extract`, `/v1/web/research`, and `/v1/web/deep-research`. Search/Read forms support `include_raw_content`, `read_model`, `include_domains`, `exclude_domains`, `include_images`, and Tavily-compatible search/extract parameters. Deep research exposes a **Generate illustrations** checkbox — when it is enabled, the gateway runs the request through GPT Researcher with image generation turned on.

The other sections call existing OpenAI-compatible gateway routes. **Text to Speech** loads the voice list for the selected model from `/v1/audio/voices?model=...`, lets the admin choose a language, sends JSON to `/v1/audio/speech`, renders an audio player, and adds a download link for the audio file. **Speech to Text** sends `multipart/form-data` to `/v1/audio/transcriptions`, supports language/response-format selection, and adds a download link for the result. **Image Generation** and **Image Editing** expose image-size selection, show returned `url`/`b64_json` images in a gallery, and add image download links; `url` may be either an HTTP(S) link or `data:image/...;base64,...`. Editing sends up to 4 `image[]` files and an optional `mask` to `/v1/images/edits`. **PDF Conversion** starts an asynchronous job through `/v1/pdf/jobs`, sends the real converter-service parameters (`output=both|docx|md`, `language` such as `rus+eng`, `target_language` for translation, `math_ocr_provider`, `formulas_max_pages`, `max_pages`, `password`, and `ocr_preprocess_save`), shows progress/ETA, and builds artifact download links through `/v1/pdf/jobs/{job_id}/download/{artifact}?model=...` after completion.

Generated illustrations are returned in the `/v1/web/deep-research` response as a dedicated `images[]` field (each entry `{url, prompt, alt_text}`). When the provider returns image bytes, the gateway atomically publishes a local PNG at `<GATEWAY_OUTPUTS_DIR>/images/{research_id}/image_*.png` and serves it at `/outputs/images/...`; startup fails closed unless write, rename, and delete work in that directory. Access to `/outputs/images` goes through the same auth layer as the rest of the gateway endpoints; no separate `api_key` is required when the same session that received the URL fetches the file. Only these locally published PNGs are pruned automatically **10 days** after creation by the daily background task. Under Docker Compose they live in the project-scoped `gateway_outputs` volume and survive restart/recreate and ordinary `down`/`up`; `docker compose down -v` permanently removes the volume. When the provider returns an external URL, the gateway passes it through without making a local copy, and the provider controls its lifetime.

### Unified UI Theme System
All UI pages (`rules-editor`, `usage-stats`, `api-keys`, `gateway-docs`, `web-playground`, `quota`) share a single `static/theme.js` and `static/theme.css`. `theme.js` manages light/dark/system mode switching via the `llmgateway:theme` `localStorage` key, automatically migrates legacy keys (`darkMode`, `theme`), and dispatches `llmgateway:theme-changed` events. CSS custom properties (`--bg`, `--bg-elevated`, `--text`, `--border`, `--accent`, etc.) are declared once in `theme.css`.

### Translator Check

A developer tool for inspecting how the gateway transforms request bodies between OpenAI and Anthropic formats, without sending a real request to any provider.

- **URL**: `/v1/ui/translator-debug`
- **Access**: master only (GATEWAY_API_KEY). Virtual keys receive 403.
- **What it shows**: a 7-step transformation chain — from the original client request to the final response in the client's format, including intermediate OpenAI representations and a mock provider response.
- **How to use**: select source/target formats, paste the request JSON, optionally paste a mock provider response, and click "Run translation". Each step is displayed in a read-only block with a copy button.
- **Presets**: "OpenAI sample", "Anthropic sample", and "Tool-use sample" buttons populate the input field with minimal valid examples.

API endpoint: `POST /v1/admin/translator/debug` — accepts `{source_format, target_format, request_body, mock_provider_response?}` and returns an array of 7 steps.

### Pricing UI

The page `/v1/ui/pricing` is accessible to the master key only and lets you manage per-model token pricing and calculate request costs.

- **URL**: `/v1/ui/pricing`
- **Access**: master only (GATEWAY_API_KEY); virtual keys receive 403.

**Editor section** shows a table (provider, model, input rate, output rate) with in-cell editing. «Add Row» appends a new row, «Delete» removes it, «Save Changes» writes the full list to `providers.json` via a PUT request. Modified rows are highlighted. Writes are atomic; if the file contained JSON5 comments they are backed up before overwrite. On mobile widths (<768px) the table shows only Provider/Model — the rate columns are hidden, and a row's «›» button opens a bottom sheet to edit that row's input/output rate.

The table shows not only models explicitly listed in `providers.json`, but every model actually used by the running configuration (`models_fallback_rules.json`, `models_operation_rules.json`, `models_fusion_rules.json`, `models_router_rules.json`) that has no price yet. Each row carries a `source` status:
- `configured` — the price is explicitly set in `providers.json` (an editable row, same as before);
- `openrouter_autofill` — an `openrouter` model found in the OpenRouter catalog (`/api/v1/models`); the price is auto-filled and written to `providers.json` (shown with an «Auto-filled from OpenRouter» badge, and the row stays editable);
- `operation_default` — the model is only used by a service operation (embeddings/rerank/images/audio/web/pdf) and has no price yet; the row is read-only, shows the default `$0.10 per request` for operation models (see below), and a «Set manual price» button to switch it to manual editing;
- `upstream_only` — a plain (chat) model with no known price, whose cost is tracked only from upstream `usage.cost`; the row is likewise read-only until «Set manual price» is clicked;
- `awaiting_openrouter_catalog` — an `openrouter` model whose pricing catalog has not been loaded yet by the `OpenRouterFreeModelsService`; once the first successful sync completes, the row is automatically reclassified into `openrouter_autofill` or one of the statuses above.

Autofill runs on every `GET /v1/admin/pricing` request: whenever prices are found for used `openrouter` models, they are atomically (CAS, revision-checked) appended to `providers.json`, without removing already-configured prices for other providers.

**Calculator section** — select a model from the drop-down, enter prompt and completion token counts, click «Calculate». The server returns the exact cost in USD with 6 decimal places along with the current model rates.

Rates are stored under the `models` key of each provider in `providers.json`:

```json
[
  {
    "openai": {
      "baseUrl": "https://api.openai.com/v1",
      "apikey": "${APIKEY_OPENAI}",
      "models": {
        "gpt-4o": { "input_rate": 2.5, "output_rate": 10.0 },
        "gpt-4o-mini": { "input_rate": 0.15, "output_rate": 0.60 }
      }
    }
  }
]
```

Units are **USD per 1 million tokens**. After saving, the configuration is hot-reloaded without restarting the server. If the upstream response omits `usage.cost` or returns `usage.cost: null`, the gateway calculates `cost` locally from the current model rates and the actual prompt/completion token counts; if the model price is missing or invalid, the default `0.1 USD` per successful request is used. A valid upstream `usage.cost`, including `0`, remains the source of truth.

API endpoints (master-only):
- `GET /v1/admin/pricing` — list (provider, model) pairs that are either explicitly configured in `providers.json` or actually used by the configuration; each entry has a `source` (`configured`/`openrouter_autofill`/`operation_default`/`upstream_only`/`awaiting_openrouter_catalog`) and, for unpriced operation models, `default_cost_per_request`.
- `PUT /v1/admin/pricing` — update the full pricing list; atomically written to `providers.json`.
- `POST /v1/admin/pricing/calculate` — `{provider, model, prompt_tokens, completion_tokens}` → `{cost_usd, input_rate, output_rate}`.

### Provider Topology

The statistics page includes a **Topology** tab — an interactive **routing map** powered by [@xyflow/react](https://reactflow.dev/). It shows not just providers but how gateway models route through their fallback chains, with live metrics layered on top. The graph reads left to right in three tiers:

- A central **LLM Gateway** node on the left.
- **Gateway models** — one node per rule in `models_fallback_rules.json`; the caption shows the chain length and active-request count.
- **Providers** (right) — the union of configured providers and every provider referenced by a fallback target. Border colour: green — `ok`, red — `error`, grey — `invalid`. A provider referenced by a rule but missing from the config is marked "not configured" — a signal that the rule points nowhere.

Edges reflect the routes:

- `gateway → model` — aliases; animated when the model has active traffic.
- `model → provider` — fallback steps labelled with the priority number (1, 2, 3…); animated and emphasised when active requests flow through the route.
- A dashed orange edge labelled `ctx` — the `context_overflow_fallback` target.

Interaction:

- **Clicking a gateway model** highlights its chain and dims the rest; clicking the background or another node resets it.
- **Detail panel** (click a node): for a provider — health / configured / active / penalty / models; for a gateway model — the full chain with `retry_count`/`retry_delay`, the active flags (`rotate_models`, `dynamic_penalty`, `strip_think_tags`, `compress_tool_results`), `max_total_attempts`, and the `context_overflow` target.
- **Auto-refresh** (on by default) reloads every 5 seconds without resetting the graph's zoom/pan or the selected chain; a manual Refresh button sits next to it. Polling pauses while the tab is hidden.
- **Adaptive height**: the map area fills the spare viewport height (never shrinking below the previous ~520px minimum), so taller screens reveal more of the graph without leaving empty space below it.

Active requests are attributed down to the exact `model → provider` edge (each active record carries both `gateway_model` and `provider`). Virtual-key callers see only their own requests in the `active_requests` field. Data is cached in memory for 5 seconds.

API endpoint: `GET /v1/topology` (auth required; accessible to all keys).

React and ReactFlow ship as a locally hosted ESM bundle at `static/vendor/topology.bundle.mjs` (~300 KB minified), accompanied by `static/vendor/topology.bundle.css` (~15 KB). The CSS is layout-critical — without it nodes fall back to `position: static` and stack vertically instead of being placed on the layered left-to-right layout. The browser never reaches out to any external CDN, so the UI keeps working inside firewalled networks. If the bundle fails to load, an inline error with a Retry button is shown.

Rebuild the bundle (only required when React or ReactFlow versions change):

```bash
cd frontend/topology
npm install
npm run build  # writes static/vendor/topology.bundle.{mjs,css}
```

The bundle source lives in `frontend/topology/entry.mjs`; versions are pinned in `frontend/topology/package.json`. Both compiled `topology.bundle.mjs` and `topology.bundle.css` are committed; `node_modules/` is not (see `.gitignore`).

### Quota Dashboard
The page at http://localhost:9000/v1/ui/quota is available to all authenticated users and shows the current rate-limit window utilisation in real time.

- **URL**: `/v1/ui/quota`
- **Access**: master sees all virtual keys; a virtual key sees only its own card.
- **What it shows**: for each virtual key — current RPM (requests/min) and TPM (tokens/min) in the rolling 60-second window, progress bars with colour coding (green < 60%, yellow 60–85%, red > 85%), a countdown to window reset, remaining budget, and the number of fallback events in the last 24 hours.
- **Polling**: data refreshes every 5 seconds; the countdown updates client-side every second without contacting the server.
- **Data states**: an initial load failure exposes an explicit retry action. If a refresh fails after a successful load, the visible data is preserved and marked stale until a later poll succeeds. Switching between Russian and English rerenders the stored snapshot without another quota request.

API endpoint: `GET /v1/api/quota/keys` — returns a JSON array with a per-key snapshot; cached for 5 seconds.

### Upstream subscription quotas

The gateway can display remaining quotas from upstream providers (GitHub Copilot, Gemini CLI, Antigravity) directly in the Quota Dashboard.

**Supported providers:**
- `github_copilot` — paid and free response formats from the GitHub Copilot API (`quota_snapshots` / `monthly_quotas` + `limited_user_quotas`).
- `gemini_cli` — checks Google Cloud availability via Cloud Resource Manager.
- `antigravity` — stub provider, returns an empty snapshot.

**Required environment variables:**
- `GITHUB_COPILOT_TOKEN` (or any other name specified via `token_env`) — Copilot access token.
- `GEMINI_CLI_TOKEN` — Google Cloud access token for Gemini CLI.

**`providers.json` configuration:**
```json
[
  {
    "github_copilot": {
      "baseUrl": "https://api.github.com",
      "apikey": "${APIKEY_X}",
      "subscription_quota": {
        "kind": "github_copilot",
        "token_env": "GITHUB_COPILOT_TOKEN"
      }
    }
  }
]
```

The `subscription_quota` block is optional. Providers without it are completely ignored by the fetcher — backward compatibility is preserved.

**Endpoint:** `GET /v1/admin/upstream-quotas` — accessible to the master key only (403 for virtual keys). Returns a list of `SubscriptionQuotaSnapshot` objects with fields:
- `provider`, `kind`, `plan`, `reset_date` — meta information.
- `categories` — dictionary `{name: {used, total, remaining, unlimited}}`.
- `error` — `null` on success, otherwise an error message string.

Data is cached for 60 seconds (errors for min(30, ttl/2)). In the Quota Dashboard, the upstream section is only shown to master users and is hidden on 403.

### Free LLM Catalog
The page at http://localhost:9000/v1/ui/free-models is available to both roles (master and virtual keys) and shows the catalog of free-tier LLM models from [freellmapi.co](https://freellmapi.co/models), filtered to a reasonable monthly token budget.

- **URL**: `/v1/ui/free-models`
- **Source**: `GET https://api.freellmapi.co/v1/latest` — an open JSON feed, no HTML scraping; the upstream catalog itself is regenerated roughly once a day.
- **Filter criterion**: only models whose source-reported lower bound of the monthly token budget (e.g. `~50-100M` → 50M) is at least the configured threshold — 30,000,000 tokens/month by default, configurable via `FREE_LLM_CATALOG_MIN_MONTHLY_TOKENS` — are shown. Models with an unrecognisable budget (`free · 40 RPM`, `promo (trial)`, etc.) are excluded. A provider is shown as long as at least one of its models clears the filter.
- **What it shows**: providers grouped and sorted by their highest-budget model; for each model — display name, `modelId` (monospace, with a copy button), the source's raw budget string, only the limits that are actually populated (RPM/RPD/TPM/TPD), the context window, vision/tools badges, and an expandable "connection quirks" section.
- **Data refresh**: a background task fetches the catalog on startup and then every 24 hours; until the first successful fetch, it retries every 5 minutes. Data is kept in memory and the page itself does not poll — it issues a single request on load plus a manual retry action.
- **States**: before any successful fetch, the page shows "data unavailable" together with the last error text; if a snapshot exists but the most recent refresh failed, the previously shown data stays on screen marked "as of \<date\>" alongside the error text.
- **Mobile width**: the «Retry» button and the `modelId` copy button have a minimum height of 44px (touch target).
- **Search and Configured/External split**: a search box filters models by name, `modelId`, and provider name (200ms debounce); an "All / Configured / External" toggle appears once the list of configured models can be fetched from `GET /v1/config/providers/structured` (master role only — virtual keys get a 403 on that endpoint and the catalog stays a single unsplit list, as before). On success, the catalog splits into "Configured (N)" and "External (M)" sections with a badge on each model.

API endpoint: `GET /v1/api/free-models` — returns `{updatedAt, sourceVersion, sourceGeneratedAt, lastError, minMonthlyTokens, providers}`; available to both roles.

### Fallback Analytics
The statistics page includes a **Fallback Analytics** tab that provides detailed information about provider transitions (fallback events). The tab contains two sub-tabs:

- **Summary** — aggregated failure statistics by period. Shows which provider/model failed, with what error (429, timeout, etc.), how many times, and average attempt duration.
- **Chains** — detailed fallback chains for individual requests. Only requests with 2+ attempts are shown. The chain header displays the incoming `X-Title`. Each chain can be expanded to see the full sequence: which providers were tried, the reason for each failure, an expanded error message for generic upstream failures and transport failures (`ReadTimeout`, `ConnectTimeout`, `ConnectError`) with a short payload summary, and how long each attempt took (with a visual duration bar).
- **Upstream Analytics** — fast aggregates by `provider/model/upstream_key_fingerprint`: attempts, successes, errors, success rate, average duration, and max duration. This helps distinguish a single upstream-key problem from a model-wide or client-side issue.

Fallback event and upstream analytics data are stored in the `fallback_events` table in the same database (`tokens_usage.db`) and are subject to the same automatic cleanup (90 days). Fallback events and successful usage records store the incoming `X-Title`; usage records also store `upstream_key_fingerprint`, so token/cost records can be correlated with the selected upstream key or client title without exposing the secret.

API endpoints:
- `GET /v1/api/fallback-stats/{period}` — aggregated failure statistics (period: hour/day/week/month)
- `GET /v1/api/fallback-records?limit=25&offset=0` — fallback chains with pagination
- `GET /v1/api/upstream-status` — read-only runtime status for upstream keys (`healthy`/`invalid`/`error`, last check, last error, cooldown/quota, `observed_limits`, `cooldown_escalation_level`)
- `POST /v1/api/upstream-health/run` — manual health probe for upstream keys referenced by current fallback rules
- `GET /v1/api/upstream-stats/{period}` — success/latency/error aggregates by provider/model/key (period: hour/day/week/month)

Responses for `GET /v1/api/usage-stats/{period}`, `GET /v1/api/fallback-stats/{period}`, and `GET /v1/api/upstream-stats/{period}` are cached in memory for 30 seconds. This protects SQLite from repeated full-scan aggregations when the dashboard is open in several tabs at once, each polling the same endpoint. The 30-second lag is invisible in the UI, which already shows a snapshot from the previous poll anyway.

![Config example](./images/statistics-example-01.png)

![Config example](./images/statistics-example-02.png)


## .env file

Create a `.env` file from the example .env.example:
```bash
cp .env.example .env
```

Process environment variables always take precedence over values from `.env`. When `LLMGATEWAY_ENV_FILE` is unset, a direct launch reads the project `.env` only for variables missing from the process environment. A file explicitly selected through `LLMGATEWAY_ENV_FILE` must be an absolute regular non-symlink file; a read/parse error or duplicate key aborts startup without partially applying values. In production systemd, the generated `runtime.env` sets this selector while user secrets remain in a separate `gateway.env`.

 ### **.env** configuration example:
 ```
# This gateway must have its own API key that clients must use to access it
# Use it in http header as "Authorization: Bearer <ThisGatewayApiKey>"
GATEWAY_API_KEY=<ThisGatewayApiKey>

# Maximum number of log files to keep (older files will be deleted)
LOG_FILE_LIMIT=15

# Maximum non-accounting SQLite writes accepted by the WriteBatcher queue
WRITE_BATCHER_QUEUE_MAXSIZE=4096

# Process-wide byte budget, per-observer queue, and single SSE event limit
STREAM_OBSERVATION_BUFFER_MAX_BYTES=33554432
STREAM_CHUNK_QUEUE_MAXSIZE=32
STREAM_EVENT_MAX_BYTES=1048576

# Per-response and process-wide budgets for observed JSON responses
JSON_RESPONSE_MAX_BYTES=8388608
JSON_RESPONSE_INFLIGHT_MAX_BYTES=33554432

# Multipart file limits and process-wide upload-processing admission
AUDIO_UPLOAD_MAX_BYTES=33554432
IMAGE_UPLOAD_MAX_FILE_BYTES=16777216
IMAGE_UPLOAD_MAX_TOTAL_BYTES=67108864
PDF_UPLOAD_MAX_BYTES=83886080
UPLOAD_INFLIGHT_MAX_BYTES=268435456
UPLOAD_ADMISSION_TIMEOUT_SECONDS=5
AUDIO_DECODED_MAX_BYTES=100663296

# Isolated deep-research processes and maximum admission wait
DEEP_RESEARCH_PROCESS_CAPACITY=1
DEEP_RESEARCH_ADMISSION_TIMEOUT_SECONDS=5

# Enable/disable logging of chat messages to the /logs folder (true/false). 
# Useful for debugging
LOG_CHAT_ENABLED=false

# Temporary debug mode for fallback investigations.
# When true, failed fallback warning logs keep full request messages instead of redacting them.
# Enable it only temporarily because full prompt/history will be written to logs.
LOG_FALLBACK_FULL_MESSAGES=false

# Optional X-Routed-Via and X-Fallback-Attempts diagnostic headers
ROUTING_DIAGNOSTIC_HEADERS=false

# The default fallback provider to use when the model received is not recognized 
# by this gateway in the fallback rules.
FALLBACK_PROVIDER=openrouter

# The keys of your providers. Used in the providers.json
# Fill the ones you want to use or add more if you need
APIKEY_OPENROUTER=<your_openrouter_api_key>
APIKEY_REQUESTY=<your_requesty_api_key>
APIKEY_OPENAI=<your_openai_api_key>
APIKEY_NEBIUS=<your_nebius_api_key>
APIKEY_TOGETHER=<your_together_api_key>
APIKEY_KLUSTERAI=<your_klusterai_api_key>
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GATEWAY_API_KEY` | Fixed API key clients must use to access this gateway | *required* |
| `GATEWAY_OUTPUTS_DIR` | Absolute generated-output root; the writer, authenticated StaticFiles mount, and retention use its `images` child | `<project>/outputs` (`/app/outputs` in Docker) |
| `LLMGATEWAY_ENV_FILE` | Absolute path to the single explicit environment file. Do not set this variable inside that file; production systemd writes it to `runtime.env`. | project `.env` when unset |
| `LLMGATEWAY_LOG_DIR` | Single absolute root used by gateway/chat log writers and cleanup | `<project>/logs` (`/var/log/llm-gateway` under systemd) |
| `LLMGATEWAY_EXPECTED_PRODUCT_VERSION` | Required expected canonical product version for Docker builds; `docker-compose.yml` reads it from the environment and fails when it is absent or mismatched | *required for Docker build* |
| `LLMGATEWAY_BUILD_SHA` | Git SHA or other revision id, exposed in health response headers and OCI label | empty |
| `LLMGATEWAY_BUILD_DATE` | Build timestamp, exposed in health response headers and OCI label | empty |
| `LLMGATEWAY_BUILD_REF` | Git ref or release name, exposed in health response headers and OCI label | empty |
| `LOG_FILE_LIMIT` | Maximum number of chat log files to keep | `15` |
| `WRITE_BATCHER_QUEUE_MAXSIZE` | Total limit for accepted but unfinished non-accounting SQLite writes in `WriteBatcher`, including pre-start, cross-thread handoff, queued, and in-flight work. It must be a positive integer; overload fails explicitly without silently dropping the write. | `4096` |
| `STREAM_OBSERVATION_BUFFER_MAX_BYTES` | Process-wide byte budget for data retained by streaming-response observation. Producers wait for capacity, applying upstream backpressure. | `33554432` |
| `STREAM_CHUNK_QUEUE_MAXSIZE` | Maximum chunk items across the process-wide stream capacity and in one stream observer queue. | `32` |
| `STREAM_EVENT_MAX_BYTES` | Maximum size of one assembled SSE event; overflow fails stream observation explicitly. | `1048576` |
| `JSON_RESPONSE_MAX_BYTES` | Maximum size of one JSON response retained for protocol validation and accounting finalization. | `8388608` |
| `JSON_RESPONSE_INFLIGHT_MAX_BYTES` | Process-wide byte budget for concurrently retained JSON responses. Exhaustion fails explicitly before response headers are sent. | `33554432` |
| `AUDIO_UPLOAD_MAX_BYTES` | Maximum size of one multipart audio file. | `33554432` |
| `IMAGE_UPLOAD_MAX_FILE_BYTES` | Maximum size of one multipart image or mask file. | `16777216` |
| `IMAGE_UPLOAD_MAX_TOTAL_BYTES` | Maximum aggregate file size for one image-edit request. | `67108864` |
| `PDF_UPLOAD_MAX_BYTES` | Maximum size of one multipart PDF file. | `83886080` |
| `UPLOAD_INFLIGHT_MAX_BYTES` | Process-wide byte budget for upload processing after route selection. Admission uses strict FIFO order. | `268435456` |
| `UPLOAD_ADMISSION_TIMEOUT_SECONDS` | Maximum wait for process-wide upload capacity; expiry returns an explicit `503`. | `5` |
| `AUDIO_DECODED_MAX_BYTES` | Maximum normalized WAV/PCM size for NVIDIA Riva audio routes. | `100663296` |
| `DEEP_RESEARCH_PROCESS_CAPACITY` | Maximum concurrently running isolated GPT Researcher child processes. | `1` |
| `DEEP_RESEARCH_ADMISSION_TIMEOUT_SECONDS` | Maximum wait for a deep-research process slot; expiry fails explicitly. | `5` |
| `LOG_CHAT_ENABLED` | Enable detailed chat log files in the `logs/` directory. It does not affect usage-statistics collection. | `false` |
| `LOG_FALLBACK_FULL_MESSAGES` | Temporary debug flag for fallback investigations. When enabled, warning logs keep `messages` for failed fallback attempts instead of redacting them. Use it only temporarily because full prompt/history will be written to logs. | `false` |
| `ROUTING_DIAGNOSTIC_HEADERS` | Enables diagnostic response headers `X-Routed-Via` and `X-Fallback-Attempts`. Disabled by default to avoid exposing downstream routing details to clients. | `false` |
| `MAX_REQUEST_BODY_BYTES` | Maximum HTTP request body size. The gateway checks `Content-Length` and also counts bytes for streaming/chunked bodies; when exceeded it returns `413 Request body too large`. | `104857600` |
| `SESSION_COOKIE_SECURE` | Add the `Secure` flag to the browser session cookie. Keep `true` for HTTPS; set `false` only for local HTTP/test environments. | `true` |
| `WEB_READ_CLOAKBROWSER_ENABLED` | Enables local CloakBrowser rendering in `/v1/web/read` after direct fetch fails. Disabled by default: the browser engine performs its own navigation and does not use the direct-fetch IP-pinning layer. | `false` |
| `WEB_READ_CLOAKBROWSER_NO_SANDBOX` | Adds `--no-sandbox` to CloakBrowser launch arguments. Enable only when the runtime environment explicitly requires it. | `false` |
| `CORS_ALLOW_ORIGINS` | Optional comma-separated list of allowed cross-origin origins. When unset, CORS stays disabled and same-origin UI still works. | disabled |
| `FREE_LLM_CATALOG_ENABLED` | Enables the background task that fetches the free-tier LLM catalog from freellmapi.co for the `/v1/ui/free-models` page. When `false`, the background loop makes no network requests and the API returns an empty snapshot. | `true` |
| `CAPABILITY_AUTOFILL_ENABLED` | Enables the background task that autofills capability fields (`supports_vision`/`supports_tools`/`context_window`) in fallback rules from provider `/models` catalogs. When `false`, the background loop makes no network requests; editor-save triggered autofill keeps working. | `true` |
| `FREE_LLM_CATALOG_MIN_MONTHLY_TOKENS` | Filter threshold for the free-tier LLM catalog: the lower bound of a model's source-reported monthly token budget below which it is excluded from the page. | `30000000` |
| `FALLBACK_PROVIDER` | Default provider name shared by `/v1/models` and `/v1/chat/completions` when no rule matches. Startup fails if this provider is missing from `providers.json`. | `openrouter` |
| `VERIFY_MODELS_ON_STARTUP` | Startup model-availability check mode: `off` disables it, `warn` logs warnings for missing models, `strict` aborts startup. | `warn` |
| `PROXY_URL` | Base URL of your own Proxy service used by the built-in `/v1/web/search` and `/v1/web/read` adapters (`{PROXY_URL}/zai/search` and `{PROXY_URL}/zai/read`). When unset, the Proxy adapter is skipped. | disabled |
| `TAVILY_API_KEY` | API key for Tavily, used by the built-in `/v1/web/search` (Tavily Search) and `/v1/web/read` (Tavily Extract) adapters. You can provide multiple keys separated by commas; on each Tavily call the gateway selects the next non-empty key by round-robin. When unset, the Tavily adapter is skipped. | disabled |
| `JINA_API_KEY` | API key for Jina AI, used by the built-in `/v1/web/search` (Jina Search) and `/v1/web/read` (Jina Reader at `r.jina.ai`) adapters. You can provide multiple keys separated by commas; on each Jina call the gateway selects the next non-empty key by round-robin. When unset, the Jina adapter is skipped. | disabled |
| `ZAI_API_KEY` | API key for Z.AI, used by the built-in `/v1/web/search` and `/v1/web/read` adapters. The adapter talks to Z.AI's Streamable HTTP MCP servers — `web_search_prime` for search and `web_reader` for page extraction (requires the GLM Coding Plan subscription). The search region (`cn`/`us`) is auto-detected from the query. You can provide multiple keys separated by commas; on each Z.AI call the gateway selects the next non-empty key by round-robin. When unset, the Z.AI adapter is skipped. | disabled |
| `APIKEY_PROVIDERNAME` | API key for a specific provider (e.g., `APIKEY_OPENROUTER`). In `providers.json`, reference it explicitly as `${APIKEY_PROVIDERNAME}`. You can provide multiple keys separated by commas; on each downstream provider call the gateway selects the next non-empty key by round-robin. OpenRouter Free scoring and manual Fallback Eval use the same rule and select a key separately for each provider request. | *required for providers in providers.json* |

## Providers Example (`providers.json`)
Here, you must define your providers. By default each provider is treated as OpenAI-compatible, but the optional `type` field lets you declare the native wire format: `"openai"` (default) or `"anthropic"`. Anthropic providers are called at `{baseUrl}/v1/messages` with `x-api-key` and `anthropic-version` headers, and their model catalog is fetched from `{baseUrl}/v1/models`. The gateway converts requests and responses in both directions automatically: an OpenAI client that falls back to an Anthropic provider still receives an OpenAI-shaped response (including streaming SSE chunks and `tool_calls`), and an Anthropic client that falls back to an OpenAI provider still receives a native Anthropic response. For Anthropic providers the gateway injects `max_tokens=32768` when the client request does not set it explicitly. For OpenAI providers the `type` field can be omitted — existing `providers.json` files keep working unchanged.
The `apikey` and `proxy` fields are treated as literals by default. To read a value from an environment variable, use the explicit `${VAR_NAME}` syntax, for example `"apikey": "${APIKEY_OPENROUTER}"`. The value `"APIKEY_OPENROUTER"` without `${...}` is not resolved from the environment; if such an environment variable exists, the gateway logs a warning and still uses the string as a literal. Known template values such as `your-secure-api-key` and `your-openrouter-api-key` are rejected for `GATEWAY_API_KEY` and provider `apikey`: the app fails startup or rejects the config instead of running with a demo secret.
Provider names must be unique in `providers.json`. Duplicate provider entries are rejected during startup, and the web editor returns `400` instead of silently overwriting the previous entry.
When saving `providers.json` through the UI, the gateway switches to strict `${VAR}` resolution: if the payload references an environment variable that is not present in the process or `.env`, the gateway responds with `400` and the message `env var X referenced but missing for provider 'Y' field 'Z'` and does not write the file. Saving also re-validates `provider` references from the already loaded `models_fallback_rules.json` and `models_operation_rules.json`: if renaming or removing a provider leaves dangling references in those rules, the gateway responds with `400` listing the broken routes instead of silently writing an inconsistent state.
A provider proxy must be a complete URL with an `http`, `https`, or `socks5` scheme and a non-empty host. Startup and hot reload build proxy clients through one factory with identical timeouts, connection limits, and HTTP/2. Before either the raw or structured provider config is written, the gateway resolves and validates every proxy and builds the complete client candidate; on failure the file and active runtime remain unchanged, and the response does not expose proxy URLs or credentials.
When `apikey` contains several comma-separated keys, chat fallback chooses the concrete upstream key with per-key cooldown and upstream-limit awareness. Limits are configured under `models.<model>.upstream_limits` as `rpm`, `rpd`, `tpm`, and `tpd`; this is a separate upstream-provider ledger and does not replace client virtual-key limits.
The editor UI (`Providers` → `Advanced options` → `Upstream Limits per Model`) exposes these limits as explicit `Model`, `RPM`, `RPD`, `TPM`, `TPD` fields per model, with an `ⓘ` tooltip next to each variable explaining what it does. The `Models Metadata (JSON)` textarea stays available for any other free-form metadata (e.g. `pricing`); on save the gateway merges the structural limits back into that JSON, so the two representations never go out of sync.
A separate `available_models` field declares an explicit list of the provider's model ids as a JSON array of strings (e.g. `"available_models": ["deepseek/deepseek-r1:free", "qwen/qwen3-max"]`). When such a list is set, the gateway uses it verbatim and does not query the provider's `/models` endpoint — useful for proxies without a working `/models` endpoint. The list is applied everywhere a provider model catalog is needed: the model picker in the fallback-rules editor, rule validation on save, and the public `/v1/models` endpoint (when the provider is selected as `FALLBACK_PROVIDER`). When `available_models` is absent, behaviour is unchanged — the catalog is fetched from the provider API. This field is independent of `models`, so an explicit list can be combined with `models` metadata/`upstream_limits`. In the editor UI (`Providers` → `Advanced options`) the list is entered in the `Available Models` field — one id per line (commas are also accepted).
The `routing` field defines the default upstream-key choice behavior: `strategy` (`round-robin`, `fill-first`, `priority`), `session_affinity`, `session_affinity_header`, and `session_affinity_ttl_seconds`. `upstream_key_pools` lets you define several named key pools with their own strategy and affinity settings. A fallback row selects a pool with `upstream_key_pool`. If a provider is configured only with `upstream_key_pools` and no legacy `apikey`, `upstream_key_pool` is required — the gateway fails configuration validation instead of falling back to an arbitrary key or another pool.
Localized free-tier provider reference catalogs live in [`static/locales/en/free-tier-providers.md`](static/locales/en/free-tier-providers.md) and [`static/locales/ru/free-tier-providers.md`](static/locales/ru/free-tier-providers.md). The original [`examples/free-tier-providers.md`](examples/free-tier-providers.md) remains for the legacy endpoint. Catalogs are not applied automatically: free-tier terms change often, so providers and limits must be enabled manually.

```json
[
    {
        "openrouter":
        {
            "baseUrl" : "https://openrouter.ai/api/v1",            
            "apikey" : "${APIKEY_OPENROUTER}",  // the variable may contain several comma-separated keys
            "models": {
                "deepseek/deepseek-r1:free": {
                    "upstream_limits": {
                        "rpm": 20,
                        "rpd": 200,
                        "tpm": 60000,
                        "tpd": 1000000
                    }
                }
            }
        }
    },
    {
        "nebius":
        {
            "baseUrl" : "https://api.studio.nebius.ai/v1",            
            "apikey" : "${APIKEY_NEBIUS}" // explicit environment reference for the provider API key
        }
    },
    {
        "customproxy":
        {
            "baseUrl" : "https://proxy.example/v1",
            "apikey" : "${APIKEY_CUSTOMPROXY}",
            // explicit model list: the gateway will not query the provider's /models
            "available_models" : ["deepseek/deepseek-r1:free", "qwen/qwen3-max"]
        }
    },
    {
        "pooled_proxy":
        {
            "baseUrl" : "https://proxy.example/v1",
            "type" : "openai",
            "routing" : {
                "strategy" : "fill-first",
                "session_affinity" : true,
                "session_affinity_header" : "X-Workspace-Session",
                "session_affinity_ttl_seconds" : 3600
            },
            "upstream_key_pools" : {
                "primary" : {
                    "strategy" : "priority",
                    "keys" : [
                        { "id" : "team-a", "apikey" : "${APIKEY_POOL_A}", "priority" : 100 },
                        { "id" : "team-b", "apikey" : "${APIKEY_POOL_B}", "priority" : 10 }
                    ]
                }
            }
        }
    },
    {
        "openai":
        {
            "baseUrl" : "https://api.openai.com/v1",            
            "apikey" : "${APIKEY_OPENAI}" // explicit environment reference for the provider API key
        }
    },
    {
        "anthropic":
        {
            "baseUrl" : "https://api.anthropic.com",
            "apikey" : "${APIKEY_ANTHROPIC}",
            "type" : "anthropic" // requests are routed to {baseUrl}/v1/messages with Anthropic headers
        }
    }
]
```


### Fallback Rules JSON Example (`models_fallback_rules.json`):

>[!Note]
> You can edit the fallback rules using web browser in `http://localhost:9000/v1/ui/rules-editor`

Each `gateway_model_name` must be unique in `models_fallback_rules.json`. Duplicate model rules are rejected during startup and are not written by the web editor.
The web editor no longer asks you to type `provider` and `model` manually for fallback rules: the provider is selected from the current `providers.json`, and the model is selected from that provider's `/models` list. Provider catalogs load lazily only when a card is expanded, the model field receives focus, the provider changes, or the user explicitly retries an error; the initial render preserves saved provider/model values without a network fan-out. Concurrent requests for the same provider are deduplicated and cached for 15 minutes. A temporary catalog failure does not erase the saved model or block submitting the original configuration, while a fallback model confirmed absent from the catalog stays visible, is marked unavailable, and must be replaced before saving.
The optional `context_overflow_fallback` field lets you configure a dedicated model that the gateway will try within the same request when the provider reports a context overflow error. Detection uses common OpenAI-compatible signals such as `context_length_exceeded`, `maximum context length`, `context window exceeded`, and similar error wording.
The optional `strip_think_tags` field controls literal `<think>...</think>` blocks for regular text replies on that gateway model. When the field is `true`, the gateway removes those blocks from both non-stream `message.content` and SSE `delta.content`, and for native Anthropic replies (`/v1/messages`) from the `content` text blocks and the `content_block_delta` SSE events. When the field is `false` or omitted, regular text replies stay untouched. This does not change JSON-object mode: there, `<think>` stripping remains part of the mandatory valid-JSON sanitization.

#### Simple fallback 
In this mode (`rotate_models=false`), the gateway always starts with the first model in each request and falls back to the next ones in case of failures.<br>
Retries can be also configured for each model.
```json
[
    {
        "gateway_model_name": "llmgateway/free-stack", // this gateway model to be referenced
        "rotate_models" : "false", // no rotation, always starts with the first model and falls back to the next in case of failure
        "dynamic_penalty": false, // opt-in: temporarily prefer less-penalized routes after 429/5xx
        "strip_think_tags": true, // remove literal <think>...</think> from regular and streaming chat replies
        "fallback_models" :
        [
            { 
                "provider": "openrouter",
                "model" : "deepseek/deepseek-r1:free",
                "retry_delay" : 15,     // retry delay in seconds for this model in case of failure
                "retry_count" : 3,      // how many times to retry
                "providers_order" : ["Chutes", "Targon"] // Force provider order in openrouter for this model
            },
            {
                "provider": "requesty",
                "model" : "google/gemini-2.5-pro-exp-03-25"
            },
            {
                "provider": "openrouter",
                "model" : "deepseek/deepseek-chat-v3-0324:free",
                "use_provider_order_as_fallback": true, // use providers_order as fallback
                "providers_order" : ["Chutes", "Targon"] // if use_provider_order_as_fallback is true, this will be used as fallback one by one
            }
        ]                    
    }
]
```

#### Model Rotation
In this mode (`rotate_models=true`), the gateway cycles through all models between requests. This is useful when we want to utilize credits from various providers. Fallback also works in this mode in case of failures; the sequence loops back to the first model when the sequence finishes.
```json
[
    {
        // an example of a model that exists in various providers
        "gateway_model_name": "llmgateway/deepseek-v3.1", 
        "rotate_models": true,  // When true, gateway rotates models between requests. retry_count/retry_delay still apply to the selected attempt
        "fallback_models" :
        [
            {
                "provider": "openrouter",
                "model" : "deepseek/deepseek-chat-v3-0324",
                "providers_order" : ["Lambda", "DeepInfra", "Nebius AI Studio"]
            },
            {
                "provider": "nebius",
                "model": "deepseek-ai/DeepSeek-V3-0324"
            },
            {
                "provider": "requesty",
                "model" : "novita/deepseek/deepseek-v3-0324"
            }
        ]                    
    }
]    
```

#### Custom Parameters and Headers Injection
For any model, you can inject custom headers and additional body parameters by specifying them in the rules. `custom_body_params` cannot override gateway-reserved fields: `stream`, `messages`, `tool_choice`, `tools`, `model`.
Here is an example of using grok-3-mini-beta from xAI that accepts a `reasoning_effort` parameter.
Custom headers are also available if needed.
```json
[
    {
        // an example of a model with custom body
        "gateway_model_name": "llmgateway/xAI", 
        "fallback_models" :
        [
            {
                "provider": "xAI",
                "model" : "grok-3-mini-beta",                
                "custom_body_params" : {
                    "reasoning": { "effort": "high" }  // grok has this reasoning/effort parameter that can be set like this
                },
                "custom_headers" : {
                    "x-param" : "demo"  // custom header example
                }
            }
        ]                    
    }       
]    
```

For limited top-level payload edits, fallback rows and operation routes can define `payload_transforms`. It is applied after the base payload and `custom_body_params`: `defaults` adds a value only when the field is still absent, `overrides` explicitly replaces a field, and `filters` removes a field before the downstream call. The transform engine is fail-closed and only supports top-level fields; reserved or auth-sensitive fields (`model`, `messages`, `stream`, `tools`, `tool_choice`, `authorization`, `api_key`, `x-api-key`, `cookie`) cannot be changed.

```json
{
    "provider": "xAI",
    "model": "grok-3-mini-beta",
    "payload_transforms": {
        "defaults": { "top_p": 0.9 },
        "overrides": { "parallel_tool_calls": false },
        "filters": ["seed"]
    }
}
```

#### Dedicated fallback model for context overflow errors
If the primary model has a smaller context window than a backup model, you can explicitly configure a dedicated model for context overflow failures.
```json
[
    {
        "gateway_model_name": "llmgateway/long-input",
        "fallback_models": [
            {
                "provider": "devbox",
                "model": "zai.glm-5-air"
            },
            {
                "provider": "devbox",
                "model": "zai.glm-5"
            }
        ],
        "context_overflow_fallback": {
            "provider": "openrouter",
            "model": "openai/gpt-4.1"
        }
    }
]
```

If the first attempt fails because of a context overflow, the gateway tries `context_overflow_fallback` first. Only if that special fallback also fails does it continue through the normal fallback sequence.

#### RTK Token Compression

The gateway supports compressing tool-result content before sending it to the LLM provider. This can save 20–40% of tokens in requests with large tool outputs (git diff, grep, find, ls, tree, build output, etc.).

**How to enable:**
```json
[
    {
        "gateway_model_name": "llmgateway/my-model",
        "compress_tool_results": true,
        "fallback_models": [
            { "provider": "openrouter", "model": "anthropic/claude-3-5-sonnet" }
        ]
    }
]
```

**How it works:**
- Applied once before the first request in the fallback chain (not on each retry).
- Processes only `role: tool` messages (OpenAI format) and `tool_result` blocks (Anthropic/Claude format).
- Error tool results (`is_error: true`) are never compressed — their tracebacks are preserved in full.
- Safe-by-design: on any error or if output grows larger than input, the original text is returned unchanged.
- Compression metrics are logged as: `[RTK Compression] saved: X% | input: N bytes | output: M bytes | filters: [...]`

**12 supported filters:**

| Filter | What it compresses |
|---|---|
| `git-diff` | Unified diffs — truncates hunks > 100 lines |
| `git-status` | Repository status (long and porcelain formats) |
| `git-log` | Long git log output with head/tail preservation |
| `grep` | Groups by file, max 10 matches per file |
| `find` | Groups by directory, max 10 files/dir, 20 dirs total |
| `ls` | Compact ls -la output, removes noise directories |
| `tree` | Removes summary line, caps at 200 lines |
| `dedup-log` | Collapses consecutive duplicate lines |
| `smart-truncate` | Keeps beginning and end of long output |
| `read-numbered` | Compacts numbered files (Cursor/Codex read_file format) |
| `search-list` | Compacts Cursor Glob search result lists |
| `build-output` | Keeps only errors, warnings, summary; strips Compiling/Downloading lines |

**Typical savings:**
- git diff 500 lines → ~70% compression
- grep with 200 matches → ~60% compression
- npm install output → ~50% compression
- readFile > 250 lines → ~40% compression

#### Tool-call rescue

Some models return a tool call as text instead of structured `tool_calls`: Kimi sections, `<function=NAME>{json}</function>`, `<tool_call>{json}</tool_call>`, or bare/```json-fenced JSON that matches one of the declared `tools` schemas. The `tool_call_rescue` field (default `false`) enables recognizing these dialects and converting them into regular `tool_calls` with `finish_reason: "tool_calls"`, including repairing the arguments against the tool's JSON Schema (unwraps double-JSON-encoding, coerces a string to an array/object only when the schema requires it). This feature is never applied to native Anthropic providers. In non-streaming mode, an unrecognized dialect (`unparsed_tool_call_dialect`) moves the request to the next model in the fallback chain; in streaming mode, the gateway holds up to 256 characters in a buffer while trying to recognize the dialect and synthesize `delta.tool_calls` on the fly, and if parsing fails it passes the raw text through unchanged (failover is no longer possible once headers were sent). Enabled via a checkbox in the Fallback Rules web editor.

```json
[
    {
        "gateway_model_name": "llmgateway/kimi-tools",
        "tool_call_rescue": true,
        "fallback_models": [
            { "provider": "openrouter", "model": "moonshotai/kimi-k2" }
        ]
    }
]
```

#### Capability-based fallback-candidate filtering

The `supports_vision`, `supports_tools`, and `context_window` fields in `fallback_models` entries pre-filter candidates that clearly do not fit a given request, before any call is attempted. A `null`/missing field never filters a candidate out — filtering only triggers when the metadata explicitly says "no" (`supports_vision`/`supports_tools` is `false`) or when the estimated request size (prompt token estimate + response reserve, capped at 2000 tokens) exceeds `context_window`. If no candidate remains after filtering, the gateway returns `422` with `code: "no_capable_model"` and the list of filtered-out candidates together with a reason (`vision_unsupported`, `tools_unsupported`, or `context_window_exceeded`). The fields are editable in the Fallback Rules web editor.

You do not need to fill these fields by hand: the gateway automatically resolves unset capability fields and writes ("materializes") them into `models_fallback_rules.json` through the same transactional save path the editor uses (with a `.bak` copy). Resolution is per-field, with sources in priority order: the provider's own `/models` catalog (when it returns OpenRouter-compatible metadata — `architecture.input_modalities`, `supported_parameters`, `context_length`), then the public OpenRouter catalog (matched by model basename without the `:...` suffix, same as Fallback Eval: `openai/gpt-oss-120b:free` matches `gpt-oss-120b`). Auto-fill runs at gateway startup, periodically every 8 hours, and after every rules save in the editor — independently of the OpenRouter catalog refresh timer (it has its own separate 8-hour cycle); after a cold start, while the OpenRouter catalog is not populated yet, the follow-up run happens not after the full 8 hours but shortly after the OpenRouter catalog is first populated. Fields set by automation are listed in the candidate entry's service list `capabilities_autofilled` — only those may be updated by later runs; manually-entered values are never overwritten, and if a field could not be resolved by either source it simply stays empty (catalog errors are logged, nothing is silently substituted). In the web editor, auto-filled fields render as a read-only badge with the value and source (`provider`/`openrouter`); the "Edit" button switches the field to manual mode and removes it from `capabilities_autofilled`, and manual input fields are shown only where auto-fill did not succeed. The status of the last run (time, error, the map of resolved fields with sources) is available via `GET /v1/capability-autofill`.

```json
[
    {
        "gateway_model_name": "llmgateway/vision-safe",
        "fallback_models": [
            {
                "provider": "openrouter",
                "model": "openai/gpt-4o",
                "supports_vision": true,
                "supports_tools": true,
                "context_window": 128000
            },
            {
                "provider": "openrouter",
                "model": "deepseek/deepseek-chat",
                "supports_vision": false,
                "context_window": 64000
            }
        ]
    }
]
```


**Failover and Rotation Logic:**

When a request comes to `/v1/chat/completions`:

1.  The gateway first applies `models_model_rules.json`: alias/prefix rules can map the public model name to an internal route name, while `excluded_models` rejects blocked names.
2.  The gateway finds the rule matching the resolved `model` in the models_fallback_rules.json file.
3.  If the model is not found in the rules, the gateway routes the request to the fallback provider defined by the `FALLBACK_PROVIDER` environment variable (default: `openrouter`). The same fallback provider is also used by `/v1/models`. Startup fails with a validation error if the configured fallback provider is missing from `providers.json`. The model name is kept as received.
4.  If model rotation is enabled (`"rotate_models": true`), the gateway selects the next model in the sequence for each request.
5.  If model rotation is disabled (`"rotate_models": false` or omitted), the gateway always starts with the first model in the sequence and only falls back to the next ones in case of failure.
6.  (OpenRouter only) If the current model has the parameter `use_provider_order_as_fallback=true` and has a list of `providers_order`, the gateway will use only the first provider in the list and fall back to the next ones only in case of failures. This way, the fallback is treated by this gateway and not OpenRouter.
7.  The gateway filters out chain candidates that clearly do not fit the request based on the fallback-rule fields `supports_vision`/`supports_tools`/`context_window` (details below). If no candidate remains after filtering, the request fails with `422` and `code=no_capable_model` before any downstream call is made.
8.  If the selected model fails because of a context overflow and the rule defines `context_overflow_fallback`, the gateway tries that dedicated model first.
9.  If the selected model hits a temporary availability failure (`429`, `5xx`, timeout/connect, `overloaded`, `rate_limit`, `try again later`), the gateway puts the concrete upstream key for that `provider/model` pair into an in-memory cooldown. A single `429` gets a short 90-second cooldown; repeated exhaustions of a key within a rolling 24-hour window escalate along a ladder of 120s → 600s → 3600s → 86400s, and daily-quota errors block the key until UTC midnight. All other temporary failures keep the fixed 10-minute cooldown. If the provider has several keys and the failure is rate-limit-class (`429`, daily quota, an observed `remaining == 0` from headers), the gateway immediately reselects another available key and retries on the same rule — with no pause and without consuming `retry_count` (switches are capped at the number of candidate keys); once no available key remains, the gateway moves on to the next model as usual. For other temporary failures, later requests may use another available key for the same model.
10.  If the provider's response comes back as HTTP 200 but is empty (`empty_completion`) or ignores `response_format: json_object`/`json_schema` (`format_ignored`), the gateway also moves to the next model immediately — but without a cooldown or key penalty, since this is a model-behavior failure, not a provider/key failure.
11.  For streaming requests, the gateway can safely move to the next retry/fallback only before the first real payload chunk is sent to the client. Keepalive/comment chunks do not count as a successful stream start. After the first payload chunk, the stream is committed to the selected upstream.
12.  If the selected model fails for another reason, or if `context_overflow_fallback` also fails, the gateway continues with the next models in the regular fallback sequence until one succeeds. Keys with active cooldown or exhausted upstream ledger are skipped without a downstream call; `dynamic_penalty` changes ordering only for rules where it is explicitly enabled.
13.  If none of the called models succeed, the gateway returns HTTP 503 with the last error text (redacted of secrets), an `attempts[]` field (provider, model, anonymized key alias, `error_class`, `http_status` for each attempt), and, if at least one attempted upstream key is still in cooldown, a `retry_after_seconds` field (the same value is also duplicated in the `Retry-After` header).

**Model Rotation:**

The model rotation feature allows you to distribute requests across multiple providers even when there are no failures. This is useful for:

- Load balancing across different providers
- Avoiding rate limits on individual providers
- Reducing costs by distributing usage

The rotation state is tracked per API key and gateway model combination, ensuring consistent behavior for each client.

### Model policy (`models_model_rules.json`)
The optional `models_model_rules.json` file is selected with `MODEL_RULES_FILENAME`. It is applied on top of chat rules and operation routes before the downstream route is selected:

- `aliases` publishes an additional model name and resolves it to an existing gateway model. The alias is exposed in `/v1/models` as a separate entry with `aliases_to`.
- `prefixes` rewrites model names by prefix: either to a fixed `target`, or by replacing `prefix` with `target_prefix`.
- `excluded_models` hides and rejects models. Exact names and simple trailing wildcards (`"legacy/*"`) are supported.
- `upstream_model_pools` creates synthetic fallback rules with the same fields as `models_fallback_rules.json`. This is useful for separate upstream pools without duplicating them in the main file.

```json
{
    "aliases": {
        "public-fast": "pool-fast"
    },
    "prefixes": [
        { "prefix": "openai/", "target_prefix": "gateway/openai/" }
    ],
    "excluded_models": ["legacy/*"],
    "upstream_model_pools": {
        "pool-fast": {
            "fallback_models": [
                { "provider": "pooled_proxy", "model": "fast-model", "upstream_key_pool": "primary" }
            ],
            "rotate_models": false
        }
    }
}
```

### Operation routes configuration (`models_operation_rules.json`)
The gateway can also load a separate operation routes config for the `embeddings`, `rerank`, `images_generations`, `images_edits`, `audio_speech`, `audio_transcriptions`, `pdf_conversions`, `web_search`, `web_read`, `web_research`, and `web_deep_research` sections. This file is optional: if `models_operation_rules.json` is missing, the application starts with empty sections for all these operation types. An existing file must contain a valid JSON object; use `{}` for an empty configuration because a zero-byte file is rejected. The `/v1/models` endpoint merges these sections with the chat rules and returns a `capabilities` list for gateway models, for example `["chat", "embeddings"]`, `["images"]`, `["audio_speech"]`, `["audio_transcription"]`, `["pdf_conversion"]`, `["web_research"]`, or `["web_deep_research"]`.
The `/v1/embeddings` endpoint uses this config directly: for each `gateway_model_name`, routes are processed in order as a fallback chain. Within a route, `retry_count` and `retry_delay` are applied first; after a retryable `502`/`503` or a response accounting validation error, the endpoint logs the transition and tries the next route while terminal accounting commit has not started. Model rotation is not supported for operation routes.
The `/v1/rerank` endpoint uses the same operation routes config and the same ordered fallback semantics for routes. For rerank routes, `target_path` defaults to `"/score"` when it is omitted. If `target_path` starts with `http://` or `https://`, the gateway uses it as the complete downstream URL and does not join it with `provider.baseUrl`. Vendor-specific request and response formats are now configured per route via `request_format` and `response_format`: for example, `request_format: "query_passages"` enables the `{"query":{"text": ...}, "passages":[{"text": ...}]}` payload, and `response_format: "rankings_logit"` enables reading NVIDIA-style `rankings`/`logit` responses. For self-hosted rerank services with the `{"query": ..., "texts": [...]}` payload, use `request_format: "query_texts"` together with `response_format: "scores"` for `{"scores": [...]}` responses. The same routes can also define `retry_count` and `retry_delay` to retry the same request within a route before switching to the next fallback route. The separate `response_output_format` field controls the final gateway response shape: by default it is `{"data": [{"index": ..., "score": ...}]}`, while `response_output_format: "jina_results"` switches it to `{"results": [{"index": ..., "relevance_score": ...}]}`. Without these fields, the gateway uses the default `text_1`/`text_2` payload and expects downstream results in `results` or `data`. After normalization, `top_n` additionally limits the response length, and `return_documents=true` injects the caller's original documents back into the results.
The `/v1/images` and `/v1/images/generations` endpoints use the `images_generations` section. The `/v1/images/edits` endpoint uses the `images_edits` section and supports either JSON bodies with `images` or `multipart/form-data` uploads with `image` / `image[]` and `mask`. For generation/edit, multiple routes for the same gateway model are used as an ordered fallback chain: the current route first exhausts its own `retry_count`/`retry_delay`, and the next route is tried after a gateway-level `503`; for edit multipart, fallback also runs after downstream `413 Payload Too Large`. Client request validation errors and route configuration errors are not masked by fallback. For edit routes, `target_path` defaults to `"/images/edits"` when omitted. If an edit route sets `request_format: "openai_images_multipart"`, the gateway accepts the caller JSON body but sends downstream `multipart/form-data`; JSON `images`/`mask` values must contain gateway-internal raw `bytes`, a base64/data URL string, or an object with `data_url`, `b64_json`, `base64`, `image_url`/`url` holding base64/data URL content. HTTP(S) image URLs are not downloaded in this mode and return `400`. Streaming is not supported for image endpoints, and `stream=true` returns an explicit `400`.
The `/v1/audio/speech` endpoint uses the `audio_speech` section and accepts an OpenAI-style JSON body with `model`, `input`, `voice`, `response_format`, `speed`, and similar downstream TTS parameters. For speech routes, `target_path` defaults to `"/audio/speech"` when omitted. The gateway uses only the first route for the selected gateway model: fallback/rotation are not supported for TTS, but `retry_count` and `retry_delay` are still available within the route. The downstream response is returned as raw audio; if downstream does not send `content-type`, the gateway sets `audio/mpeg`. The `/v1/audio/voices` endpoint uses the same `audio_speech` routes and calls the route's `voices_target_path`; if the field is omitted, it uses `"/voices"` relative to `provider.baseUrl`. With `model` it returns one TTS model's voices, and without `model` it returns a catalog for all available TTS gateway models. If the downstream catalog includes a `model` field on voices, the gateway filters it by the selected route model. For built-in Silero voices the gateway adds known `gender` and `language`; for other voices it does not invent metadata.
The `/v1/audio/transcriptions` endpoint uses the `audio_transcriptions` section and accepts `multipart/form-data` with OpenAI-style fields such as `file`, `model`, `language`, `prompt`, `response_format`, `temperature`, and `timestamp_granularities[]`. For audio transcription routes, `target_path` defaults to `"/audio/transcriptions"` when omitted. Routes are processed in order as a fallback chain: within a route, `retry_count` and `retry_delay` are applied first, and the next route is tried only after a gateway-level `503`, including network/downstream HTTP failures and invalid downstream JSON. Rotation is not supported, and `stream=true` explicitly returns `400`. By default the route behaves as an OpenAI-compatible multipart passthrough. If the route sets `request_format: "nvidia_riva_grpc"`, the public OpenAI-compatible request is transformed into an NVIDIA Riva gRPC downstream call. For the standard NVIDIA API Catalog provider `https://integrate.api.nvidia.com/v1`, the gateway automatically uses the gRPC endpoint `grpc.nvcf.nvidia.com:443` and requires `custom_headers.function-id`; for self-hosted Riva, set your own host:port in `provider.baseUrl` (for example `https://speech.example:50051`). The NVIDIA gRPC adapter supports `response_format` values `json`, `text`, and `verbose_json`, word/segment timestamps through `timestamp_granularities[]`, and NVIDIA-specific settings via `custom_body_params` (`language`, `enable_automatic_punctuation`, `verbatim_transcripts`, `max_alternatives`, `custom_configuration`, `boosted_lm_words`, `boosted_lm_score`, `speaker_diarization`, and similar). For API Catalog routes the gateway does not forward `model` into the gRPC `RecognitionConfig`, because the model is already selected by `function-id`, and it chooses offline or streaming RPC according to upstream capabilities. If the client omits `language`, the gateway tries to derive a provider-driven default from upstream capabilities; it currently auto-fills only aggregate language codes such as `multi` or `indic` when the NVIDIA API Catalog function advertises them, and otherwise returns an explicit error listing the supported values. Before the downstream call, the gateway normalizes audio to WAV via `ffmpeg` when needed, extracts raw PCM, and sends it to Riva with `RecognitionConfig.encoding = LINEAR_PCM`. OpenAI client fields that have no NVIDIA equivalent are ignored for such routes. If the downstream responds with JSON, the gateway returns JSON; if the downstream responds with plain text / srt / vtt-like content, the gateway proxies it as-is.
The PDF converter uses the `pdf_conversions` section. `POST /v1/pdf/convert` and `POST /v1/pdf/jobs` accept `multipart/form-data` with `file`, `model`, and downstream converter parameters; `model` selects the gateway route and is not forwarded downstream. To translate before DOCX/MD assembly, pass `target_language`, for example `English`; if the field is empty or omitted, translation is skipped. `GET /v1/pdf/jobs/{job_id}`, `/result`, and `/download/{artifact}` require `model` in the query string so virtual-key access is checked against the same gateway model. For PDF routes, `target_path` defaults to `"/api"`, but for services published outside `provider.baseUrl` you can use an absolute URL such as `http://host:18080/pdf/api`; the gateway appends `/convert`, `/jobs`, `/jobs/{id}`, and download paths to it.
Multipart uploads are limited to one audio file, at most four image files plus one mask, and one PDF file. Sizes are controlled by `AUDIO_UPLOAD_MAX_BYTES`, `IMAGE_UPLOAD_MAX_FILE_BYTES`, `IMAGE_UPLOAD_MAX_TOTAL_BYTES`, and `PDF_UPLOAD_MAX_BYTES`; process-wide FIFO admission is controlled by `UPLOAD_INFLIGHT_MAX_BYTES` and `UPLOAD_ADMISSION_TIMEOUT_SECONDS`. Local upload `413`/`503` failures do not start provider fallback. The spool remains open through the route attempt and is rewound before every HTTP attempt.
The `/v1/web/search` endpoint uses the `web_search` section: each entry only has `gateway_model_name` and an optional `query_model` (an internal gateway chat model used to expand the user query into multiple search queries). It also accepts Tavily-compatible fields `include_raw_content`, `read_model`, `include_domains`, `exclude_domains`, and `include_images`; the base response remains `data[]`, while `raw_content` and `images[]` are added to result items when the corresponding flags are enabled. The actual search providers are built into the gateway and fixed — Proxy → Tavily → Jina → Z.AI; each is enabled by setting the corresponding env var (`PROXY_URL`, `TAVILY_API_KEY`, `JINA_API_KEY`, `ZAI_API_KEY`), and adapters are tried in order with the first non-empty result winning. When `include_domains`/`exclude_domains` are set, an adapter is considered successful only if at least one result remains after domain filtering. The `/v1/web/read` endpoint uses the `web_read` section (only `gateway_model_name` in each entry) and also accepts `include_images`. The gateway first attempts a keyless direct HTTP fetch: the URL and every redirect hop are resolved fresh, every resolved address must be public, and the TCP connection is pinned to the validated IP; for Medium URLs it first reads through the Freedium mirror, and falls back to the original Medium URL if that fails. Direct extraction preserves links/images already returned by the extractor in markdown and best-effort collects images from HTML, `srcset`, OpenGraph/Twitter metadata, and JSON-LD only into `images[]`; partial image extraction is not treated as a failure and does not trigger the next source by itself. If direct fetch returned empty or failed and `WEB_READ_CLOAKBROWSER_ENABLED=true`, the gateway tries local CloakBrowser rendering; it is disabled by default because the browser engine performs its own navigation and does not use the direct-fetch IP-pinning layer. Then the gateway tries the same four built-in adapters in order: Proxy → Tavily → Jina → Z.AI. Tavily is called with `include_images` and its separate image list is appended inline into the markdown content; Z.AI reader with `retain_images`, Jina reader with image-retention headers, and all adapter responses are additionally parsed for markdown image links. The additional `/v1/tavily/search` and `/v1/tavily/extract` endpoints use the same gateway models and pipeline but return Tavily-style `results[]` / `failed_results[]` / `raw_content` / `images[]`; the Tavily-compatible top-level `images[]` is collected from result images when `include_images=true`. The `/v1/web/research` endpoint uses the `web_research` section, where `search_model` points to a model from `web_search`, `read_model` points to a model from `web_read`, `rerank_model` points to a model from `rerank`, and `analysis_model` selects the internal chat model for fact extraction and final synthesis. By default research searches `ru` (2 queries), `en` (3 queries), and `zh` (3 queries); `max_results_per_lang`/`max_results` limits the number of found and downloaded candidates per language (default 10, maximum 20), `max_articles` sets the number of best downloaded articles selected after rerank for analysis per language (default 8, maximum 10), `num_queries` overrides query rewrites for the selected search languages when provided, `language` selects source-search languages (`all`, `ru`, `en`, `zh`, or a comma-separated list), and `output_language` selects the final report language (default `ru`). After articles are downloaded, content longer than 16,000 characters is first processed as a whole by `analysis_model`: the model keeps only text relevant to the user's query without blindly cutting off the tail. Then `rerank_model` ranks articles by title and prepared text without putting the URL in the rerank document text; article analysis runs in parallel without an additional hard truncation; then `analysis_model` writes one coherent `output` in the selected language. The `/v1/web/deep-research` endpoint uses the external `gpt-researcher` package, but search/read are replaced with gateway-native adapters: `search_model` points to a service from `web_search`, and `read_model` points to a service from `web_read`. In deep research, the request `language` field selects the final report language and is passed to GPT Researcher as `LANGUAGE`; search queries already produced by GPT Researcher are sent to gateway search directly without another `query_model` rewrite, so the report language does not change the search language. The same `web_deep_research` section defines `fast_model`, `smart_model`, `strategic_model`, optional `embedding_model`, plus `image_generation_model` and `image_generation_size` for image mode. `image_generation_model` must be a gateway model from the `images_generations` section because image generation also flows through the gateway's own `/v1/images/generations` rather than calling an external provider directly. The gateway passes LLM settings to GPT Researcher as `FAST_LLM`, `SMART_LLM`, `STRATEGIC_LLM`, `LANGUAGE`, and `EMBEDDING`, while the OpenAI-compatible base URL points back to the gateway. When the request contains `image_generation: true`, the gateway requires image settings in `web_deep_research`, explicitly installs its own `GatewayImageGenerator` into `researcher.image_generator.image_provider` after creating GPT Researcher, and forwards the parameters as `IMAGE_GENERATION_ENABLED=true`, `IMAGE_GENERATION_MODEL`, `IMAGE_GENERATION_SIZE`, and `IMAGE_GENERATION_PROVIDER=gateway`. No separate image-provider API key is required — everything goes through the gateway API key. Virtual-key access is checked against the external service model in the request (`llmgateway/web-search`, `llmgateway/web-read`, `llmgateway/web-research`, or `llmgateway/web-deep-research`); when search requests `raw_content`, the selected `read_model` is checked as well. Internal `query_model` / `rerank_model` / `analysis_model` calls made by regular research do not require separate entries; models and services invoked by GPT Researcher inside deep research inherit the caller's ACL and must be listed in its `allowed_models`.

Deep research never forwards the raw virtual key to its internal worker. The gateway issues a short-lived, process-local, HMAC-signed context token instead. Every successful internal LLM, embedding, image, search, and read call receives its own admission, reservation, and typed charge event under the original user. The outer `/v1/web/deep-research` event is a zero-cost rollup over child receipts, so spend and TPM are not charged twice; response usage is aggregated from immutable child events. `GPTResearcher.get_costs()` is only a diagnostic consistency check; durable child events remain the source of truth.

For candidate selection/comparison queries, `/v1/web/research` automatically enables `evidence_matrix`. First, `analysis_model` classifies the query and builds required criteria; when the mode applies, the same `analysis_model` extracts structured evidence for each candidate from downloaded sources. The final pass/fail result is not taken from the LLM: the gateway groups evidence itself, accepts only `supports` entries with a quote found in the source text, and removes candidates that do not satisfy all required criteria. If no candidate passes, `output` explicitly reports insufficient evidence. For ordinary research queries, the `evidence_matrix` field is omitted.

Inside the web pipeline, operation fallback applies to gateway operation models: `web_research.rerank_model` uses the ordered fallback from the `rerank` section, while `web_deep_research.embedding_model` is passed to GPT Researcher as a gateway embedding model through `OPENAI_BASE_URL`, so embedding calls go through `/v1/embeddings` and its fallback chain.

Tavily is not required for deep research: GPT Researcher receives URLs through `web_search` and page content through `web_read`. The gateway `/v1/web/deep-research` endpoint does not use GPT Researcher's external retriever configuration.
By default an image route behaves as an OpenAI-compatible passthrough. If the downstream image API uses a different wire format, the route can switch to another adapter through `request_format` / `response_format`. Built-in request-side adapters currently include `openai_images`, `openai_images_multipart`, and `nvidia_genai_json`, and built-in response-side adapters include `openai_images` and `nvidia_artifacts`. `openai_images_multipart` is supported only for `images_edits` and forces caller JSON to downstream multipart conversion. The `request_mapping` and `response_mapping` objects describe field mappings so routes can be configured instead of hardcoded.
Image mappings also include built-in generic transforms: `size_width`, `size_height`, `to_data_url`, `to_data_url_list`, and `first_image_to_reference`. The last one is useful for downstream APIs that expect an image reference or identifier instead of an uploaded file. For example, the NVIDIA `flux.2-klein-4b` preview edit route only accepts strings such as `data:image/png;example_id,{0-3}` and does not support arbitrary uploaded files through OpenAI `/v1/images/edits`; the gateway now validates this up front and returns an explicit `400` instead of sending a known-invalid downstream request.
For mapping-based image routes the gateway does not silently drop unsupported client fields: if a field is not allowed by the configured mapping, the endpoint returns an explicit `400`. If a client field should be accepted for compatibility but the downstream API does not support it, map it to the empty target key `""`; the field is then allowed but omitted from the downstream JSON. NVIDIA `flux.2-klein-4b` generation uses this for the OpenAI `n` parameter.
For OpenAI-compatible image routes where the downstream is almost compatible with the OpenAI Images API but rejects specific client fields, set `request_mapping.omit_client_fields`, for example `["response_format", "seed"]`. The gateway accepts those fields from the client but does not send them in the downstream JSON.

The `models_operation_rules.json` structure consists of `embeddings`, `rerank`, `images_generations`, `images_edits`, `audio_speech`, `audio_transcriptions`, `pdf_conversions`, `web_search`, `web_read`, `web_research`, and `web_deep_research` sections. Route-based section entries (`embeddings`, `rerank`, `images_generations`, `images_edits`, `audio_speech`, `audio_transcriptions`, `pdf_conversions`) contain `gateway_model_name` and a `routes` list where each route can define `provider`, `model`, `target_path`, `custom_headers`, `custom_body_params`, `payload_transforms`, `retry_count`, and `retry_delay`; `audio_speech` routes can also define `voices_target_path` when the voice list is not available at `"/voices"`. For `embeddings`, `rerank`, `images_generations`, `images_edits`, and `audio_transcriptions`, the route order defines the fallback chain; for `audio_speech` and `pdf_conversions`, the first route is used. Image routes can also define `request_format`, `response_format`, `request_mapping`, and `response_mapping`; rerank routes can define the optional `request_format`, `response_format`, and `response_output_format` fields; and audio transcription routes can define `request_format`. The `web_search` and `web_read` sections do not contain downstream routes at all: an entry holds just `gateway_model_name` (and optionally `query_model` for `web_search`), while the actual search/reader providers are built into the gateway and enabled by the presence of the env vars `PROXY_URL` / `TAVILY_API_KEY` / `JINA_API_KEY` / `ZAI_API_KEY`. Legacy `routes` fields are no longer accepted under `web_search`/`web_read`: if they are still present in `models_operation_rules.json`, the gateway refuses to start and returns an explicit validation error — you must remove the `routes` block from these sections manually. A `web_research` entry defines the service model, gateway search/read references (`search_model`, `read_model`), the ranking model (`rerank_model`), and the analysis chat model (`analysis_model`). A `web_deep_research` entry has no downstream routes either: it defines the service model, gateway search/read references (`search_model`, `read_model`), GPT Researcher models (`fast_model`, `smart_model`, `strategic_model`, `embedding_model`), and image settings (`image_generation_model`, `image_generation_size`). `image_generation_model` always references a gateway model from `images_generations`. When operation routes are saved, the gateway additionally validates cross-section references: `analysis_model`, `fast_model`, `smart_model`, `strategic_model` must be declared in fallback rules, `rerank_model` in `rerank`, `embedding_model` in `embeddings`, `image_generation_model` in `images_generations`, and `search_model` / `read_model` in `web_search` / `web_read`. In the rules editor Web tab all these fields are backed by dropdowns that refresh automatically as the related sections change.

Image, audio, PDF, and web gateway models may define a fixed-cost policy at model level: `"cost_calculator": {"unit": "operation", "rate_usd": 0.25}`. `rate_usd` is expressed in USD and must be a finite non-negative number; an explicit `0` makes the operation free, while a missing calculator selects the `0.1 USD` per-operation policy. The durable accounting service now applies this policy: a valid upstream `usage.cost`, including `0`, remains the source of truth; otherwise the selected rate is charged exactly once for a successfully completed client request, without multiplying internal retry/fallback attempts. Errors, cancellations, and incomplete PDF jobs are not billed; repeated successful polling of the same PDF job is idempotent. Rules Editor exposes this field on the Images, Audio, and Web tabs; PDF remains JSON-only, but its `cost_calculator` is preserved when another tab saves the operation document.
Each `gateway_model_name` must be unique within its section. Each `provider` must exist in `providers.json`. For `rerank`, the `target_path` field defaults to `"/score"` when omitted, for `images_edits` to `"/images/edits"`, for `audio_speech` to `"/audio/speech"`, for `audio_transcriptions` to `"/audio/transcriptions"`, and for `pdf_conversions` to `"/api"`. `target_path` may be either a relative path starting with `/` or an absolute `http(s)` URL. For audio routes with `request_format: "nvidia_riva_grpc"`, keep `target_path` at the default `"/audio/transcriptions"` because the real downstream uses gRPC transport rather than an HTTP path. The same `gateway_model_name` is allowed in both chat rules and operation rules because they are separate configuration namespaces.

```json
{
  "embeddings": [
    {
      "gateway_model_name": "gateway/embed-small",
      "routes": [
        {
          "provider": "openrouter",
          "model": "text-embedding-3-small",
          "target_path": "/embeddings",
          "retry_count": 2,
          "retry_delay": 1,
          "custom_headers": {
            "X-Embed-Version": "2026-03"
          },
          "custom_body_params": {
            "encoding_format": "float"
          }
        }
      ]
    }
  ],
  "rerank": [
    {
      "gateway_model_name": "gateway/rerank-v1",
      "routes": [
        {
          "provider": "cohere",
          "model": "rerank-v3.5",
          "retry_count": 1,
          "retry_delay": 2,
          "request_format": "query_passages",
          "response_format": "rankings_logit",
          "response_output_format": "jina_results",
          "custom_headers": {
            "X-Rerank-Version": "2026-03"
          },
          "custom_body_params": {
            "return_documents": true
          }
        }
      ]
    }
  ],
  "images_generations": [
    {
      "gateway_model_name": "gateway/image-generation-v1",
      "routes": [
        {
          "provider": "openrouter",
          "model": "openai/gpt-image-1",
          "target_path": "/images/generations",
          "custom_body_params": {
            "size": "1024x1024",
            "quality": "high"
          }
        }
      ]
    }
  ],
  "images_edits": [
    {
      "gateway_model_name": "gateway/image-edit-v1",
      "routes": [
        {
          "provider": "openrouter",
          "model": "openai/gpt-image-1",
          "custom_body_params": {
            "input_fidelity": "high"
          }
        }
      ]
    }
  ],
  "audio_transcriptions": [
    {
      "gateway_model_name": "gateway/audio-transcription-v1",
      "routes": [
        {
          "provider": "openrouter",
          "model": "gpt-4o-mini-transcribe",
          "custom_body_params": {
            "language": "en",
            "response_format": "json"
          }
        },
        {
          "provider": "nvidia",
          "model": "nvidia/whisper-large-v3",
          "request_format": "nvidia_riva_grpc",
          "custom_headers": {
            "function-id": "b702f636-f60c-4a3d-a6f4-f3568c13bd7d"
          },
          "custom_body_params": {
            "language": "ru",
            "enable_automatic_punctuation": true
          }
        }
      ]
    }
  ],
  "web_search": [
    {
      "gateway_model_name": "gateway/web-search",
      "query_model": "llmgateway/light_model"
    }
  ],
  "web_read": [
    {
      "gateway_model_name": "gateway/web-read"
    }
  ],
  "web_research": [
    {
      "gateway_model_name": "gateway/web-research",
      "search_model": "gateway/web-search",
      "read_model": "gateway/web-read",
      "rerank_model": "gateway/rerank",
      "analysis_model": "llmgateway/light_model"
    }
  ],
  "web_deep_research": [
    {
      "gateway_model_name": "gateway/web-deep-research",
      "search_model": "gateway/web-search",
      "read_model": "gateway/web-read",
      "fast_model": "llmgateway/light_model",
      "smart_model": "llmgateway/light_model",
      "strategic_model": "llmgateway/light_model",
      "embedding_model": "llmgateway/embedding",
      "image_generation_model": "gateway/image-generation-v1",
      "image_generation_size": "1024x1024"
    }
  ]
}
```

## Running

## With pip

The project requires Python 3.12+. Create and activate the local virtual environment, then install the dependencies from the current `requirements.txt`:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

For development, install the pinned dev set. It includes `pytest`, `pytest-cov`, Ruff, Playwright, `httpx2` for Starlette TestClient, the `pytest-playwright` plugin with its `page` fixture, plus `pip-tools` and `pip-audit` for dependency-pipeline checks:
```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
```

The `/v1/web/deep-research` endpoint also needs the external `gpt-researcher` package. It is no longer part of the runtime dependency set and must be installed explicitly:
```bash
pip install -r requirements-research.txt
```

> `requirements.txt`, `requirements-dev.txt`, `requirements-research.txt`, and `requirements-container.txt` are generated files — do not edit them manually. `security-constraints.txt` contains reviewed exact security pins only. The separate `compatibility-constraints.txt` contains reviewed cross-extra bounds that are not security policy; it currently carries the semantic `requests<2.34` boundary required by `arxiv==4.0.0`. The upper bound is intentional rather than an exact pin: generated locks own exact reproducibility, while the compatibility input describes the actual incompatibility boundary. Both constraint files apply to all four locks. Change ordinary direct dependencies in `pyproject.toml`, security pins in `security-constraints.txt`, and agreed compatibility bounds in `compatibility-constraints.txt`, then run the single fail-fast entrypoint:
> ```bash
> ./scripts/compile_requirements.sh
> ```
> Canonical `pip-compile` commands live only in `scripts/check_dependency_contract.py`: the script generates the runtime, dev, research, and container locks in order with the same security/compatibility inputs, then runs static validation. `requirements-container.txt` mirrors the research production closure, adds SHA-256 hashes for `--require-hashes`, and excludes development tools. Active installer dependencies (`pip`/`setuptools`) are included as ordinary exact pins; a comments-only unsafe-omission footer is a contract violation. Normal regeneration does not use `--upgrade` and may preserve existing lock preferences. The independent `python scripts/check_dependency_contract.py --verify-bootstrap` command copies source inputs only into a temporary directory and compiles all four locks without prior outputs, leaving repository locks unchanged. The static checker also validates active direct dependencies and markers for Python 3.12, the security/compatibility contract, exact lock pins, shared-version parity, container hashes, and headers, but intentionally does not replace the resolver. Resolver closure is additionally proved by clean bootstrap, a clean `requirements-dev.txt` install, `python -m pip check`, and `python -m pip_audit -r requirements-dev.txt` without silently excluding advisories. Recreate the local `.venv` after lock changes instead of adding packages manually.
>
> Pytest treats deprecation warnings as errors. The only temporary exception is the exact full `gpt-researcher==0.14.8` upstream warning about its `langchain-community` import; the policy is bound to the entire message (including its URL and end anchor), category/module, and automatically expires on 2026-10-11 through the dependency checker. Changed text or an added suffix is not suppressed. W4 must re-check upstream and remove the exception once the import is gone. This policy does not mask other warning categories; those remain owned by their respective subsystem work.

### With Docker
if you prefer docker deployment see [this guide](/docker/README.md) (Thanks  [canadaduane](https://github.com/canadaduane)!👍)

The Docker image is built on `python:3.12-slim` and installs hash-locked dependencies from `requirements-container.txt`, so dependency updates are included when you rebuild the image.

The canonical product version lives only in `llm_gateway_core/version.py`. Before any Compose command in a new shell session, derive it from source and pass it to the fail-fast build contract:

```bash
export LLMGATEWAY_EXPECTED_PRODUCT_VERSION="$(python3 scripts/check_product_version.py --print)"
docker compose up -d --build
```

The Docker build compares the required `LLMGATEWAY_EXPECTED_PRODUCT_VERSION` with the canonical source before installing dependencies and fails when the value is missing or mismatched. The validated value is used only for the OCI version label and never becomes runtime configuration. The product version in OpenAPI, the startup log, the UI, and the `X-LLMGateway-Build-Version` health header always comes from the Python source; the former `LLMGATEWAY_BUILD_VERSION` is no longer read. The independent `LLMGATEWAY_BUILD_SHA`, `LLMGATEWAY_BUILD_DATE`, and `LLMGATEWAY_BUILD_REF` build args continue to populate revision/date/ref labels and health headers. The Docker healthcheck makes one `HEAD /health` attempt; Docker itself schedules repeated checks.

The provided `docker-compose.yml` mounts one writable `./config` directory at `/app/config`; individual file bind mounts are no longer used because they cannot support atomic replacement through a same-directory `rename`. Before the first start, prepare the directory and migrate existing root configs without overwriting targets:

```bash
sudo python3 scripts/init_docker_config.py --source-dir . --target-dir ./config
```

All six configuration paths point into this directory, so the web editor saves Providers, Fallback, Operations, Fusion, Router, and Model Rules atomically. Both `providers.json` and `models_fallback_rules.json` are required before startup; the entrypoint no longer synthesizes a fallback model. Missing Operations or Model Rules are created atomically as `{}`, while missing Fusion or Router Rules are created as `[]`. The Docker image sets `APP_DIR=/app`; a manual override must be a non-empty absolute path, and relative config filenames resolve from it. Mandatory files, application/config paths, and writable/search/rename support are checked before startup side effects. An ambiguous post-publication failure exits fail-closed with the `publication=uncertain` marker. The final command takes PID 1 through a safe exec handoff; an operating-system execution error returns only a fixed reason and never prints command arguments.

Compose stores generated images in its `gateway_outputs` volume. Ordinary `docker compose down` preserves it, while `docker compose down -v` deletes it permanently. A named volume is not a backup: verified migration, backup, and restore commands with a `count`/SHA-256/`mtime_ns` manifest are documented in the [Docker deployment guide](docker/docker-deployment.md). Stop the gateway during migration/restore, and remove a legacy source manually only after its manifest matches.

### With a systemd service
For a local service outside Docker, first prepare a separate deployment checkout and `.venv` with dependencies from `requirements.txt`:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Before `sudo`, ensure that the canonical checkout, `.venv`, `main.py`, and deployment helpers are root-owned and not writable by the service identity; an ordinary group-writable development checkout intentionally fails the trust preflight. Then use the deployment helper:
```bash
sudo sh docker/setup-gateway-service.sh
```

This command mutates the host: when needed it creates the fixed `llmgateway:llmgateway` system account with UID/GID `10001:10001`, prepares the FHS directories, migrates data, installs the unit, and starts the service. The production installer accepts no path, service-name, or identity overrides; `--test-mode` is for hermetic tests only. The [Docker deployment guide](docker/docker-deployment.md#host-systemd-alternative) includes the read-only inventory command to run first.

Secrets and user variables live in `/etc/llm-gateway/gateway.env`, while generated paths live in `/etc/llm-gateway/runtime.env`; both files are owned by `root:llmgateway` with mode `0640`. `runtime.env` points configs/DB/outputs to `/var/lib/llm-gateway`, logs to `/var/log/llm-gateway`, the CloakBrowser cache to `/var/cache/llm-gateway`, and pins one worker. Before any mutation, the installer runs a secret-safe inventory. Duplicate/reserved env keys, unknown state files, unsafe targets, and a missing source/target environment or mandatory Providers/Fallback fail closed; safe existing targets remain authoritative and the source is never deleted.

The unit passes `systemd-analyze verify`, applies hardening, and has two mandatory `EnvironmentFile` directives in `gateway.env` → `runtime.env` order. A bounded `ExecStartPost` retries the local `HEAD /health` for up to 60 seconds (`TimeoutStartSec=75`) without calling LLM providers. When replacing the unit/runtime files, the installer creates mode-`0600` backups and publishes atomically; failure restores them and leaves the service stopped. A fully converged rerun with the service already active and enabled changes no files and invokes no mutating `systemctl` command.

Restarting an already installed service does not need the installer, which exists for installation and migration. A separate helper restarts the unit and opens its journal right away:
```bash
sh docker/restart-gateway-service.sh
```

It runs `sudo systemctl restart llm-gateway.service` followed by `sudo journalctl -u llm-gateway.service -f` (Ctrl+C to exit). A failed restart aborts the script without opening the journal, so that a failure never reads like a successful start.

If the gateway runs behind a reverse proxy, set `TRUSTED_PROXIES` to a comma-separated list of immediate proxy IPs/CIDRs. `X-Forwarded-For` is used for auth, rejection, and IP-blocking logic only from trusted peers; direct client spoofing is ignored.

## Development Checks

Run the local verification steps from the project root inside `.venv`:

```bash
source .venv/bin/activate
pytest -q
python -m pytest -q
python -m ruff check .
```

Both `pytest -q` and `python -m pytest -q` work from the project root without setting `PYTHONPATH` manually.


## Using on Cline
Once set, you can use it with any local tool that supports services with OpenAI Compatible APIs, like **Cline** and **RooCode**. You just need to configure it as an OpenAI-compatible provider. Here is an example of using this gateway with Cline, set to use the model `'llmgateway/free-stack'`, which only uses models free of charge, as configured in the example above.

![Cline example](./images/cline-example.png)
