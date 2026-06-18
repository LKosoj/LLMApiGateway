import os


def _build_value(env_name: str, default: str = "") -> str:
    return os.getenv(env_name, default).strip()


def build_metadata() -> dict[str, str]:
    return {
        "version": _build_value("LLMGATEWAY_BUILD_VERSION", "dev") or "dev",
        "sha": _build_value("LLMGATEWAY_BUILD_SHA"),
        "date": _build_value("LLMGATEWAY_BUILD_DATE"),
        "ref": _build_value("LLMGATEWAY_BUILD_REF"),
    }


def build_headers() -> dict[str, str]:
    metadata = build_metadata()
    headers = {
        "X-LLMGateway-Build-Version": metadata["version"],
        "X-LLMGateway-Build-Sha": metadata["sha"],
        "X-LLMGateway-Build-Date": metadata["date"],
        "X-LLMGateway-Build-Ref": metadata["ref"],
    }
    return {name: value for name, value in headers.items() if value}
