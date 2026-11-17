import asyncio
import ipaddress
import json
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import Field, model_validator

from agent_harness.domain import Agent, Contract, HarnessError, Limits, ModelSelection, Principal, RetryPolicy
from agent_harness.json_codec import decode_object


ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"agent:read", "execution:read", "session:read", "tool:read", "workflow:read", "approval:read"}),
    "operator": frozenset({
        "agent:read", "execution:read", "session:read", "tool:read", "workflow:read",
        "execution:create", "execution:cancel", "session:write", "memory:write", "workflow:create", "approval:read",
    }),
}
ROLE_PERMISSIONS["designer"] = ROLE_PERMISSIONS["operator"] | {"agent:write"}
ROLE_PERMISSIONS["approver"] = ROLE_PERMISSIONS["viewer"] | {"approval:decide"}
ROLE_PERMISSIONS["evaluator"] = ROLE_PERMISSIONS["viewer"] | {"evaluation:read", "evaluation:write"}
ROLE_PERMISSIONS["admin"] = ROLE_PERMISSIONS["designer"] | {"tool:write", "audit:read", "metrics:read", "approval:read", "approval:decide"}
ROLE_PERMISSIONS["admin"] |= {"evaluation:read", "evaluation:write"}
Role = Literal["viewer", "operator", "designer", "admin", "approver", "evaluator"]


class AuthenticationError(HarnessError):
    def __init__(self) -> None:
        super().__init__("unauthenticated", "A valid access token is required.")


@dataclass(frozen=True)
class Identity:
    tenant_id: UUID
    subject: str


@dataclass(frozen=True)
class Access:
    principal: Principal
    role: Role

    def require(self, permission: str) -> None:
        if permission not in ROLE_PERMISSIONS[self.role]:
            raise HarnessError("forbidden", "This operation is not permitted.")


class OIDCConfig(Contract):
    issuer: str = Field(min_length=1, max_length=2048)
    audience: str = Field(min_length=1, max_length=256)
    jwks_url: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def fixed_https_endpoints(self) -> "OIDCConfig":
        for value in (self.issuer, self.jwks_url):
            try:
                parsed = urlparse(value)
                port = parsed.port
            except ValueError:
                raise ValueError("OIDC URLs must be valid HTTPS endpoints.") from None
            if (
                parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or port == 0
            ):
                raise ValueError("OIDC URLs must be HTTPS without credentials, query or fragment.")
        return self


class ServerConfig(Contract):
    oidc: OIDCConfig
    models: dict[str, ModelSelection] = Field(min_length=1, max_length=32)
    limits: Limits = Field(default_factory=Limits)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    trusted_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    tenant_requests_per_minute: int = Field(default=600, ge=1, le=100000)
    subject_requests_per_minute: int = Field(default=120, ge=1, le=10000)


async def resolve_public(host: str, port: int) -> tuple[str, ...]:
    try:
        entries = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise HarnessError("identity_unavailable", "Identity service DNS is unavailable.") from None
    addresses = tuple(dict.fromkeys(str(entry[4][0]) for entry in entries))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise HarnessError("egress_denied", "Identity service must resolve only to public addresses.")
    return addresses


class JWKSClient:
    def __init__(
        self, client: httpx.AsyncClient, config: OIDCConfig, *,
        resolver: Callable[[str, int], Awaitable[tuple[str, ...]]] = resolve_public,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client, self.config, self.resolver, self.clock = client, config, resolver, clock
        self._keys: dict[str, RSAPublicKey] = {}
        self._refreshed = -float("inf")
        self._attempted = -float("inf")
        self._refresh_error: str | None = None
        self._lock = asyncio.Lock()

    async def key(self, kid: str) -> RSAPublicKey:
        if self.clock() - self._refreshed >= 300 or kid not in self._keys:
            async with self._lock:
                needs_refresh = self.clock() - self._refreshed >= 300 or kid not in self._keys
                if needs_refresh and self.clock() - self._attempted >= 30:
                    self._attempted = self.clock()
                    try:
                        await self._refresh()
                    except HarnessError as exc:
                        self._refresh_error = exc.code
                        raise
                    self._refresh_error = None
                if self.clock() - self._refreshed >= 300:
                    raise HarnessError(self._refresh_error or "identity_unavailable", "Fresh identity keys are unavailable.")
                if kid not in self._keys and self._refresh_error is not None:
                    raise HarnessError(self._refresh_error, "Identity key retrieval is temporarily unavailable.")
        if kid not in self._keys:
            raise AuthenticationError()
        return self._keys[kid]

    async def _refresh(self) -> None:
        url = httpx.URL(self.config.jwks_url)
        try:
            async with asyncio.timeout(5):
                addresses = await self.resolver(url.host, url.port or 443)
                if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
                    raise HarnessError("egress_denied", "Identity service resolved to a prohibited address.")
                async with self.client.stream(
                    "GET", url.copy_with(host=addresses[0]),
                    headers={"Host": url.netloc.decode("ascii")},
                    extensions={"sni_hostname": url.host}, follow_redirects=False, timeout=3,
                ) as response:
                    if response.status_code != 200:
                        raise HarnessError("identity_unavailable", "Identity key retrieval failed.")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 262144:
                            raise HarnessError("identity_unavailable", "Identity key response exceeds the limit.")
                        body.extend(chunk)
        except (httpx.RequestError, TimeoutError):
            raise HarnessError("identity_unavailable", "Identity service is unavailable.") from None
        try:
            document = decode_object(body.decode())
            keys = document.get("keys")
            if not isinstance(keys, list) or not 1 <= len(keys) <= 16:
                raise ValueError
            parsed: dict[str, RSAPublicKey] = {}
            for value in keys:
                if not isinstance(value, dict) or value.get("kty") != "RSA" or value.get("use", "sig") != "sig":
                    continue
                if value.get("alg", "RS256") != "RS256":
                    continue
                kid = value.get("kid")
                if not isinstance(kid, str) or not 1 <= len(kid) <= 128 or kid in parsed:
                    raise ValueError
                modulus, exponent = value.get("n"), value.get("e")
                if (
                    not isinstance(modulus, str) or not 1 <= len(modulus) <= 700
                    or not isinstance(exponent, str) or not 1 <= len(exponent) <= 8
                ):
                    raise ValueError
                key = jwt.PyJWK.from_dict(value, algorithm="RS256").key
                if not isinstance(key, RSAPublicKey) or not 2048 <= key.key_size <= 4096:
                    raise ValueError
                parsed[kid] = key
            if not parsed:
                raise ValueError
        except (ValueError, jwt.PyJWTError):
            raise HarnessError("identity_unavailable", "Identity keys violate the configured contract.") from None
        self._keys, self._refreshed = parsed, self.clock()

    async def authenticate(self, authorization: str | None) -> Identity:
        if authorization is None:
            raise AuthenticationError()
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not 1 <= len(token) <= 16384:
            raise AuthenticationError()
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (
                header.get("alg") != "RS256" or header.get("typ") not in {"JWT", "at+jwt"}
                or not isinstance(kid, str) or not 1 <= len(kid) <= 128
                or "jku" in header or "x5u" in header or "jwk" in header or "crit" in header
            ):
                raise AuthenticationError()
            key = await self.key(kid)
            claims = jwt.decode(
                token, key, algorithms=["RS256"], issuer=self.config.issuer, audience=self.config.audience,
                options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub", "tenant_id"]},
            )
            subject = claims["sub"]
            if (
                not isinstance(subject, str) or not 1 <= len(subject) <= 256 or not subject.strip()
                or not isinstance(claims["tenant_id"], str)
            ):
                raise AuthenticationError()
            return Identity(UUID(claims["tenant_id"]), subject)
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError, RecursionError):
            raise AuthenticationError() from None


def narrowed_limits(request: Limits, ceiling: Limits) -> Limits:
    return Limits.model_validate({
        key: min(value, ceiling.model_dump()[key]) for key, value in request.model_dump().items()
    })


SCHEMA_KEYS = frozenset({
    "type", "properties", "required", "items", "additionalProperties", "enum", "const",
    "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "description", "title",
})


def validate_result_schema(schema: dict[str, object]) -> None:
    if len(json.dumps(schema).encode()) > 16384 or schema.get("type") != "object":
        raise ValueError("Output schema must be a bounded object schema.")
    nodes = 0

    def visit(value: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 128 or depth > 8:
            raise ValueError("Output schema exceeds complexity limits.")
        if not isinstance(value, dict):
            raise ValueError("Subschemas must be objects.")
        if not value.keys() <= SCHEMA_KEYS:
            raise ValueError("Unsupported schema keywords, references and patterns are prohibited.")
        properties = value.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("Schema properties must be an object.")
        for child in properties.values():
            visit(child, depth + 1)
        for key in ("items", "additionalProperties"):
            child = value.get(key)
            if isinstance(child, dict):
                visit(child, depth + 1)

    visit(schema, 0)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ValueError("Invalid output schema.") from None


def validate_result(agent: Agent, content: str) -> None:
    if agent.result_schema is None:
        return
    try:
        validate_result_schema(agent.result_schema)
    except ValueError:
        raise HarnessError("invalid_output_policy", "The configured output policy is invalid.") from None
    try:
        value = decode_object(content)
    except ValueError:
        raise HarnessError("output_schema_violation", "Final result must be a valid JSON object.") from None
    if not Draft202012Validator(agent.result_schema).is_valid(value):
        raise HarnessError("output_schema_violation", "Final result violates the required schema.")


def reservation(agent: Agent) -> tuple[int, Decimal]:
    tokens = agent.limits.max_model_calls * agent.limits.context_window_tokens
    cost = max(
        (Decimal(agent.limits.context_window_tokens) * selection.input_usd_per_million
         + Decimal(agent.limits.max_output_tokens) * selection.output_usd_per_million) / Decimal(1000000)
        for selection in (agent.selection, *agent.fallbacks)
    ) * agent.limits.max_model_calls
    return tokens, cost
