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
- **Temporary model cooldown**: if a chat fallback-chain model exhausts its attempts because of a temporary availability failure (`429`, `5xx`, timeout/connect, `overloaded`, `rate_limit`, `try again later`), the gateway marks the `provider/model` pair for 10 minutes and skips it in later requests without making a downstream call. The marker is kept in process memory and resets on restart.
- **Chain-wide retry budget**: a fallback rule can define `max_total_attempts` as a hard cap on the total number of attempts across the entire fallback chain, not just the per-model `retry_count`. When the budget is exhausted, the chain stops even if later fallback models were not tried. The field is exposed in the editor as `Max Total Attempts (chain budget)`.
- **Startup model verification**: on startup the gateway can optionally hit each provider's `/models` endpoint from `providers.json` and diff the response against what is referenced in fallback/operation rules. The mode is controlled by `VERIFY_MODELS_ON_STARTUP=off|warn|strict`: `warn` (default) logs missing models as warnings, `strict` aborts startup, `off` disables the check entirely.
- **OpenRouter free-model ranking**: if the official `openrouter` provider and its API key are configured, the gateway analyzes free OpenRouter text models every 8 hours. For scoring, you can provide multiple OpenRouter keys separated by commas; each OpenRouter request selects the next non-empty key by round-robin. When the eligible model list changes, it runs a full score (`metadataScore + healthScore + latencyScore + liteEvalScore - instabilityPenalty`); when the list is unchanged, it refreshes only health/latency. Lite eval checks instruction following, tool-call JSON, Python code with unit tests, parameterized arithmetic, and factual QA. The result is shown in the read-only **OpenRouter Free** tab in `/v1/ui/rules-editor`; model routing is not changed automatically yet.
- **Virtual API keys**: on top of the master `GATEWAY_API_KEY` you can now provision per-user virtual keys via the `/v1/ui/api-keys` page (master-only). Every key is stored plaintext in `db/api_keys.db` and carries its own USD budget, RPM/TPM limits, and an optional `allowed_models` whitelist. When the budget is exhausted the gateway replies `429`; when a model is not allowed it replies `403`. Virtual keys authenticate via `Authorization: Bearer <lgk_...>` or `X-Api-Key`, and their UI session is restricted to their own statistics — they cannot edit rules or manage other keys. The master admin can open per-key usage statistics directly from the keys page. An unset `budget_usd` disables budget tracking for that key.
- **Tool-schema compatibility shim**: before forwarding requests with `tools` to downstream providers, the gateway rewrites the JSON Schema dialect used in function parameters (flattens `["string","null"]`, collapses `anyOf/oneOf` nullable variants into `nullable: true`, converts draft-04 boolean `exclusiveMaximum/exclusiveMinimum` into draft-07 numeric bounds, and strips `$schema`/`$id`/`$comment`). This removes common 400 errors from strict OpenAI-compatible providers and gRPC backends.


## Gateway endpoints

  - `/v1/models` - Like v1, returns the list of available models. For gateway models defined in chat/embeddings/rerank/images/audio_speech/audio_transcriptions/pdf/web rules, the response also includes merged `capabilities` plus normalized `type` and `architecture` metadata from the matching sections. When the request is served in Anthropic format, the endpoint keeps the Anthropic-compatible `type: "model"` field and exposes the gateway task marker separately as `gateway_type`, together with the same `capabilities` and `architecture`.
  - `/v1/chat/completions` - OpenAI compatible API that routes calls to other providers with fallback in case of call failure.
  - `POST /v1/embeddings` - OpenAI Embeddings-compatible API. It accepts `model`, `input`, `encoding_format`, and `dimensions`, proxies the request to a downstream provider according to `models_operation_rules.json`; multiple routes for the same gateway model are used as an ordered fallback chain. Each route first applies its own `retry_count`/`retry_delay`, and if the downstream still surfaces as a gateway-level `503`, the endpoint tries the next route. Rotation is not supported.
  - `POST /v1/rerank` - Rerank API. It accepts `model`, `query`, `documents` (`list[str]`), `top_n`, and `return_documents`, maps `query -> text_1` and `documents -> text_2`, sends the request to `target_path` (default `"/score"`); multiple routes for the same gateway model are used as an ordered fallback chain. Each route first applies its own `retry_count`/`retry_delay`, and the next route is tried only after a gateway-level `503`, including an invalid downstream rerank response. By default, successful downstream responses are normalized to `{"data": [{"index": int, "score": float, "document"?: str}]}`. If the route field `response_output_format` is set to `jina_results`, the gateway returns the Jina-style shape `{"results": [{"index": int, "relevance_score": float, "document"?: str}]}`. If `target_path` is an absolute `http(s)` URL, the gateway uses it as the full downstream URL instead of joining it with `provider.baseUrl`. For NVIDIA rerank routes, the gateway automatically switches to the native `{"query":{"text": ...}, "passages":[{"text": ...}]}` payload. For rerank services that expect `{"query": ..., "texts": [...]}` and return `{"scores": [...]}`, configure the route with `request_format: "query_texts"` and `response_format: "scores"`; the gateway maps each score back to the source document index and sorts results by score. The `top_n` parameter additionally truncates the result list, and `return_documents=true` reinserts the caller's original documents by `index` or position.
  - `POST /v1/images` and `POST /v1/images/generations` - OpenAI Images generation-compatible endpoints. They accept JSON payloads with `model`, `prompt`, and generation parameters; multiple routes for the same gateway model are used as an ordered fallback chain. Each route first applies its own `retry_count`/`retry_delay`, and the next route is tried only after a gateway-level `503`, including downstream error statuses, network errors, invalid JSON, or invalid image responses. Rotation and streaming are not supported. By default a route behaves as an OpenAI-compatible passthrough, but it can switch to a mapping-based adapter through `request_format`, `response_format`, `request_mapping`, and `response_mapping`.
  - `POST /v1/images/edits` - OpenAI Images edits-compatible endpoint. It supports both JSON bodies with `images` and `multipart/form-data` uploads with `image` / `image[]` and `mask`, rewrites `model` to the configured route model; multiple routes for the same gateway model are used as an ordered fallback chain after a gateway-level `503` or downstream multipart `413 Payload Too Large`. Rotation is not supported, and `stream=true` explicitly returns an error. By default, caller JSON is sent downstream as JSON and caller multipart is sent downstream as multipart; if an edit route sets `request_format: "openai_images_multipart"`, the caller JSON body is converted to downstream `multipart/form-data` for models that do not accept image edit JSON. Edit routes can also use mapping-based adapters for downstream APIs with a different wire format, such as NVIDIA image generation/edit endpoints, without changing the public OpenAI-compatible gateway API.
  - `POST /v1/audio/speech` - OpenAI Audio Speech-compatible endpoint. It accepts JSON with `model`, `input`, `voice`, `response_format`, `speed`, and similar OpenAI-style parameters, rewrites `model` to the configured route model from `audio_speech`, and returns raw downstream audio bytes with the downstream `content-type`. Fallback/rotation are not supported; a single route can still use `retry_count` and `retry_delay`, and `stream=true` returns an explicit `400`.
  - `GET /v1/audio/voices` - TTS voice catalog. With a `model` query parameter it returns voices for one gateway model; without `model` it returns a common JSON object shaped as `gateway_model -> voices[]` for all available `audio_speech` models, filtered by virtual-key `allowed_models`. The gateway normalizes downstream `voices` / `preset_voices` / `custom_voices`; when downstream voices include a `model` field, the gateway filters the catalog by the selected route model. It adds known `gender` and `language` metadata for built-in Silero voices, and leaves unknown voices as plain `id`, `name`, and `source`.
  - `POST /v1/audio/transcriptions` - OpenAI Audio Transcriptions-compatible endpoint. It accepts `multipart/form-data` with OpenAI-style fields (`file`, `model`, `language`, `prompt`, `response_format`, `temperature`, `timestamp_granularities[]`, and similar), rewrites `model` to the configured route model from `models_operation_rules.json`; multiple routes for the same gateway model are used as an ordered fallback chain. Each route first applies its own `retry_count`/`retry_delay`, and the next route is tried only after a gateway-level `503`. Rotation is not supported, and `stream=true` explicitly returns an error. By default the route behaves as an OpenAI-compatible multipart passthrough, but `request_format: "nvidia_riva_grpc"` switches only the downstream transport: the gateway still accepts OpenAI-style multipart and forwards it to NVIDIA Riva gRPC / API Catalog under the hood. For NVIDIA routes, unsupported OpenAI client fields are ignored instead of failing the request. JSON downstream responses are returned as JSON, while plain text / srt / vtt-like responses are proxied as-is for official OpenAI SDK compatibility.
  - `POST /v1/pdf/convert`, `POST /v1/pdf/jobs`, `GET /v1/pdf/jobs/{job_id}`, `GET /v1/pdf/jobs/{job_id}/result`, `GET /v1/pdf/jobs/{job_id}/download/{artifact}` - gateway API for the PDF converter. `convert` and `jobs` accept `multipart/form-data` with `file`, `model`, and converter parameters, including `target_language` for translation before DOCX/MD assembly; `model` is used only to select the gateway route and is not forwarded downstream. Job status/result/download requests require `model` in the query string so virtual-key access is checked against the same gateway model.
  - `POST /v1/web/search` - Web search service. It accepts `model`, `query`, optional `max_results`, `num_queries`, `language`, and Tavily-compatible fields `include_raw_content`, `read_model`, `include_domains`, `exclude_domains`, `include_images`. The response keeps the current `data[]` shape; when `include_raw_content` is enabled, each result gets `raw_content` downloaded through the local `/v1/web/read` pipeline including paid adapter fallback, and when `include_images` is enabled, each result gets `images[]` (empty when the pipeline did not extract images). Virtual-key access is checked against the search model and, when raw content is requested, against `read_model`. The `web_search` section now contains only the service name (`gateway_model_name`) and an optional `query_model` (gateway chat LLM used to expand the user query into several search queries). The actual search always goes through four built-in adapters in a fixed order: Proxy → Tavily → Jina → Z.AI. Each adapter is enabled only if the corresponding environment variable is set (`PROXY_URL`, `TAVILY_API_KEY`, `JINA_API_KEY`, `ZAI_API_KEY`). Adapters are tried in order; the first non-empty response wins, but when domain filters are set the adapter must still have at least one result after filtering.
  - `POST /v1/web/read` - URL reading service. It accepts `model`, `url`, and optional `format`; virtual-key access is checked only against the read-service `model`. The `web_read` section contains only the service name. The gateway first attempts a keyless direct HTTP fetch (no API key required: `trafilatura` → built-in HTML parser fallback, PDF extraction via `pdfminer`, YouTube subtitles via `youtube_transcript_api`). If the direct fetch returned empty or failed, the gateway makes one more local attempt: it renders the page with CloakBrowser and extracts Markdown with `rs-trafilatura`. Only after that does it try the same four paid/external built-in adapters in order: Proxy → Tavily → Jina → Z.AI, each enabled only when its env var is set.
  - `POST /v1/tavily/search` and `POST /v1/tavily/extract` - Tavily-compatible endpoints for clients that expect `results[]`, `failed_results[]`, `raw_content`, `images[]`, `response_time`, `usage`, and `request_id`. They use the same gateway models and the same search/read pipeline, but return Tavily-style shapes instead of `data[]` / `content`.
  - `POST /v1/web/research` - Research service. It accepts `model`, `query`, optional `max_results_per_lang` (alias: `max_results`), `max_articles`, `num_queries`, `language`, `output_language`, and `format`; virtual-key access is checked only against the external service `model`, then the gateway uses configured `search_model`, `read_model`, `rerank_model`, and `analysis_model` internally. If parameters are omitted, search runs across three languages at once: `ru` (2 queries), `en` (3 queries), and `zh` (3 queries), downloads search candidates, reranks the downloaded articles by title and full text, takes up to 8 best articles per language, and analyzes them in parallel. The final `output` is additionally synthesized through `analysis_model` into one coherent report in `output_language` (default `ru`).
  - `POST /v1/web/deep-research` - Deep research service through the external `gpt-researcher` package. It accepts `model`, `query`, and optional `max_words`, `breadth`, `depth`, `concurrency`, `language`, `format`, `image_generation`; `language` selects the final report language, not the search language. Virtual-key access is checked only against the `web_deep_research` service model, while GPT Researcher receives its search/read, LLM/embedding/image models from the `web_deep_research` section and calls the gateway as an OpenAI-compatible API.
  - For `/v1/web/research` and `/v1/web/deep-research`, the gateway explicitly watches for client disconnects after the request body is read: the active research phase is cancelled, and deep research also signals the worker thread for cooperative cancellation.
  - `/v1/responses` - OpenAI Responses-compatible API. It accepts `input` / `instructions` / `text.format` / `tools` style payloads, translates them into the internal OpenAI Chat payload used by the routing layer, and converts the downstream response back into Responses-format.
  - `/v1/messages` - Anthropic Messages compatible API. It accepts Anthropic-format input, translates it into the existing OpenAI-style payload for the routing layer, and converts the response back into Anthropic-format. It is intended to work with the official `anthropic` SDK in `messages.create(...)`, `messages.stream(...)`, and `messages.count_tokens()` mode.

  The `capabilities` field in `/v1/models` is returned only for gateway models defined in chat rules and/or operation routes. Typical values are `["chat"]`, `["embeddings"]`, `["rerank"]`, `["images"]`, `["audio_speech"]`, `["audio_transcription"]`, `["pdf_conversion"]`, `["web_search"]`, `["web_read"]`, `["web_research"]`, and `["web_deep_research"]`; if the same gateway model exists in multiple sections, the gateway returns the merged list, for example `["chat", "embeddings"]`. For gateway models, the endpoint also adds a top-level `type` field (`text`, `embeddings`, `rerank`, `image`, `audio`, `document`, `web`) and an `architecture` block with `input_modalities` / `output_modalities`; when a model has multiple capabilities at once, `type` is omitted instead of pretending there is a single task. For image models, the gateway also returns `image_operations` (`["generation"]`, `["edit"]`, or both), and for image models with an edit route `architecture.input_modalities` also includes `image`. For web models, the gateway also returns `web_operations` (`["search"]`, `["read"]`, `["research"]`, or `["deep_research"]`). For audio speech models, input is `text` and output is `audio`; for audio transcription models, input is `audio` and output is `text`; for PDF conversion models, input and output are `document`. In the Anthropic-compatible response, the `type` field stays `"model"` to match the Anthropic schema, so the normalized gateway task type is exposed as `gateway_type`, while `capabilities`, `architecture`, `image_operations`, and `web_operations` are preserved. Raw fallback-provider models keep the existing behavior and may be returned without `capabilities`, `type`, `gateway_type`, `architecture`, `image_operations`, or `web_operations`.

  
  **HOT FEATURE (Chat Completions):**: The `/v1/chat/completions` endpoint allows you to create a sequence of fallback models to be called in case of failure, with support for retries. For example, if a model response fails, the gateway can retry the same model or automatically move to the next model in the fallback sequence, and so on. The model's sequence can consist of different models and different providers. For instance, the first model in the sequence could be deepseek-chat from OpenRouter, and the gateway can be configured to fall back to gpt-4o from OpenAI in case of failure. This fallback sequence can be of any size and must be configured in the file `models_fallback_rules.json` (either manually or using the new web editor).

  **OpenAI Responses compatibility:** The `/v1/responses` endpoint is compatible with OpenAI Responses-style clients that send `input`, `instructions`, `tools`, `tool_choice`, `text.format`, `max_output_tokens`, and `stream` instead of classic `messages`. The gateway translates `input` message items and `function_call` / `function_call_output` items into the internal OpenAI Chat payload, then routes the request through the same fallback/routing mechanism used by `/v1/chat/completions`. Regular JSON responses and SSE streams are converted back into Responses-format, including `function_call` items and `response.function_call_arguments.delta` stream events for tool-calling flows.

  **Anthropic Messages compatibility:** The `/v1/messages` endpoint is compatible with the official `anthropic` SDK for `messages.create(...)`, `messages.stream(...)`, and `messages.count_tokens()`. The gateway accepts the standard `X-Api-Key: <GATEWAY_API_KEY>` and `anthropic-version` headers, and it still accepts `Authorization: Bearer <GATEWAY_API_KEY>` for direct calls. The `messages`, `system`, `max_tokens`, `temperature`, `top_p`, `top_k`, `metadata`, `stop_sequences`, `stream`, `tools`, `tool_choice`, `cache_control`, `container`, `inference_geo`, `output_config`, `service_tier`, and `thinking` fields are translated into the internal OpenAI-style payload, then routed through the same fallback/routing logic used by `/v1/chat/completions`. Assistant `text`, `thinking`, `redacted_thinking`, and `tool_use` blocks, user `text`, `image`, `document`, and `tool_result` blocks are supported on input. On output, regular JSON responses and SSE streams are converted back into Anthropic-compatible format, including `tool_use` events. For regular JSON responses, unsigned downstream reasoning is not exposed as Anthropic `thinking`, because Anthropic requires `signature` / `signature_delta` fields for those blocks. `messages.count_tokens()` returns a local gateway-side `input_tokens` estimate for the already translated payload and does not perform a separate network call to the provider.

  For chat requests with `response_format: {"type": "json_object"}`, the gateway additionally strips accidental markdown wrappers such as ```` ```json ... ``` ```` or a leading `json` prefix when the inner payload is a valid JSON object. If the model emits reasoning blocks inside `<think>...</think>` tags before the JSON payload, the gateway removes them as well. For regular JSON responses these cleanups are applied to `message.content` as a whole. For SSE streams the gateway removes `<think>...</think>` blocks incrementally from text-delta chunks, then strips the opening ` ```json`/`json` prefix and trims the trailing ` ``` ` fence from the end of the stream without buffering the full response. This applies to `/v1/chat/completions` and also to the compatible `/v1/responses` and `/v1/messages` endpoints that reuse the same routing layer.
  For regular text chat replies and SSE streams, literal `<think>...</think>` blocks can be controlled per gateway model through the fallback-rule field `strip_think_tags`. When it is `true`, the gateway removes those blocks from `message.content` and streaming `delta.content`; when it is `false`, the text is preserved as returned by the provider. The JSON-object sanitization above remains mandatory so the JSON contract stays valid.


## Configuration

### Edit providers and fallback rules
Before starting to use LLMGateway, you need to fill in your providers and models with their fallback rules by accessing the configuration page with your web browser at http://localhost:9000/v1/ui/rules-editor. Refer to the following sections to learn how to structure these rules.
Browser sign-in now goes through a dedicated `/auth/login` page. Opening `/` without authentication redirects to `/auth/login`; opening `/` with valid authentication redirects to `/v1/ui/usage-stats`. After a successful login the server creates a persistent signed HttpOnly cookie, so the browser UI uses that cookie instead of `sessionStorage`; the cookie survives browser restarts and gateway restarts until you explicitly log out or change `GATEWAY_API_KEY`. SDK clients and direct API calls can still use the header `Authorization: Bearer <GATEWAY_API_KEY>`, and `/v1/messages` also accepts `X-Api-Key: <GATEWAY_API_KEY>` together with the standard `anthropic-version` header used by the official SDK. The gateway parses the Bearer header strictly as `scheme + token` separated by whitespace, accepts `Bearer` case-insensitively, returns `401` for malformed headers or missing authentication, and returns `403` only when the header format is correct but the token value is wrong. The only public paths are `/health`, `/auth/login`, `/` as a redirect entrypoint, and static assets under `/static/`. HTML pages without a valid cookie or Bearer token are redirected to login, while API endpoints without valid authentication continue to return `401/403`. Unregistered routes still return the standard `404`.
The Configuration Editor includes the main tabs:
- **Fallback Rules**: A structured form for managing chat model fallback chains.
- **Embeddings**: A structured form for managing `/v1/embeddings` routes.
- **Rerank**: A structured form for managing `/v1/rerank` routes.
- **Images**: A structured form for managing `/v1/images`, `/v1/images/generations`, and `/v1/images/edits` routes.
- **Audio**: A structured form for managing `/v1/audio/speech` and `/v1/audio/transcriptions` routes; the `pdf_conversions` section is preserved by the structured API but is still edited through JSON.
- **Web**: A structured form for managing `/v1/web/search`, `/v1/web/read`, `/v1/web/research`, and `/v1/web/deep-research` services.
- **OpenRouter Free**: A read-only ranking of free OpenRouter text models. Lite eval checks instruction following, tool-call JSON, Python code with unit tests, parameterized arithmetic, and factual QA. The tab is shown only if the official `openrouter` provider and its API key are configured; it does not automatically change routing rules.
- **Providers**: A structured form for managing providers: name, `baseUrl`, `apikey`, API type (`openai`/`anthropic`), optional `proxy`, and the JSON `models` field.

The Fallback Rules, Embeddings, Rerank, Images, Audio, Web, and Providers tabs use a structured form instead of raw JSON. The OpenRouter Free tab only shows the current ranking snapshot and does not save configuration.
The model list for a selected provider is loaded lazily and cached for 15 minutes. In the Fallback Rules tab, manual model entry is not allowed. In the Embeddings, Rerank, Images, and Audio tabs, you can either choose a model from the list or enter it manually if needed.
In the Images tab, advanced options now expose `request_format` / `response_format` plus `request_mapping` / `response_mapping`. This keeps OpenAI format as the default, lets you enable `openai_images_multipart` for JSON-to-multipart image edit routes, or configure vendor-specific image routes such as NVIDIA image generation/edit endpoints without provider-specific hardcoding.
In the Audio tab, you can set `provider`, `model`, `target_path`, `retry_count`, `retry_delay`, `custom_headers`, and `custom_body_params` for TTS routes and OpenAI-compatible transcription routes. TTS routes also expose `voices_target_path`, which is used by `/v1/audio/voices`; the default downstream speech path is `"/audio/speech"`, and the voice catalog uses `"/voices"` when the field is left empty. Transcription routes additionally expose `request_format`; their default `target_path` is `"/audio/transcriptions"`.
For NVIDIA downstreams, set `request_format: "nvidia_riva_grpc"`. The public gateway API still remains OpenAI-compatible at `/v1/audio/transcriptions`, but the downstream call is sent to NVIDIA Riva gRPC. For NVIDIA API Catalog (`build.nvidia.com`), the gateway uses the official gRPC endpoint `grpc.nvcf.nvidia.com:443`; such routes must pass `function-id` through `custom_headers`. NVIDIA-specific settings such as `language`, `enable_automatic_punctuation`, `verbatim_transcripts`, `max_alternatives`, `custom_configuration`, `boosted_lm_words`, `boosted_lm_score`, and `speaker_diarization` can be configured through `custom_body_params`. If the client omits `language`, the gateway tries to derive a safe provider-driven default from upstream capabilities: it currently auto-fills only aggregate codes such as `multi` or `indic` when the NVIDIA API Catalog function itself advertises them; otherwise the endpoint returns an explicit error with the supported language codes. For API Catalog the gateway also chooses the transport mode from upstream capabilities: if the function advertises offline inference, the gateway uses the offline RPC; otherwise it keeps the streaming RPC. If a NVIDIA route receives non-WAV audio, the gateway converts it to WAV locally via `ffmpeg`, then extracts raw PCM and sends it to Riva with `RecognitionConfig.encoding = LINEAR_PCM`.
If one of the already saved fallback models is no longer returned by its provider, the Fallback Rules tab still opens the rule and highlights that specific model as unavailable. Saving remains blocked until you pick an available model from the current provider list.
The Fallback Rules tab uses the Structured API `/v1/config/models-rules/structured`. Each gateway model there also exposes a `Strip <think> tags from replies` toggle that controls literal `<think>...</think>` stripping for regular and streaming chat responses. The Embeddings, Rerank, Images, Audio, and Web tabs use the Structured API `/v1/config/model-operations/structured`, which reads and saves the `embeddings`, `rerank`, `images_generations`, `images_edits`, `audio_speech`, `audio_transcriptions`, `pdf_conversions`, `web_search`, `web_read`, `web_research`, and `web_deep_research` sections without restarting the server. The UI edits Embeddings, Rerank, Images, Audio Speech, Audio Transcriptions, and Web sections; `pdf_conversions` is preserved during those saves but remains JSON-only. The Providers tab uses the Structured API `/v1/config/providers/structured`; on save it still validates `${VAR}` references, rejects provider deletes/renames that would break fallback or operation rules, refreshes runtime provider config, and clears the provider model-list cache. The Web tab configures four services: search, page reading, research, and deep research with GPT Researcher, embedding, and image-generation models. When saving a single operation tab, the editor reloads the latest operation config and replaces only the section being saved so changes in other sections are not overwritten. `retry_delay` is preserved as a fractional number when you enter a value such as `0.5`.
After loading saved data, Embeddings, Rerank, Images, Audio, Web, and Providers cards are shown as collapsed accordions by default; newly added cards are expanded automatically for editing.

If you run the gateway with `docker-compose.yml`, the mounted `providers.json`, `models_fallback_rules.json`, and `models_operation_rules.json` files are now writable inside the container. Changes made in the web editor are persisted directly back to the host files.

![Config example](./images/config-example.png)

## Usage Statistics
From the page http://localhost:9000/v1/ui/usage-stats, you can see your usage statistics.

**Note**: The usage statistics page now includes successful `/v1/chat/completions`, `/v1/responses`, `/v1/messages` (Anthropic), `/v1/embeddings`, `/v1/rerank`, `/v1/images`, `/v1/images/generations`, `/v1/images/edits`, `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/pdf/convert`, `/v1/pdf/jobs`, `/v1/web/search`, `/v1/web/read`, `/v1/web/research`, and `/v1/web/deep-research` calls. For embeddings, rerank, image, audio, PDF, and web operations, the gateway also writes a statistics record when the downstream response does not include a `usage` block; in that case the token fields remain zero, but the request count and route metadata are still visible. Token and cost values can still remain empty or zero for providers whose APIs do not return token usage or return it in an incompatible shape. The local `/v1/messages/count_tokens` endpoint is excluded from usage statistics.
Usage statistics are collected independently from `LOG_CHAT_ENABLED`. The `LOG_CHAT_ENABLED` flag controls only whether detailed chat log files are written to the `logs/` directory.
In usage statistics, the first `Gateway Model` column shows the original gateway model, the adjacent `Resolved Model` column shows the full resolved target in the `provider/model` format, for example `devbox/zai.glm-5`, and the `Operation` column shows the request type (`chat` for OpenAI `/v1/chat/completions` and `/v1/responses`, `messages` for Anthropic `/v1/messages`, `embeddings`, `rerank`, `images_generation`, `images_edit`, `audio_speech`, `audio_transcription`, `pdf_conversion`, `web_search`, `web_read`, `web_research`, `web_deep_research`). Historical records created before this change may still show `N/A` in `Gateway Model` or `Operation` if `gateway_model` and `operation` were not stored for them yet.
For fallback errors with `response_format: {"type": "json_object"}`, the gateway now also appends `system_preview` to `error_message` — the first 100 characters of the first `system` message. This makes it easier to diagnose cases where the downstream provider expects an explicit JSON mention in the instructions while the full prompt remains hidden.
In the **Latest Usage Records** tab, the `Timestamp` column shows the request date and time in the same format as **Fallback Chains**: `YYYY-MM-DDTHH:MM:SS`, without microseconds. Running requests are shown at the top of the table with a colored row highlight; after completion they are replaced by normal usage records.
The same table also includes a `Duration (ms)` column with the full gateway-side request processing time in milliseconds. Older records created before this field existed may show `N/A`.
Usage statistics are cleaned automatically once a day, deleting records older than 90 days.

### Virtual API keys
The page http://localhost:9000/v1/ui/api-keys (available only under the master `GATEWAY_API_KEY`) lets you create, edit and delete virtual API keys prefixed with `lgk_`. Each key can define:

- `budget_usd` — USD spend cap. `null` = no limit. Only finite non-negative values are accepted; `NaN`, `Inf`, and negative values are rejected with `400` before anything is written to the DB. Once `spent_usd >= budget_usd`, the gateway replies `429` for any LLM request made with that key. To stop concurrent and streaming requests from breaking through the cap, the gateway keeps an in-memory per-key reservation ledger: every attempt first reserves a conservative cost estimate, and the reservation is released only after `chat_logging` commits the actual spend. This closes the race where multiple in-flight requests each see a still-stale `spent_usd`.
- `rpm` / `tpm` — 60-second sliding window limits kept in-process. Exceeding either returns `429` with a message indicating which limit was hit.
- `allowed_models` — whitelist of gateway model names. Empty list = any model allowed; non-empty list = only the listed models. This list also filters `/v1/models` and `GET /v1/models/{id}`: a virtual key only sees the models it is allowed to use, and requesting a single disallowed model returns `403`. The master `GATEWAY_API_KEY` is not subject to this restriction and always sees the full catalog.
- `disabled` — instantly blocks the key without deleting it. Disabled keys return a uniform `403` with `detail="api_key_disabled"` for both Bearer/`X-Api-Key` and UI-session callers (no login redirect for sessions), so API and browser behavior stay symmetric.
- `metadata` — free-form JSON object, handy for storing owner/team/tag. The serialized value is capped at 16 KB; exceeding that limit returns `400` and the change is not persisted.

Virtual-key holders that log in via the UI only see `/v1/ui/usage-stats` filtered to their own requests; the **Fallback Analytics**, **Rules Editor**, and **API Keys** tabs are hidden and the corresponding master-only endpoints return `403`. Spend tracking is linked to the request's `api_key_id` and is written through the same shared `WriteBatcher` used by the usage table.

On `/v1/ui/api-keys`, the master admin can click **Usage** on a specific key to see the last-12-month summary and the latest usage records for only that key. For automation, the master can pass the `api_key_id` query parameter to `GET /v1/api/usage-stats/{period}` and `GET /v1/api/usage-records`; for virtual-key holders this parameter is ignored, and statistics remain scoped to their own key.

CRUD over virtual keys is available through the master-only Admin API:
- `GET /v1/admin/api-keys` — list every key together with its `api_key`, `spent_usd`, `last_used_at`.
- `POST /v1/admin/api-keys` — create a new key; the response contains the `api_key` value, and master admins can also view the full key in the list and edit modal.
- `PATCH /v1/admin/api-keys/{id}` — partial update, including `reset_spent: true` to reset the spend counter.
- `DELETE /v1/admin/api-keys/{id}` — remove a key.

### Gateway Documentation UI
The page http://localhost:9000/v1/ui/docs is available to any authenticated user and documents how to call gateway models through the public APIs. It shows the current gateway models from `models_fallback_rules.json` and `models_operation_rules.json`, groups them by capabilities (`chat`, `embeddings`, `rerank`, `images`, `audio_speech`, `audio_transcription`, `pdf_conversion`, `web_*`), and does not make network calls to downstream providers. For a virtual key with `allowed_models`, the catalog shows only the gateway models allowed for that key.

The page gives focused call-parameter details for `/v1/rerank`, `/v1/embeddings`, `/v1/images*`, `/v1/audio/speech`, `/v1/audio/voices`, `/v1/audio/transcriptions`, `/v1/pdf/*`, and the web services `/v1/web/search`, `/v1/web/read`, `/v1/tavily/search`, `/v1/tavily/extract`, `/v1/web/research`, `/v1/web/deep-research`.

### Playground (admin testing)
The page http://localhost:9000/v1/ui/playground (available only under the master `GATEWAY_API_KEY`) provides an admin UI for manually exercising operation endpoints. The old `/v1/ui/web-playground` path remains as a compatible alias. Playground is split into **Web**, **Speech to Text**, **Text to Speech**, **Image Generation**, **Image Editing**, and **PDF Conversion** sections; model dropdowns are populated from the configured `models_operation_rules.json` sections `web_search`, `web_read`, `web_research`, `web_deep_research`, `audio_speech`, `audio_transcriptions`, `images_generations`, `images_edits`, and `pdf_conversions`.

The **Web** section keeps the existing forms for `/v1/web/search`, `/v1/web/read`, `/v1/tavily/search`, `/v1/tavily/extract`, `/v1/web/research`, and `/v1/web/deep-research`. Search/Read forms support `include_raw_content`, `read_model`, `include_domains`, `exclude_domains`, `include_images`, and Tavily-compatible search/extract parameters. Deep research exposes a **Generate illustrations** checkbox — when it is enabled, the gateway runs the request through GPT Researcher with image generation turned on.

The other sections call existing OpenAI-compatible gateway routes. **Text to Speech** loads the voice list for the selected model from `/v1/audio/voices?model=...`, lets the admin choose a language, sends JSON to `/v1/audio/speech`, renders an audio player, and adds a download link for the audio file. **Speech to Text** sends `multipart/form-data` to `/v1/audio/transcriptions`, supports language/response-format selection, and adds a download link for the result. **Image Generation** and **Image Editing** expose image-size selection, show returned `url`/`b64_json` images in a gallery, and add image download links; editing sends up to 4 `image[]` files and an optional `mask` to `/v1/images/edits`. **PDF Conversion** starts an asynchronous job through `/v1/pdf/jobs`, sends the real converter-service parameters (`output=both|docx|md`, `language` such as `rus+eng`, `target_language` for translation, `math_ocr_provider`, `formulas_max_pages`, `max_pages`, `password`, and `ocr_preprocess_save`), shows progress/ETA, and builds artifact download links through `/v1/pdf/jobs/{job_id}/download/{artifact}?model=...` after completion.

Generated illustrations are returned in the `/v1/web/deep-research` response as a dedicated `images[]` field (each entry `{url, prompt, alt_text}`). Files are persisted on the server at `outputs/images/{research_id}/image_*.png` and served by a mounted StaticFiles route at `/outputs/images/...`. Access to `/outputs/images` goes through the same auth layer as the rest of the gateway endpoints; no separate `api_key` is required when the same session that received the URL fetches the file. A background task prunes these files automatically **10 days** after they were created, running once per day.

### Fallback Analytics
The statistics page includes a **Fallback Analytics** tab that provides detailed information about provider transitions (fallback events). The tab contains two sub-tabs:

- **Summary** — aggregated failure statistics by period. Shows which provider/model failed, with what error (429, timeout, etc.), how many times, and average attempt duration.
- **Chains** — detailed fallback chains for individual requests. Only requests with 2+ attempts are shown. Each chain can be expanded to see the full sequence: which providers were tried, the reason for each failure, an expanded error message for generic upstream failures and transport failures (`ReadTimeout`, `ConnectTimeout`, `ConnectError`) with a short payload summary, and how long each attempt took (with a visual duration bar).

Fallback event data is stored in the `fallback_events` table in the same database (`tokens_usage.db`) and is subject to the same automatic cleanup (90 days).

API endpoints:
- `GET /v1/api/fallback-stats/{period}` — aggregated failure statistics (period: hour/day/week/month)
- `GET /v1/api/fallback-records?limit=25&offset=0` — fallback chains with pagination

Responses for `GET /v1/api/usage-stats/{period}` and `GET /v1/api/fallback-stats/{period}` are cached in memory for 30 seconds. This protects SQLite from repeated full-scan aggregations when the dashboard is open in several tabs at once, each polling the same endpoint. The 30-second lag is invisible in the UI, which already shows a snapshot from the previous poll anyway.

![Config example](./images/statistics-example-01.png)

![Config example](./images/statistics-example-02.png)


## .env file

Create a `.env` file from the example .env.example:
```bash
cp .env.example .env
```

Process environment variables always take precedence over values from `.env`. The `.env` file is used only for variables that are missing from the real process environment.

 ### **.env** configuration example:
 ```
# This gateway must have its own API key that clients must use to access it
# Use it in http header as "Authorization: Bearer <ThisGatewayApiKey>"
GATEWAY_API_KEY=<ThisGatewayApiKey>

# Maximum number of log files to keep (older files will be deleted)
LOG_FILE_LIMIT=15

# Enable/disable logging of chat messages to the /logs folder (true/false). 
# Useful for debugging
LOG_CHAT_ENABLED=false

# Temporary debug mode for fallback investigations.
# When true, failed fallback warning logs keep full request messages instead of redacting them.
# Enable it only temporarily because full prompt/history will be written to logs.
LOG_FALLBACK_FULL_MESSAGES=false

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
| `LOG_FILE_LIMIT` | Maximum number of chat log files to keep | `15` |
| `LOG_CHAT_ENABLED` | Enable detailed chat log files in the `logs/` directory. It does not affect usage-statistics collection. | `false` |
| `LOG_FALLBACK_FULL_MESSAGES` | Temporary debug flag for fallback investigations. When enabled, warning logs keep `messages` for failed fallback attempts instead of redacting them. Use it only temporarily because full prompt/history will be written to logs. | `false` |
| `CORS_ALLOW_ORIGINS` | Optional comma-separated list of allowed cross-origin origins. When unset, CORS stays disabled and same-origin UI still works. | disabled |
| `FALLBACK_PROVIDER` | Default provider name shared by `/v1/models` and `/v1/chat/completions` when no rule matches. Startup fails if this provider is missing from `providers.json`. | `openrouter` |
| `VERIFY_MODELS_ON_STARTUP` | Startup model-availability check mode: `off` disables it, `warn` logs warnings for missing models, `strict` aborts startup. | `warn` |
| `PROXY_URL` | Base URL of your own Proxy service used by the built-in `/v1/web/search` and `/v1/web/read` adapters (`{PROXY_URL}/zai/search` and `{PROXY_URL}/zai/read`). When unset, the Proxy adapter is skipped. | disabled |
| `TAVILY_API_KEY` | API key for Tavily, used by the built-in `/v1/web/search` (Tavily Search) and `/v1/web/read` (Tavily Extract) adapters. You can provide multiple keys separated by commas; on each Tavily call the gateway selects the next non-empty key by round-robin. When unset, the Tavily adapter is skipped. | disabled |
| `JINA_API_KEY` | API key for Jina AI, used by the built-in `/v1/web/search` (Jina Search) and `/v1/web/read` (Jina Reader at `r.jina.ai`) adapters. You can provide multiple keys separated by commas; on each Jina call the gateway selects the next non-empty key by round-robin. When unset, the Jina adapter is skipped. | disabled |
| `ZAI_API_KEY` | API key for Z.AI, used by the built-in `/v1/web/search` and `/v1/web/read` (Z.AI Reader) adapters. You can provide multiple keys separated by commas; on each Z.AI call the gateway selects the next non-empty key by round-robin. When unset, the Z.AI adapter is skipped. | disabled |
| `APIKEY_PROVIDERNAME` | API key for a specific provider (e.g., `APIKEY_OPENROUTER`). In `providers.json`, reference it explicitly as `${APIKEY_PROVIDERNAME}`. You can provide multiple keys separated by commas; on each downstream provider call the gateway selects the next non-empty key by round-robin. OpenRouter Free scoring uses the same rule and selects a key separately for each OpenRouter request. | *required for providers in providers.json* |

## Providers Example (`providers.json`)
Here, you must define your providers. By default each provider is treated as OpenAI-compatible, but the optional `type` field lets you declare the native wire format: `"openai"` (default) or `"anthropic"`. Anthropic providers are called at `{baseUrl}/v1/messages` with `x-api-key` and `anthropic-version` headers, and their model catalog is fetched from `{baseUrl}/v1/models`. The gateway converts requests and responses in both directions automatically: an OpenAI client that falls back to an Anthropic provider still receives an OpenAI-shaped response (including streaming SSE chunks and `tool_calls`), and an Anthropic client that falls back to an OpenAI provider still receives a native Anthropic response. For Anthropic providers the gateway injects `max_tokens=32768` when the client request does not set it explicitly. For OpenAI providers the `type` field can be omitted — existing `providers.json` files keep working unchanged.
The `apikey` and `proxy` fields are treated as literals by default. To read a value from an environment variable, use the explicit `${VAR_NAME}` syntax, for example `"apikey": "${APIKEY_OPENROUTER}"`. The value `"APIKEY_OPENROUTER"` without `${...}` is not resolved from the environment; if such an environment variable exists, the gateway logs a warning and still uses the string as a literal.
Provider names must be unique in `providers.json`. Duplicate provider entries are rejected during startup, and the web editor returns `400` instead of silently overwriting the previous entry.
When saving `providers.json` through the UI, the gateway switches to strict `${VAR}` resolution: if the payload references an environment variable that is not present in the process or `.env`, the gateway responds with `400` and the message `env var X referenced but missing for provider 'Y' field 'Z'` and does not write the file. Saving also re-validates `provider` references from the already loaded `models_fallback_rules.json` and `models_operation_rules.json`: if renaming or removing a provider leaves dangling references in those rules, the gateway responds with `400` listing the broken routes instead of silently writing an inconsistent state.

```json
[
    {
        "openrouter":
        {
            "baseUrl" : "https://openrouter.ai/api/v1",            
            "apikey" : "${APIKEY_OPENROUTER}"  // explicit environment reference for the provider API key
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
The web editor no longer asks you to type `provider` and `model` manually for fallback rules: the provider is selected from the current `providers.json`, and the model is selected from that provider's `/models` list. If the provider cannot return its model list, or if the previously saved model is no longer present in that list, the editor shows a clear error that lists the unavailable fallback models and refuses to save the change.
The optional `context_overflow_fallback` field lets you configure a dedicated model that the gateway will try within the same request when the provider reports a context overflow error. Detection uses common OpenAI-compatible signals such as `context_length_exceeded`, `maximum context length`, `context window exceeded`, and similar error wording.
The optional `strip_think_tags` field controls literal `<think>...</think>` blocks for regular text replies on that gateway model. When the field is `true`, the gateway removes those blocks from both non-stream `message.content` and SSE `delta.content`. When the field is `false` or omitted, regular text replies stay untouched. This does not change JSON-object mode: there, `<think>` stripping remains part of the mandatory valid-JSON sanitization.

#### Simple fallback 
In this mode (`rotate_models=false`), the gateway always starts with the first model in each request and falls back to the next ones in case of failures.<br>
Retries can be also configured for each model.
```json
[
    {
        "gateway_model_name": "llmgateway/free-stack", // this gateway model to be referenced
        "rotate_models" : "false", // no rotation, always starts with the first model and falls back to the next in case of failure
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


**Failover and Rotation Logic:**

When a request comes to `/v1/chat/completions`:

1.  The gateway finds the rule matching the requested `model` in the models_fallback_rules.json file.
2.  If the model is not found in the rules, the gateway routes the request to the fallback provider defined by the `FALLBACK_PROVIDER` environment variable (default: `openrouter`). The same fallback provider is also used by `/v1/models`. Startup fails with a validation error if the configured fallback provider is missing from `providers.json`. The name of the model will be the same as received.
3.  If model rotation is enabled (`"rotate_models": true`), the gateway selects the next model in the sequence for each request.
4.  If model rotation is disabled (`"rotate_models": false` or ommited), the gateway always starts with the first model in the sequence and only falls back to the next ones in case of failure.
5.  (OpenRouter only) If the current model has the parameter `use_provider_order_as_fallback=true` and has a list of `providers_order`, the gateway will use only the first provider in the list and fall back to the next ones only in case of failures. This way, the fallback is treated by this gateway and not OpenRouter.
6.  If the selected model fails because of a context overflow and the rule defines `context_overflow_fallback`, the gateway tries that dedicated model first.
7.  If the selected model exhausts its attempts because of a temporary availability failure (`429`, `5xx`, timeout/connect, `overloaded`, `rate_limit`, `try again later`), the gateway puts the `provider/model` pair into an in-memory cooldown for 10 minutes.
8.  If the selected model fails for another reason, or if `context_overflow_fallback` also fails, the gateway continues with the next models in the regular fallback sequence until one succeeds. Models with an active cooldown are skipped without a downstream call.
9.  Return an HTTP 503 error if none of the called models succeed.

**Model Rotation:**

The model rotation feature allows you to distribute requests across multiple providers even when there are no failures. This is useful for:

- Load balancing across different providers
- Avoiding rate limits on individual providers
- Reducing costs by distributing usage

The rotation state is tracked per API key and gateway model combination, ensuring consistent behavior for each client.

### Operation routes configuration (`models_operation_rules.json`)
The gateway can also load a separate operation routes config for the `embeddings`, `rerank`, `images_generations`, `images_edits`, `audio_speech`, `audio_transcriptions`, `pdf_conversions`, `web_search`, `web_read`, `web_research`, and `web_deep_research` sections. This file is optional: if `models_operation_rules.json` is missing or empty, the application starts with empty sections for all these operation types. The `/v1/models` endpoint merges these sections with the chat rules and returns a `capabilities` list for gateway models, for example `["chat", "embeddings"]`, `["images"]`, `["audio_speech"]`, `["audio_transcription"]`, `["pdf_conversion"]`, `["web_research"]`, or `["web_deep_research"]`.
The `/v1/embeddings` endpoint uses this config directly: for each `gateway_model_name`, routes are processed in order as a fallback chain. Within a route, `retry_count` and `retry_delay` are applied first; if the route exhausts its retries and returns a gateway-level `503`, the endpoint logs the transition and tries the next route. Model rotation is not supported for operation routes.
The `/v1/rerank` endpoint uses the same operation routes config and the same ordered fallback semantics for routes. For rerank routes, `target_path` defaults to `"/score"` when it is omitted. If `target_path` starts with `http://` or `https://`, the gateway uses it as the complete downstream URL and does not join it with `provider.baseUrl`. Vendor-specific request and response formats are now configured per route via `request_format` and `response_format`: for example, `request_format: "query_passages"` enables the `{"query":{"text": ...}, "passages":[{"text": ...}]}` payload, and `response_format: "rankings_logit"` enables reading NVIDIA-style `rankings`/`logit` responses. For self-hosted rerank services with the `{"query": ..., "texts": [...]}` payload, use `request_format: "query_texts"` together with `response_format: "scores"` for `{"scores": [...]}` responses. The same routes can also define `retry_count` and `retry_delay` to retry the same request within a route before switching to the next fallback route. The separate `response_output_format` field controls the final gateway response shape: by default it is `{"data": [{"index": ..., "score": ...}]}`, while `response_output_format: "jina_results"` switches it to `{"results": [{"index": ..., "relevance_score": ...}]}`. Without these fields, the gateway uses the default `text_1`/`text_2` payload and expects downstream results in `results` or `data`. After normalization, `top_n` additionally limits the response length, and `return_documents=true` injects the caller's original documents back into the results.
The `/v1/images` and `/v1/images/generations` endpoints use the `images_generations` section. The `/v1/images/edits` endpoint uses the `images_edits` section and supports either JSON bodies with `images` or `multipart/form-data` uploads with `image` / `image[]` and `mask`. For generation/edit, multiple routes for the same gateway model are used as an ordered fallback chain: the current route first exhausts its own `retry_count`/`retry_delay`, and the next route is tried after a gateway-level `503`; for edit multipart, fallback also runs after downstream `413 Payload Too Large`. Client request validation errors and route configuration errors are not masked by fallback. For edit routes, `target_path` defaults to `"/images/edits"` when omitted. If an edit route sets `request_format: "openai_images_multipart"`, the gateway accepts the caller JSON body but sends downstream `multipart/form-data`; JSON `images`/`mask` values must contain gateway-internal raw `bytes`, a base64/data URL string, or an object with `data_url`, `b64_json`, `base64`, `image_url`/`url` holding base64/data URL content. HTTP(S) image URLs are not downloaded in this mode and return `400`. Streaming is not supported for image endpoints, and `stream=true` returns an explicit `400`.
The `/v1/audio/speech` endpoint uses the `audio_speech` section and accepts an OpenAI-style JSON body with `model`, `input`, `voice`, `response_format`, `speed`, and similar downstream TTS parameters. For speech routes, `target_path` defaults to `"/audio/speech"` when omitted. The gateway uses only the first route for the selected gateway model: fallback/rotation are not supported for TTS, but `retry_count` and `retry_delay` are still available within the route. The downstream response is returned as raw audio; if downstream does not send `content-type`, the gateway sets `audio/mpeg`. The `/v1/audio/voices` endpoint uses the same `audio_speech` routes and calls the route's `voices_target_path`; if the field is omitted, it uses `"/voices"` relative to `provider.baseUrl`. With `model` it returns one TTS model's voices, and without `model` it returns a catalog for all available TTS gateway models. If the downstream catalog includes a `model` field on voices, the gateway filters it by the selected route model. For built-in Silero voices the gateway adds known `gender` and `language`; for other voices it does not invent metadata.
The `/v1/audio/transcriptions` endpoint uses the `audio_transcriptions` section and accepts `multipart/form-data` with OpenAI-style fields such as `file`, `model`, `language`, `prompt`, `response_format`, `temperature`, and `timestamp_granularities[]`. For audio transcription routes, `target_path` defaults to `"/audio/transcriptions"` when omitted. Routes are processed in order as a fallback chain: within a route, `retry_count` and `retry_delay` are applied first, and the next route is tried only after a gateway-level `503`, including network/downstream HTTP failures and invalid downstream JSON. Rotation is not supported, and `stream=true` explicitly returns `400`. By default the route behaves as an OpenAI-compatible multipart passthrough. If the route sets `request_format: "nvidia_riva_grpc"`, the public OpenAI-compatible request is transformed into an NVIDIA Riva gRPC downstream call. For the standard NVIDIA API Catalog provider `https://integrate.api.nvidia.com/v1`, the gateway automatically uses the gRPC endpoint `grpc.nvcf.nvidia.com:443` and requires `custom_headers.function-id`; for self-hosted Riva, set your own host:port in `provider.baseUrl` (for example `https://speech.example:50051`). The NVIDIA gRPC adapter supports `response_format` values `json`, `text`, and `verbose_json`, word/segment timestamps through `timestamp_granularities[]`, and NVIDIA-specific settings via `custom_body_params` (`language`, `enable_automatic_punctuation`, `verbatim_transcripts`, `max_alternatives`, `custom_configuration`, `boosted_lm_words`, `boosted_lm_score`, `speaker_diarization`, and similar). For API Catalog routes the gateway does not forward `model` into the gRPC `RecognitionConfig`, because the model is already selected by `function-id`, and it chooses offline or streaming RPC according to upstream capabilities. If the client omits `language`, the gateway tries to derive a provider-driven default from upstream capabilities; it currently auto-fills only aggregate language codes such as `multi` or `indic` when the NVIDIA API Catalog function advertises them, and otherwise returns an explicit error listing the supported values. Before the downstream call, the gateway normalizes audio to WAV via `ffmpeg` when needed, extracts raw PCM, and sends it to Riva with `RecognitionConfig.encoding = LINEAR_PCM`. OpenAI client fields that have no NVIDIA equivalent are ignored for such routes. If the downstream responds with JSON, the gateway returns JSON; if the downstream responds with plain text / srt / vtt-like content, the gateway proxies it as-is.
The PDF converter uses the `pdf_conversions` section. `POST /v1/pdf/convert` and `POST /v1/pdf/jobs` accept `multipart/form-data` with `file`, `model`, and downstream converter parameters; `model` selects the gateway route and is not forwarded downstream. To translate before DOCX/MD assembly, pass `target_language`, for example `English`; if the field is empty or omitted, translation is skipped. `GET /v1/pdf/jobs/{job_id}`, `/result`, and `/download/{artifact}` require `model` in the query string so virtual-key access is checked against the same gateway model. For PDF routes, `target_path` defaults to `"/api"`, but for services published outside `provider.baseUrl` you can use an absolute URL such as `http://host:18080/pdf/api`; the gateway appends `/convert`, `/jobs`, `/jobs/{id}`, and download paths to it.
The `/v1/web/search` endpoint uses the `web_search` section: each entry only has `gateway_model_name` and an optional `query_model` (an internal gateway chat model used to expand the user query into multiple search queries). It also accepts Tavily-compatible fields `include_raw_content`, `read_model`, `include_domains`, `exclude_domains`, and `include_images`; the base response remains `data[]`, while `raw_content` and `images[]` are added to result items when the corresponding flags are enabled. The actual search providers are built into the gateway and fixed — Proxy → Tavily → Jina → Z.AI; each is enabled by setting the corresponding env var (`PROXY_URL`, `TAVILY_API_KEY`, `JINA_API_KEY`, `ZAI_API_KEY`), and adapters are tried in order with the first non-empty result winning. When `include_domains`/`exclude_domains` are set, an adapter is considered successful only if at least one result remains after domain filtering. The `/v1/web/read` endpoint uses the `web_read` section (only `gateway_model_name` in each entry) and also accepts `include_images`. The gateway first attempts a keyless direct HTTP fetch, then a local CloakBrowser render + `rs-trafilatura` extraction, and only after that falls through the same four built-in adapters in order. The additional `/v1/tavily/search` and `/v1/tavily/extract` endpoints use the same gateway models and pipeline but return Tavily-style `results[]` / `failed_results[]` / `raw_content` / `images[]`. The `/v1/web/research` endpoint uses the `web_research` section, where `search_model` points to a model from `web_search`, `read_model` points to a model from `web_read`, `rerank_model` points to a model from `rerank`, and `analysis_model` selects the internal chat model for fact extraction and final synthesis. By default research searches `ru` (2 queries), `en` (3 queries), and `zh` (3 queries); `max_results_per_lang`/`max_results` limits the number of found and downloaded candidates per language (default 10, maximum 20), `max_articles` sets the number of best downloaded articles selected after rerank for analysis per language (default 8, maximum 10), `num_queries` overrides query rewrites for the selected search languages when provided, `language` selects source-search languages (`all`, `ru`, `en`, `zh`, or a comma-separated list), and `output_language` selects the final report language (default `ru`). After articles are downloaded, content longer than 16,000 characters is first processed as a whole by `analysis_model`: the model keeps only text relevant to the user's query without blindly cutting off the tail. Then `rerank_model` ranks articles by title and prepared text without putting the URL in the rerank document text; article analysis runs in parallel without an additional hard truncation; then `analysis_model` writes one coherent `output` in the selected language. The `/v1/web/deep-research` endpoint uses the external `gpt-researcher` package, but search/read are replaced with gateway-native adapters: `search_model` points to a service from `web_search`, and `read_model` points to a service from `web_read`. In deep research, the request `language` field selects the final report language and is passed to GPT Researcher as `LANGUAGE`; search queries already produced by GPT Researcher are sent to gateway search directly without another `query_model` rewrite, so the report language does not change the search language. The same `web_deep_research` section defines `fast_model`, `smart_model`, `strategic_model`, optional `embedding_model`, plus `image_generation_model` and `image_generation_size` for image mode. `image_generation_model` must be a gateway model from the `images_generations` section because image generation also flows through the gateway's own `/v1/images/generations` rather than calling an external provider directly. The gateway passes LLM settings to GPT Researcher as `FAST_LLM`, `SMART_LLM`, `STRATEGIC_LLM`, `LANGUAGE`, and `EMBEDDING`, while the OpenAI-compatible base URL points back to the gateway. When the request contains `image_generation: true`, the gateway requires image settings in `web_deep_research`, explicitly installs its own `GatewayImageGenerator` into `researcher.image_generator.image_provider` after creating GPT Researcher, and forwards the parameters as `IMAGE_GENERATION_ENABLED=true`, `IMAGE_GENERATION_MODEL`, `IMAGE_GENERATION_SIZE`, and `IMAGE_GENERATION_PROVIDER=gateway`. No separate image-provider API key is required — everything goes through the gateway API key. Virtual-key access is checked against the external service model in the request (`llmgateway/web-search`, `llmgateway/web-read`, `llmgateway/web-research`, or `llmgateway/web-deep-research`); when search requests `raw_content`, the selected `read_model` is checked as well. Internal `query_model` / `rerank_model` / `analysis_model` calls and GPT Researcher model calls do not require separate `allowed_models` entries.

Inside the web pipeline, operation fallback applies to gateway operation models: `web_research.rerank_model` uses the ordered fallback from the `rerank` section, while `web_deep_research.embedding_model` is passed to GPT Researcher as a gateway embedding model through `OPENAI_BASE_URL`, so embedding calls go through `/v1/embeddings` and its fallback chain.

Tavily is not required for deep research: GPT Researcher receives URLs through `web_search` and page content through `web_read`. The gateway `/v1/web/deep-research` endpoint does not use GPT Researcher's external retriever configuration.
By default an image route behaves as an OpenAI-compatible passthrough. If the downstream image API uses a different wire format, the route can switch to another adapter through `request_format` / `response_format`. Built-in request-side adapters currently include `openai_images`, `openai_images_multipart`, and `nvidia_genai_json`, and built-in response-side adapters include `openai_images` and `nvidia_artifacts`. `openai_images_multipart` is supported only for `images_edits` and forces caller JSON to downstream multipart conversion. The `request_mapping` and `response_mapping` objects describe field mappings so routes can be configured instead of hardcoded.
Image mappings also include built-in generic transforms: `size_width`, `size_height`, `to_data_url`, `to_data_url_list`, and `first_image_to_reference`. The last one is useful for downstream APIs that expect an image reference or identifier instead of an uploaded file. For example, the NVIDIA `flux.2-klein-4b` preview edit route only accepts strings such as `data:image/png;example_id,{0-3}` and does not support arbitrary uploaded files through OpenAI `/v1/images/edits`; the gateway now validates this up front and returns an explicit `400` instead of sending a known-invalid downstream request.
For mapping-based image routes the gateway does not silently drop unsupported client fields: if a field is not allowed by the configured mapping, the endpoint returns an explicit `400`. If a client field should be accepted for compatibility but the downstream API does not support it, map it to the empty target key `""`; the field is then allowed but omitted from the downstream JSON. NVIDIA `flux.2-klein-4b` generation uses this for the OpenAI `n` parameter.

The `models_operation_rules.json` structure consists of `embeddings`, `rerank`, `images_generations`, `images_edits`, `audio_speech`, `audio_transcriptions`, `pdf_conversions`, `web_search`, `web_read`, `web_research`, and `web_deep_research` sections. Route-based section entries (`embeddings`, `rerank`, `images_generations`, `images_edits`, `audio_speech`, `audio_transcriptions`, `pdf_conversions`) contain `gateway_model_name` and a `routes` list where each route can define `provider`, `model`, `target_path`, `custom_headers`, `custom_body_params`, `retry_count`, and `retry_delay`; `audio_speech` routes can also define `voices_target_path` when the voice list is not available at `"/voices"`. For `embeddings`, `rerank`, `images_generations`, `images_edits`, and `audio_transcriptions`, the route order defines the fallback chain; for `audio_speech` and `pdf_conversions`, the first route is used. Image routes can also define `request_format`, `response_format`, `request_mapping`, and `response_mapping`; rerank routes can define the optional `request_format`, `response_format`, and `response_output_format` fields; and audio transcription routes can define `request_format`. The `web_search` and `web_read` sections do not contain downstream routes at all: an entry holds just `gateway_model_name` (and optionally `query_model` for `web_search`), while the actual search/reader providers are built into the gateway and enabled by the presence of the env vars `PROXY_URL` / `TAVILY_API_KEY` / `JINA_API_KEY` / `ZAI_API_KEY`. Legacy `routes` fields are no longer accepted under `web_search`/`web_read`: if they are still present in `models_operation_rules.json`, the gateway refuses to start and returns an explicit validation error — you must remove the `routes` block from these sections manually. A `web_research` entry defines the service model, gateway search/read references (`search_model`, `read_model`), the ranking model (`rerank_model`), and the analysis chat model (`analysis_model`). A `web_deep_research` entry has no downstream routes either: it defines the service model, gateway search/read references (`search_model`, `read_model`), GPT Researcher models (`fast_model`, `smart_model`, `strategic_model`, `embedding_model`), and image settings (`image_generation_model`, `image_generation_size`). `image_generation_model` always references a gateway model from `images_generations`. When operation routes are saved, the gateway additionally validates cross-section references: `analysis_model`, `fast_model`, `smart_model`, `strategic_model` must be declared in fallback rules, `rerank_model` in `rerank`, `embedding_model` in `embeddings`, `image_generation_model` in `images_generations`, and `search_model` / `read_model` in `web_search` / `web_read`. In the rules editor Web tab all these fields are backed by dropdowns that refresh automatically as the related sections change.
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
python -m pip install -r requirements.txt
python main.py
```

### With UV (preferable)
If uv is installed, it uses dependencies from `pyproject.toml` and creates a Python 3.12+ environment:
```bash
uv venv
uv run main.py
```

### With Docker
if you prefer docker deployment see [this guide](/docker/README.md) (Thanks  [canadaduane](https://github.com/canadaduane)!👍)

The Docker image is built on `python:3.12-slim` and installs dependencies from `requirements.txt`, so dependency updates are included when you rebuild the image.

When using the provided `docker-compose.yml`, the config files `providers.json`, `models_fallback_rules.json`, and `models_operation_rules.json` are mounted read-write so the web editor can save changes directly from inside the container.

### With a systemd service
If you want a local systemd service for running the gateway outside Docker, use the deployment helper:
```bash
sudo sh docker/setup-gateway-service.sh
```

The script uses the current repository as the `WorkingDirectory`, starts the gateway via the local `.venv/bin/python`, and creates the `llm-gateway.service` unit in `/etc/systemd/system` if it does not exist yet. If the unit already exists, the script does not overwrite it and only runs `daemon-reload` + `restart`.

Before running it, make sure `.venv` already exists and the dependencies are installed from `requirements.txt`:
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

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
