# Каталог free-tier провайдеров

Last checked: 2026-05-20.

Этот файл — справочник для ручной настройки, а не готовый production default. Free-tier лимиты, доступность моделей, ToS и data-retention политики меняются часто, поэтому перед включением провайдера проверьте его текущие правила и задайте явные `upstream_limits` в `providers.json`.

| Провайдер | Что использовать | Что проверить перед включением |
| --- | --- | --- |
| OpenRouter | Free-модели и `:free` variants в OpenAI-compatible API. | Текущий список free-моделей, per-provider routing policy, data policy конкретной модели, лимиты аккаунта. |
| Google Gemini API / AI Studio | Gemini API free tier для тестовой разработки. | Rate limits в Google AI Studio для конкретного проекта и модели, необходимость billing для нужных моделей, data-use условия free tier. |
| Groq | Free plan для быстрых OpenAI-compatible chat routes. | Текущие RPM/RPD/TPM/TPD на странице лимитов аккаунта, доступность нужной модели, response headers с оставшейся квотой. |
| Cloudflare Workers AI | Workers AI на Free plan с daily free allocation. | Ежедневную бесплатную квоту, модельные цены/units, необходимость Workers Paid при превышении бесплатного allocation. |

## Пример provider-записи

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

## Как переносить в gateway

1. Создайте provider вручную во вкладке **Providers** или в `providers.json`.
2. Укажите API-ключ через `${VAR_NAME}`; несколько ключей можно перечислить в переменной окружения через запятую.
3. Для каждой free-tier модели задайте `models.<model>.upstream_limits`, чтобы gateway не путал upstream quota с лимитами virtual API keys клиентов.
4. Добавьте модель в fallback chain вручную и проверьте её во вкладке **Fallback Eval**.
5. Используйте **Suggest Eval Order** только как подсказку. Сохранение порядка должно оставаться ручным решением.

## Источники для проверки

- OpenRouter free models: https://openrouter.ai/collections/free-models
- OpenRouter free variants: https://openrouter.ai/docs/routing/model-variants
- Gemini API rate limits: https://ai.google.dev/gemini-api/docs/rate-limits
- Gemini API pricing/free tier: https://ai.google.dev/gemini-api/docs/pricing
- Groq rate limits: https://console.groq.com/docs/rate-limits
- Cloudflare Workers AI pricing: https://developers.cloudflare.com/workers-ai/platform/pricing/
