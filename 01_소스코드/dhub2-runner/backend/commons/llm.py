"""LLMModel resources -> LangChain chat models."""

from __future__ import annotations

import json
from typing import Any

from .collection import Collection, CollectionError


def resolve_llm(collection: Collection, ref: str, provider: str | None = None) -> dict[str, Any]:
    """The LLMModel spec for ``ref``: a collection ``llm/`` resource by name or id,
    else ``ref`` taken as the provider's own model id. Keys and headers come
    from secrets (``api_key_ref``, ``default_headers_ref``, ``${secret:...}``)."""
    doc = collection.find("llm", ref)
    if doc is None:
        if not provider:
            raise CollectionError(f"llm {ref!r} not found in collection and no provider given")
        return {"name": ref, "provider": provider, "model": ref}
    if doc.get("api_key_env") or doc.get("default_headers_env"):
        raise CollectionError(
            f"llm {doc.get('name')!r}: keys are not read from environment variables; "
            "use api_key_ref: secret://<secret>/<key>"
        )
    spec = collection.expand(dict(doc))
    # Where the model is served can differ in the runner (a model dhub2 deploys
    # itself has no reachable address here): the secret <model>/base_url, when
    # set, replaces the model's base_url without changing the collection.
    name = spec.get("name") or ref
    endpoint = collection.secrets.get(name, "base_url")
    if endpoint:
        spec["base_url"] = endpoint
    elif spec.get("type") == "internal" or (spec.get("deployment") and not spec.get("base_url")):
        raise CollectionError(
            f"llm {name!r} is deployed by dhub2 itself; set the secret {name}/base_url "
            "to an OpenAI-compatible endpoint that serves it"
        )
    if spec.get("api_key_ref"):
        spec["api_key"] = collection.secret_value(spec["api_key_ref"])
    headers = dict(spec.get("default_headers") or {})
    if spec.get("default_headers_ref"):
        # The secret holds a JSON object of header -> value.
        headers.update(json.loads(collection.secret_value(spec["default_headers_ref"])))
    spec["default_headers"] = headers or None
    return spec


def create_chat_model(spec: dict[str, Any], **overrides: Any) -> Any:
    provider = (spec.get("provider") or "openai").lower()
    model = spec.get("model") or spec.get("deployment") or spec.get("name")
    common = {
        "temperature": spec.get("temperature"),
        "max_retries": spec.get("max_retries"),
        "timeout": spec.get("request_timeout"),
    }
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        kwargs = {
            **common,
            "model": model,
            # An OpenAI-compatible server (vLLM, Ollama) may take no key, but the
            # client refuses to start without one.
            "api_key": spec.get("api_key") or ("EMPTY" if spec.get("base_url") else None),
            "base_url": spec.get("base_url") or None,
            "max_tokens": spec.get("max_tokens"),
            "default_headers": spec.get("default_headers"),
            "model_kwargs": spec.get("model_kwargs") or None,
        }
        cls = ChatOpenAI
    elif provider in ("claude", "anthropic"):
        from langchain_anthropic import ChatAnthropic

        kwargs = {
            **common,
            "model": model,
            "api_key": spec.get("api_key"),
            "base_url": spec.get("base_url") or None,
            "max_tokens": spec.get("max_tokens"),
            "default_headers": spec.get("default_headers"),
            "model_kwargs": spec.get("model_kwargs") or None,
        }
        cls = ChatAnthropic
    elif provider in ("gemini", "google"):
        from langchain_google_genai import ChatGoogleGenerativeAI

        kwargs = {
            **common,
            "model": model,
            "google_api_key": spec.get("api_key"),
            "max_output_tokens": spec.get("max_tokens"),
        }
        cls = ChatGoogleGenerativeAI
    else:
        raise CollectionError(f"unsupported llm provider {provider!r}")
    kwargs.update(overrides)
    return cls(**{k: v for k, v in kwargs.items() if v is not None})
