# This file marks llm_gateway_core/api/v1 as a Python package
# and can be used to aggregate routers from this version.

from fastapi import APIRouter
from .chat import router as chat_router, anthropic_router, responses_router
from .models import router as models_router # Import the new models router
from .embeddings import router as embeddings_router
from .images import router as images_router
from .audio import router as audio_router
from .pdf import router as pdf_router
from .web import router as web_router
from .admin_api_keys import admin_api_keys_router
from .quota import quota_router

# Aggregate all routers for v1
router = APIRouter()
router.include_router(chat_router, prefix="/chat", tags=["Chat Completions V1"])
# Support both /v1/chat/completions and /v1/v1/chat/completions
router.include_router(chat_router, prefix="/v1/chat", tags=["Chat Completions V1 (Double Prefix Compat)"])
router.include_router(responses_router, tags=["OpenAI Responses V1"])
router.include_router(anthropic_router, tags=["Anthropic Messages V1"])
# Support both /v1/messages and /v1/v1/messages for compatibility with some SDKs
router.include_router(anthropic_router, prefix="/v1", tags=["Anthropic Messages V1 (Double Prefix Compat)"])
router.include_router(models_router, prefix="/models", tags=["Models V1"]) # Include the models router
# Support both /v1/models and /v1/v1/models
router.include_router(models_router, prefix="/v1/models", tags=["Models V1 (Double Prefix Compat)"])
router.include_router(embeddings_router, tags=["Embeddings V1"])
router.include_router(images_router, tags=["Images V1"])
router.include_router(audio_router, tags=["Audio V1"])
router.include_router(pdf_router, tags=["PDF V1"])
router.include_router(web_router, tags=["Web V1"])
router.include_router(admin_api_keys_router, tags=["Admin API Keys V1"])
router.include_router(quota_router, prefix="", tags=["Quota V1"])

# You could add other v1 routers here
