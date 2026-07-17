# Free-tier provider catalog

Last checked: 2026-05-20.

This file is a reference for manual configuration, not a ready-made production default. Free-tier limits, model availability, terms of service, and data-retention policies change often. Before enabling a provider, verify its current rules and set explicit `upstream_limits` in `providers.json`.

| Provider | What to use | What to verify before enabling |
| --- | --- | --- |
| OpenRouter | Free models and `:free` variants through the OpenAI-compatible API. | The current free-model list, per-provider routing policy, the selected model's data policy, and account limits. |
| Google Gemini API / AI Studio | The Gemini API free tier for test development. | Rate limits in Google AI Studio for the specific project and model, whether billing is required for the selected models, and free-tier data-use terms. |
| Groq | The free plan for fast OpenAI-compatible chat routes. | Current RPM/RPD/TPM/TPD on the account limits page, availability of the required model, and response headers with remaining quota. |
| Cloudflare Workers AI | Workers AI on the Free plan with a daily free allocation. | The daily free quota, model prices/units, and whether Workers Paid is required after the free allocation is exhausted. |

## Provider entry example

```json
{
  "openrouter": {
    "baseUrl": "https://openrouter.ai/api/v1",
    "apikey": "${APIKEY_OPENROUTER}",
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
}
```

## Adding it to the gateway

1. Create the provider manually on the **Providers** tab or in `providers.json`.
2. Set the API key through `${VAR_NAME}`; multiple keys can be listed in the environment variable, separated by commas.
3. Set `models.<model>.upstream_limits` for every free-tier model so the gateway does not confuse upstream quota with client virtual API key limits.
4. Add the model to a fallback chain manually and verify it on the **Fallback Eval** tab.
5. Use **Suggest Eval Order** only as guidance. Saving the order must remain a manual decision.

## Sources to verify

- OpenRouter free models: https://openrouter.ai/collections/free-models
- OpenRouter free variants: https://openrouter.ai/docs/routing/model-variants
- Gemini API rate limits: https://ai.google.dev/gemini-api/docs/rate-limits
- Gemini API pricing/free tier: https://ai.google.dev/gemini-api/docs/pricing
- Groq rate limits: https://console.groq.com/docs/rate-limits
- Cloudflare Workers AI pricing: https://developers.cloudflare.com/workers-ai/platform/pricing/
