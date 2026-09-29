"""AGENTIC STAR Marketplace entry adapter for GOV-C2-086."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider as _SdkEnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class _SdkEnvProvider(SecretProvider):
        def __init__(self, *, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.entry_adapter import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS
from src.graph.graph import Graph
from src.payload_store.payload_store import PayloadStore
from src.services.config_validation import validate_domain_config
from src.services.envelope_validation import validate_internal_envelope
from src.services.payload_broker import ScopedPayloadBroker
from src.services.repository_factory import create_repository


_ROOT = Path(__file__).resolve().parent
_NAMESPACE = "gov"
_AGENT_NAME = "GOV-C2-086"
_PROVIDER_AGENT_NAME = "FacilityInspectionIntakeAgent"
_MARKETPLACE_MODE = "invoke"

_AZURE_OPENAI_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_SCOPE_ENV_KEYS = tuple(STG_DEFAULT_SCOPE_ENVS[key] for key in SCOPE_KEYS)
_ALLOWED_ENVIRONMENT_KEYS = frozenset((*_AZURE_OPENAI_KEYS, *_SCOPE_ENV_KEYS))
_SCOPE_INPUT_KEYS = frozenset(("scope", *SCOPE_KEYS))
_INVOKE_INPUT_KEYS = frozenset(("mode", "as_of", "facility_ids"))


class _AllowlistedEnvironmentProvider(_SdkEnvProvider):  # type: ignore[misc]
    """Expose only this adapter's declared scope and Azure values."""

    def __init__(self) -> None:
        try:
            super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)
        except TypeError:  # pragma: no cover - compatibility with older constructors
            super().__init__()

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_ENVIRONMENT_KEYS:
            return default
        value = super().get(key)
        return value if value is not None else default


def _validate_scope_declaration() -> None:
    if set(STG_DEFAULT_SCOPE_ENVS) != set(SCOPE_KEYS):
        raise RuntimeError("Marketplace scope environment declaration does not match SCOPE_KEYS")
    for env_name in STG_DEFAULT_SCOPE_ENVS.values():
        if (
            not isinstance(env_name, str)
            or not env_name
            or env_name != env_name.strip()
            or "\n" in env_name
            or "\r" in env_name
        ):
            raise RuntimeError("Marketplace scope environment declaration is invalid")


def _scope_from_environment(provider: SecretProvider) -> dict[str, str]:
    """Resolve every declared scope value without trimming or repair."""

    _validate_scope_declaration()
    scope: dict[str, str] = {}
    for key in SCOPE_KEYS:
        env_name = STG_DEFAULT_SCOPE_ENVS[key]
        value = provider.get(env_name)
        if not isinstance(value, str) or not value.strip() or value != value.strip() or "\n" in value or "\r" in value:
            raise ValueError(f"Marketplace scope environment variable {env_name!r} must be a clean non-empty string")
        scope[key] = value
    return scope


def _require_clean(provider: SecretProvider, key: str) -> str:
    value = provider.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint with a scheme and host only"
        )
    return endpoint


class MarketplaceAzureLLM(BaseLLM):  # type: ignore[misc]
    """Resolve Azure connection values from the invocation-bound provider."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _client(self) -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_KEYS}
        _require_azure_endpoint(provider)
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            raise RuntimeError("AzureOpenAIClient is unavailable in this AgentCore build") from exc

        client = cast(
            BaseLLM,
            AzureOpenAIClient(
                {
                    "api_key": values["AZURE_OPENAI_API_KEY"],
                    "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                    "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
                }
            ),
        )
        tools = getattr(self, "_tools", ())
        return cast(BaseLLM, client.bind_tools(list(tools)) if tools else client)

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "MarketplaceAzureLLM":
        return type(self)(tools=tuple(tools))


def _identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
    )


def _timestamp_with_offset(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _marketplace_public_envelope(user_input: str) -> dict[str, Any]:
    """Parse the public JSON contract and force its operation to read-only invoke."""

    if not isinstance(user_input, str) or not user_input.strip():
        raise ValueError("E_TYPE")
    try:
        supplied = json.loads(user_input)
    except json.JSONDecodeError as exc:
        raise ValueError("E_TYPE") from exc
    if not isinstance(supplied, dict):
        raise ValueError("E_TYPE")
    if set(supplied) & _SCOPE_INPUT_KEYS:
        raise ValueError("E_UNKNOWN_KEY")

    public = dict(supplied)
    if set(public) - _INVOKE_INPUT_KEYS:
        raise ValueError("E_UNKNOWN_KEY")
    as_of = public.get("as_of")
    if as_of is not None and not _timestamp_with_offset(as_of):
        raise ValueError("E_TIMESTAMP_FORMAT")
    facility_ids = public.get("facility_ids")
    if facility_ids is not None and (
        not isinstance(facility_ids, list)
        or any(not _identifier(item) for item in facility_ids)
        or len(facility_ids) != len(set(facility_ids))
    ):
        raise ValueError("E_TYPE")
    return public


class MarketplaceGraph(Graph):
    """Marketplace-only read boundary with scope, storage, repository, and Azure wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        environment_provider = _AllowlistedEnvironmentProvider()
        scope = _scope_from_environment(environment_provider)
        merged = dict(config if config is not None else load_agent_config(_ROOT))
        if merged.get("llm") is None:
            merged["llm"] = MarketplaceAzureLLM()
        validated = validate_domain_config(merged)
        repository = create_repository(validated)
        payload_store = ScopedPayloadBroker(
            PayloadStore(
                scope_keys=SCOPE_KEYS,
                ttl_seconds=validated["payload_ttl_seconds"],
            )
        )
        validated["repository"] = repository
        validated["payload_store"] = payload_store
        super().__init__(config=validated)
        self._marketplace_scope = scope
        self._marketplace_repository = repository
        self._marketplace_payload_store = payload_store

    def provision_secrets(self, provider: SecretProvider) -> None:
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        _scope_from_environment(environment_provider)
        for key in _AZURE_OPENAI_KEYS:
            _require_clean(environment_provider, key)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        public = _marketplace_public_envelope(user_input)
        invocation_ctx = ctx if ctx is not None else InvocationContext(session_id=session_id)
        request_session = invocation_ctx.session_id
        caller_id = invocation_ctx.caller_id
        if not _identifier(caller_id) or not request_session:
            raise ValueError("E_INTERNAL_ENVELOPE")

        requested = public.get("facility_ids")
        if requested is None:
            facilities = sorted(
                item["facility_id"] for item in self._marketplace_repository.load_status(self._marketplace_scope)
            )
        else:
            registry_ids = {item["facility_id"] for item in self._marketplace_repository.load_registry()}
            if any(item not in registry_ids for item in requested):
                raise ValueError("E_FACILITY_UNKNOWN")
            facilities = sorted(requested)

        request_clock = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        internal = {
            "mode": _MARKETPLACE_MODE,
            "scope": dict(self._marketplace_scope),
            "request_clock": request_clock,
            "caller_id": caller_id,
            "report_refs": [],
            "facility_id_snapshot": facilities,
            "as_of": public.get("as_of") or request_clock,
            "decisions": [],
            "record_kind": None,
            "rejected": [],
        }
        validate_internal_envelope(internal)
        envelope_ref = self._marketplace_payload_store.put(
            internal,
            scope=self._marketplace_scope,
            session_id=request_session,
            envelope=True,
        )
        return cast(
            dict[str, Any],
            super().invoke(
                envelope_ref,
                session_id=request_session,
                ctx=invocation_ctx,
                input_context={},
            ),
        )


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(_ROOT),
    )


if __name__ == "__main__":
    main()
