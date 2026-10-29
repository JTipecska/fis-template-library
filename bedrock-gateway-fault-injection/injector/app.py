"""bedrock_gateway_fault_injector: a provider-agnostic HTTP fault-injection proxy
that sits between an LLM gateway and Amazon Bedrock, and emits Bedrock-accurate
transient errors (HTTP 503 / 429 / 529) for a configurable share of requests.

It injects faults WITHOUT changing the calling application. The application keeps
calling its gateway exactly as before; only the gateway's *upstream endpoint* is
pointed at this injector for the duration of an experiment:

- LiteLLM (self-hosted): set a model deployment's ``api_base`` to this injector,
  so LiteLLM forwards its upstream Bedrock/OpenAI-compatible calls through here.
- AgentCore Gateway (AWS-managed): register an inference-provider or HTTP
  passthrough *target* whose endpoint is this injector.

The injector is a fine per-request switch driven by AWS SSM Parameter Store
(``/bedrock-fis/{injector_id}/*``), which an FIS-orchestrated SSM Automation
document toggles on and off with guaranteed rollback. Every request is a
pass-through no-op until SSM reports ``active=true``.

Two forwarding sub-designs are supported for pass-through (non-faulted) traffic:
- ``PASSTHROUGH_MODE=forward`` (default): proxy the request to
  ``UPSTREAM_BASE_URL`` and relay the response. Use ``UPSTREAM_AUTH_MODE=passthrough``
  for a LiteLLM/non-AWS upstream, or ``UPSTREAM_AUTH_MODE=sigv4`` for Bedrock so
  the injector task role signs a new request for the Bedrock URL.
- ``BEDROCK_API_MODE=converse``: call Bedrock Runtime Converse with the injector
  task role and translate the result to the OpenAI-compatible response expected
  by an AgentCore inference-provider target. This supports current inference
  profile IDs that the Bedrock OpenAI-compatible endpoint may reject.
- ``PASSTHROUGH_MODE=echo``: do not forward; return a minimal synthetic success.
  Use for a pure fault-drill harness with no real upstream wired, or when the
  gateway itself already owns the real call and this injector is only ever meant
  to fault.

Design stance (matches this library's agentcore-strands-agent-faults module):
fail *open* to pass-through on any control-plane error, so a misconfigured or
unreachable SSM never turns the injector into an outage of its own. The only
thing SSM can do is *add* faults; its absence means no faults.

No AWS credentials are required to construct the app. The SSM client is created
lazily on first config read.
"""

from __future__ import annotations

import fnmatch
import hmac
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

import urllib.request
import urllib.error

from flask import Flask, Response, request

logger = logging.getLogger("bedrock_gateway_fault_injector")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------------------
# Bedrock-accurate error shapes
# ---------------------------------------------------------------------------
# These mirror the wire shape a Bedrock caller (and therefore a gateway sitting
# in front of Bedrock) actually receives, so the gateway's and application's
# own retry / fallback / error-translation logic is exercised authentically.
#
# - 503 ServiceUnavailableException: service temporarily out of capacity. The
#   official Bedrock reactive guidance says retry 503 with backoff + jitter.
# - 429 ThrottlingException: request exceeded an account quota / rate limit.
# - 529: "overloaded" transient signal (Anthropic-style overloaded_error that
#   Bedrock may surface); the official guidance pairs it with 503 for retry.
#
# Each entry carries the HTTP status, the modeled error code / exception type,
# a human message, and the headers a well-behaved service returns.
FAULT_SHAPES: dict[str, dict[str, Any]] = {
    "503": {
        "http_status": 503,
        "error_code": "ServiceUnavailableException",
        "message": (
            "The service is currently unavailable. Please retry your request "
            "after a short delay (exponential backoff with jitter recommended)."
        ),
        "headers": {"Retry-After": "2"},
    },
    "429": {
        "http_status": 429,
        "error_code": "ThrottlingException",
        "message": (
            "Too many requests, please wait before trying again. You have "
            "exceeded the request rate for your account."
        ),
        "headers": {"Retry-After": "5"},
    },
    "529": {
        "http_status": 529,
        "error_code": "overloaded_error",
        "message": "Overloaded. The model backend is temporarily overloaded.",
        "headers": {"Retry-After": "2"},
    },
}

# Valid fault types a caller may configure in SSM.
VALID_FAULT_TYPES = frozenset(FAULT_SHAPES.keys())


def _error_body(fault_type: str, response_style: str) -> tuple[str, str]:
    """Return ``(body_json, content_type)`` for a given fault in a given style.

    ``response_style`` selects how the error envelope is shaped so the fault is
    legible to whichever gateway consumes it:
    - ``aws`` : AWS/Bedrock JSON error ``{"__type": ..., "message": ...}`` — what
      a gateway forwarding a raw Bedrock response would see.
    - ``openai`` : OpenAI-compatible ``{"error": {...}}`` — what a LiteLLM /
      AgentCore inference target speaks on ``/v1/chat/completions``.

    The ``aws`` style is the default because it is the most faithful simulation
    of Bedrock itself; use ``openai`` when the injector is wired as an
    OpenAI-compatible inference target and the gateway expects that envelope.
    """
    shape = FAULT_SHAPES[fault_type]
    if response_style == "openai":
        body = {
            "error": {
                "message": shape["message"],
                "type": shape["error_code"],
                "code": shape["error_code"],
                "param": None,
            }
        }
    else:  # aws / bedrock native
        body = {
            "__type": shape["error_code"],
            "message": shape["message"],
        }
    return json.dumps(body), "application/json"


# ---------------------------------------------------------------------------
# Cached, fail-open SSM-driven configuration
# ---------------------------------------------------------------------------
@dataclass
class FaultRule:
    """One fault rule scoped to a model-family pattern.
    Attributes:
        model_pattern: A glob matched against the request's ``model`` field
            (e.g. ``anthropic.*``, ``openai.gpt-6.*``, ``qwen.qwen3-*``, ``*``).
            Matching is case-insensitive via ``fnmatch``.
        fault_type: One of ``"503" | "429" | "529"``.
        fault_rate: Probability in ``[0.0, 1.0]`` a matching request is faulted.
        latency_ms: Optional delay (ms) before the error for matching requests.
    """

    model_pattern: str
    fault_type: str
    fault_rate: float
    latency_ms: int = 0


@dataclass
class InjectorConfig:
    """A cached snapshot of the injector configuration for one injector id.

    The default-constructed instance is the fail-open pass-through config:
    inactive, no fault rate. Produced from the ``/bedrock-fis/{injector_id}/*``
    SSM parameters.

    Two control modes, both read from SSM and both honoured:

    - **Per-model-family rules** (preferred): the ``rules`` parameter holds a
      JSON list of ``{model_pattern, fault_type, fault_rate, latency_ms}``. For
      each request the injector reads the ``model`` field from the body and
      applies the FIRST rule whose ``model_pattern`` matches. This is what lets
      one running injector return different HTTP statuses for different model
      families at the same time, and lets several parallel experiments each own
      a different family's rule.
    - **Global single rule** (backward-compatible fallback): if ``rules`` is
      absent, the flat ``fault_type`` / ``fault_rate`` / ``latency_ms``
      parameters apply to every request regardless of model.

    Attributes:
        active: Whether fault injection is active for this snapshot.
        fault_rate: Global fallback probability in ``[0.0, 1.0]``.
        fault_type: Global fallback fault type (``"503" | "429" | "529"``).
        latency_ms: Global fallback artificial delay (ms) before the error.
        rules: Ordered per-model-family rules; first match wins. Empty => use
            the global fallback.
        execution_id: SSM Automation execution id, for per-run correlation.
        fetched_at: ``time.monotonic()`` when this snapshot was fetched.
    """

    active: bool = False
    fault_rate: float = 0.0
    fault_type: str = "503"
    latency_ms: int = 0
    rules: tuple[FaultRule, ...] = ()
    execution_id: str = ""
    fetched_at: float = 0.0

    @classmethod
    def passthrough(cls, fetched_at: Optional[float] = None) -> "InjectorConfig":
        """Return the fail-open pass-through configuration."""
        return cls(fetched_at=time.monotonic() if fetched_at is None else fetched_at)

    def match(self, model: str) -> Optional[FaultRule]:
        """Return the effective fault rule for a given model id, or ``None``.

        If ``rules`` is set, returns the first rule whose ``model_pattern``
        matches ``model`` (case-insensitive glob); no match => ``None`` (that
        request is not faulted). If ``rules`` is empty, falls back to the global
        single rule as a synthetic ``*`` rule. A ``fault_rate`` of 0 yields a
        rule that never fires, which ``proxy`` handles.
        """
        if self.rules:
            ml = (model or "").lower()
            for r in self.rules:
                if fnmatch.fnmatch(ml, r.model_pattern.lower()):
                    return r
            return None
        # Backward-compatible global fallback as a catch-all rule.
        return FaultRule("*", self.fault_type, self.fault_rate, self.latency_ms)


class SSMConfigLoader:
    """TTL-cached, fail-open reader for ``/bedrock-fis/{injector_id}/*``.

    Holds a single cached snapshot and a reusable boto3 SSM client. ``get()``
    returns the cache while fresh and otherwise refetches, always failing OPEN
    to a pass-through config rather than raising. Failing open is deliberate: a
    fault injector must never become an outage because its own control plane is
    unreachable.
    """

    def __init__(self, prefix: str, ttl: int = 10, ssm_client: Any = None) -> None:
        self.prefix = prefix
        self.ttl = ttl
        self._ssm = ssm_client
        self._cached: Optional[InjectorConfig] = None

    @property
    def ssm(self) -> Any:
        """Return the boto3 SSM client, creating it lazily on first use."""
        if self._ssm is None:
            import boto3  # imported lazily so construction needs no creds

            self._ssm = boto3.client("ssm")
        return self._ssm

    def get(self) -> InjectorConfig:
        """Return the current config, fetching from SSM only when stale.

        Never raises: on any SSM error or invalid value, returns a cached
        pass-through config (fail open).
        """
        now = time.monotonic()
        if self._cached is not None and (now - self._cached.fetched_at) < self.ttl:
            return self._cached

        try:
            resp = self.ssm.get_parameters_by_path(Path=self.prefix)
            params = {
                p["Name"].rsplit("/", 1)[-1]: p["Value"]
                for p in resp.get("Parameters", [])
            }
        except Exception:
            logger.warning("ssm_read_failed for %s; failing open", self.prefix, exc_info=True)
            self._cached = InjectorConfig.passthrough(fetched_at=now)
            return self._cached

        try:
            cfg = self._build_config(params, now)
        except Exception as exc:
            logger.warning("ssm_config_invalid for %s: %s; failing open", self.prefix, exc)
            cfg = InjectorConfig.passthrough(fetched_at=now)

        self._cached = cfg
        return cfg

    def _build_config(self, params: dict[str, str], fetched_at: float) -> InjectorConfig:
        """Build an ``InjectorConfig`` from raw parameter values, validating each."""
        active = params.get("active", "false").strip().lower() == "true"

        fault_rate = float(params.get("fault_rate", "0.0"))
        if not (0.0 <= fault_rate <= 1.0):
            raise ValueError(f"fault_rate out of range [0.0, 1.0]: {fault_rate}")

        fault_type = params.get("fault_type", "503").strip()
        if fault_type not in VALID_FAULT_TYPES:
            raise ValueError(f"fault_type must be one of {sorted(VALID_FAULT_TYPES)}: {fault_type}")

        latency_ms = int(params.get("latency_ms", "0"))
        if latency_ms < 0:
            raise ValueError(f"latency_ms must be >= 0: {latency_ms}")

        # Optional per-model-family rules. JSON list of objects:
        #   [{"model_pattern":"anthropic.*","fault_type":"503","fault_rate":1.0,"latency_ms":0}, ...]
        # First match wins. Absent/empty => global single-rule fallback.
        rules: tuple[FaultRule, ...] = ()
        rules_raw = params.get("rules", "").strip()
        if rules_raw:
            parsed = json.loads(rules_raw)
            if not isinstance(parsed, list):
                raise ValueError("rules must be a JSON list")
            built = []
            for i, r in enumerate(parsed):
                if not isinstance(r, dict):
                    raise ValueError(f"rules[{i}] must be an object")
                mp = str(r["model_pattern"])
                ft = str(r["fault_type"]).strip()
                if ft not in VALID_FAULT_TYPES:
                    raise ValueError(f"rules[{i}].fault_type must be one of {sorted(VALID_FAULT_TYPES)}: {ft}")
                fr = float(r.get("fault_rate", 1.0))
                if not (0.0 <= fr <= 1.0):
                    raise ValueError(f"rules[{i}].fault_rate out of range [0.0, 1.0]: {fr}")
                lm = int(r.get("latency_ms", 0))
                if lm < 0:
                    raise ValueError(f"rules[{i}].latency_ms must be >= 0: {lm}")
                built.append(FaultRule(mp, ft, fr, lm))
            rules = tuple(built)

        return InjectorConfig(
            active=active,
            fault_rate=fault_rate,
            fault_type=fault_type,
            latency_ms=latency_ms,
            rules=rules,
            execution_id=params.get("execution_id", ""),
            fetched_at=fetched_at,
        )


# ---------------------------------------------------------------------------
# Forwarding (pass-through) of non-faulted requests
# ---------------------------------------------------------------------------
def _message_content_blocks(value: Any) -> list[dict[str, str]]:
    """Translate OpenAI message content into Bedrock Converse text blocks."""
    if isinstance(value, str):
        return [{"text": value}]
    if isinstance(value, list):
        blocks = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                blocks.append({"text": str(item["text"])})
        return blocks
    return []


def _forward_bedrock_converse(model: str, timeout: float, region: str) -> Response:
    """Call Bedrock Converse and translate its response to OpenAI chat format.

    AgentCore inference-provider targets send OpenAI-compatible requests. Current
    Claude and GPT inference profiles are callable through Bedrock Converse even
    when the Bedrock OpenAI-compatible endpoint rejects their model IDs, so this
    adapter keeps the application and AgentCore contract unchanged.
    """
    import boto3
    from botocore.exceptions import ClientError

    try:
        payload = request.get_json(silent=True) or json.loads(request.get_data(cache=True) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        messages = []
        system = []
        for item in payload.get("messages", []):
            if not isinstance(item, dict):
                continue
            blocks = _message_content_blocks(item.get("content"))
            if not blocks:
                continue
            if item.get("role") == "system":
                system.extend(blocks)
            elif item.get("role") in ("user", "assistant"):
                messages.append({"role": item["role"], "content": blocks})
        if not messages:
            raise ValueError("messages must contain at least one user or assistant message")

        inference_config = {}
        max_tokens = payload.get("max_tokens", payload.get("max_completion_tokens"))
        if max_tokens is not None:
            inference_config["maxTokens"] = int(max_tokens)
        if payload.get("temperature") is not None:
            inference_config["temperature"] = float(payload["temperature"])
        if payload.get("top_p") is not None:
            inference_config["topP"] = float(payload["top_p"])
        stop = payload.get("stop")
        if isinstance(stop, str):
            inference_config["stopSequences"] = [stop]
        elif isinstance(stop, list):
            inference_config["stopSequences"] = [str(value) for value in stop]

        kwargs: dict[str, Any] = {"modelId": model, "messages": messages}
        if system:
            kwargs["system"] = system
        if inference_config:
            kwargs["inferenceConfig"] = inference_config

        client = boto3.client("bedrock-runtime", region_name=region or os.environ.get("AWS_REGION"))
        result = client.converse(**kwargs)
        output_message = result.get("output", {}).get("message", {})
        content = "".join(
            str(block.get("text", ""))
            for block in output_message.get("content", [])
            if isinstance(block, dict) and block.get("text") is not None
        )
        stop_reason = result.get("stopReason", "")
        finish_reason = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}.get(
            stop_reason, stop_reason or "stop"
        )
        usage = result.get("usage", {})
        body = {
            "id": f"chatcmpl-bedrock-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }],
            "usage": {
                "prompt_tokens": usage.get("inputTokens", 0),
                "completion_tokens": usage.get("outputTokens", 0),
                "total_tokens": usage.get("totalTokens", 0),
            },
        }
        return Response(json.dumps(body), status=200, content_type="application/json")
    except ClientError as exc:
        error = exc.response.get("Error", {})
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 502))
        body = {"error": {
            "message": error.get("Message", str(exc)),
            "type": error.get("Code", "BedrockError"),
            "code": error.get("Code", "BedrockError"),
            "param": None,
        }}
        return Response(json.dumps(body), status=status, content_type="application/json")
    except Exception as exc:
        logger.warning("bedrock_converse_failed model=%s", model, exc_info=True)
        body = {"error": {"message": str(exc), "type": "invalid_request_error", "code": "invalid_request_error", "param": None}}
        return Response(json.dumps(body), status=400, content_type="application/json")


def _sign_upstream_request(
    url: str,
    body: bytes,
    method: str,
    headers: dict[str, str],
    service: str,
    region: str,
) -> dict[str, str]:
    """Sign a new AWS upstream request with the injector task role."""
    import boto3
    from botocore.awsrequest import AWSRequest
    from botocore.auth import SigV4Auth

    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("No AWS credentials available for upstream SigV4 signing")
    if not region:
        raise RuntimeError("UPSTREAM_SIGV4_REGION or AWS_REGION is required for SigV4 signing")
    aws_request = AWSRequest(method=method, url=url, data=body, headers=headers)
    SigV4Auth(credentials, service, region).add_auth(aws_request)
    return dict(aws_request.headers)


def _forward_upstream(
    upstream_base: str,
    path: str,
    timeout: float,
    auth_mode: str,
    sigv4_service: str,
    sigv4_region: str,
) -> Response:
    """Forward the current request and relay the upstream response.

    ``passthrough`` preserves auth for a LiteLLM/non-AWS upstream. ``sigv4``
    removes AgentCore's inbound signature and signs the new URL with the ECS
    task role, which is required for Amazon Bedrock.
    """
    url = upstream_base.rstrip("/") + path
    if request.query_string:
        url = f"{url}?{request.query_string.decode('utf-8')}"

    # Strip hop-by-hop headers, Host, and the inbound signature. The inbound
    # AgentCore signature covers the injector URL, not the Bedrock URL.
    skip = {
        "host", "content-length", "connection", "keep-alive", "transfer-encoding",
        "authorization", "x-amz-date", "x-amz-security-token",
        "x-amz-content-sha256", "x-amz-target",
    }
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}

    body = request.get_data()
    if auth_mode == "sigv4":
        # Do not sign CloudFront/ALB forwarding headers. They are edge metadata,
        # not part of the application request and can change across hops.
        signable = {"content-type", "accept"}
        fwd_headers = {k: v for k, v in fwd_headers.items() if k.lower() in signable}
        fwd_headers = _sign_upstream_request(
            url, body, request.method, fwd_headers, sigv4_service, sigv4_region
        )
    elif auth_mode != "passthrough":
        raise ValueError(f"UPSTREAM_AUTH_MODE must be passthrough or sigv4: {auth_mode}")

    req = urllib.request.Request(url, data=body, headers=fwd_headers, method=request.method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as upstream_resp:
            resp_body = upstream_resp.read()
            status = upstream_resp.status
            ctype = upstream_resp.headers.get("Content-Type", "application/json")
            return Response(resp_body, status=status, content_type=ctype)
    except urllib.error.HTTPError as e:
        # Relay a real upstream error verbatim — it is a genuine response, not an
        # injector fault.
        return Response(e.read(), status=e.code, content_type=e.headers.get("Content-Type", "application/json"))
    except Exception:
        logger.warning("upstream_forward_failed to %s", url, exc_info=True)
        # Upstream unreachable: surface a 503 so the caller's retry path still
        # sees a transient error rather than a hang.
        body_json, ctype = _error_body("503", os.environ.get("RESPONSE_STYLE", "aws"))
        return Response(body_json, status=503, content_type=ctype)


def _extract_model(path: str) -> str:
    """Best-effort extract the model id from the current request.

    Looks in the JSON body ``model`` field first (OpenAI-compatible chat and
    Bedrock Converse both carry it), then falls back to the URL path, which for
    the Bedrock native API is ``/model/{modelId}/invoke`` (the id is the segment
    after ``model/``, URL-decoded).

    Returns an empty string if no model can be determined, which only matters
    when per-model rules are configured (an empty model matches only a ``*``).
    """
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            # Fall back to parsing the raw body as JSON regardless of the
            # Content-Type header (some clients omit application/json).
            raw = request.get_data(cache=True)
            if raw:
                data = json.loads(raw)
        if isinstance(data, dict) and data.get("model"):
            return str(data["model"])
    except Exception:
        pass
    import urllib.parse as _up

    parts = path.split("/")
    if "model" in parts:
        idx = parts.index("model")
        if idx + 1 < len(parts):
            return _up.unquote(parts[idx + 1])
    return ""


def _echo_success(response_style: str) -> Response:
    """Return a minimal synthetic success for echo mode (no real upstream)."""
    if response_style == "openai":
        body = {
            "id": "chatcmpl-injector-echo",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "[injector echo: baseline OK]"},
                    "finish_reason": "stop",
                }
            ],
        }
    else:
        body = {"output": {"message": {"role": "assistant", "content": [{"text": "[injector echo: baseline OK]"}]}}}
    return Response(json.dumps(body), status=200, content_type="application/json")


def _unauthorized_response() -> Response:
    """Return a generic bearer-auth failure without exposing configuration."""
    response = Response(
        json.dumps({"error": {"message": "authentication required", "type": "authentication_error"}}),
        status=401,
        content_type="application/json",
    )
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


# ---------------------------------------------------------------------------
# Flask application factory
# ---------------------------------------------------------------------------
def create_app(loader: Optional[SSMConfigLoader] = None) -> Flask:
    """Build the Flask app.

    Environment:
        INJECTOR_ID      : scopes the /bedrock-fis/{injector_id}/ SSM subtree. Required.
        UPSTREAM_BASE_URL: real upstream to forward pass-through traffic to
                           (required when PASSTHROUGH_MODE=forward).
        UPSTREAM_AUTH_MODE: "passthrough" (default) or "sigv4" for AWS upstreams.
        BEDROCK_API_MODE: "openai" (default) forwards to UPSTREAM_BASE_URL, or
                           "converse" calls Bedrock Runtime Converse and translates
                           the result to the OpenAI-compatible response style.
        UPSTREAM_SIGV4_SERVICE: AWS SigV4 service name (default "bedrock").
        UPSTREAM_SIGV4_REGION: AWS signing Region (defaults to AWS_REGION).
        PASSTHROUGH_MODE : "forward" (default) | "echo".
        RESPONSE_STYLE   : "aws" (default) | "openai" — error/echo envelope shape.
        CONFIG_TTL_SECONDS: SSM cache TTL (default 10).
        UPSTREAM_TIMEOUT : upstream forward timeout seconds (default 60).
        MODEL_IDS        : comma-separated model ids returned by /v1/models for AgentCore discovery.
        INBOUND_AUTH_MODE: "none" (default) or "bearer" for LiteLLM-to-proxy authentication.
        PROXY_API_KEY    : expected bearer token when INBOUND_AUTH_MODE=bearer; store in a secret.
    """
    app = Flask(__name__)

    injector_id = os.environ.get("INJECTOR_ID", "").strip()
    passthrough_mode = os.environ.get("PASSTHROUGH_MODE", "forward").strip().lower()
    response_style = os.environ.get("RESPONSE_STYLE", "aws").strip().lower()
    upstream_base = os.environ.get("UPSTREAM_BASE_URL", "").strip()
    upstream_auth_mode = os.environ.get("UPSTREAM_AUTH_MODE", "passthrough").strip().lower()
    bedrock_api_mode = os.environ.get("BEDROCK_API_MODE", "openai").strip().lower()
    upstream_sigv4_service = os.environ.get("UPSTREAM_SIGV4_SERVICE", "bedrock").strip()
    upstream_sigv4_region = os.environ.get("UPSTREAM_SIGV4_REGION", os.environ.get("AWS_REGION", "")).strip()
    model_ids = tuple(m.strip() for m in os.environ.get("MODEL_IDS", "").split(",") if m.strip())
    ttl = int(os.environ.get("CONFIG_TTL_SECONDS", "10"))
    upstream_timeout = float(os.environ.get("UPSTREAM_TIMEOUT", "60"))
    inbound_auth_mode = os.environ.get("INBOUND_AUTH_MODE", "none").strip().lower()
    proxy_api_key = os.environ.get("PROXY_API_KEY", "")
    if inbound_auth_mode not in {"none", "bearer"}:
        raise RuntimeError("INBOUND_AUTH_MODE must be none or bearer")
    if inbound_auth_mode == "bearer" and not proxy_api_key:
        raise RuntimeError("PROXY_API_KEY must be set when INBOUND_AUTH_MODE=bearer")

    if loader is None:
        if not injector_id:
            raise RuntimeError("INJECTOR_ID must be set to scope the SSM parameter subtree")
        loader = SSMConfigLoader(f"/bedrock-fis/{injector_id}/", ttl=ttl)

    def require_inbound_auth() -> Optional[Response]:
        """Authenticate protected provider and observability endpoints."""
        if inbound_auth_mode == "none":
            return None
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {proxy_api_key}"
        if not hmac.compare_digest(supplied, expected):
            return _unauthorized_response()
        return None

    @app.get("/healthz")
    def healthz() -> Response:
        """Liveness probe — always 200, never faulted, never forwarded."""
        return Response(json.dumps({"status": "ok"}), status=200, content_type="application/json")

    @app.get("/v1/models")
    def models() -> Response:
        """Return the configured model catalog for AgentCore target discovery."""
        auth_error = require_inbound_auth()
        if auth_error is not None:
            return auth_error
        created = int(time.time())
        return Response(
            json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"id": model_id, "object": "model", "created": created, "owned_by": "bedrock-fis"}
                        for model_id in model_ids
                    ],
                }
            ),
            status=200,
            content_type="application/json",
        )

    @app.get("/injector-status")
    def injector_status() -> Response:
        """Report the current (cached) injector config for observability/demo."""
        auth_error = require_inbound_auth()
        if auth_error is not None:
            return auth_error
        cfg = loader.get()
        return Response(
            json.dumps(
                {
                    "active": cfg.active,
                    "fault_rate": cfg.fault_rate,
                    "fault_type": cfg.fault_type,
                    "latency_ms": cfg.latency_ms,
                    "rules": [
                        {
                            "model_pattern": r.model_pattern,
                            "fault_type": r.fault_type,
                            "fault_rate": r.fault_rate,
                            "latency_ms": r.latency_ms,
                        }
                        for r in cfg.rules
                    ],
                    "execution_id": cfg.execution_id,
                    "passthrough_mode": passthrough_mode,
                    "upstream_auth_mode": upstream_auth_mode,
                    "bedrock_api_mode": bedrock_api_mode,
                    "response_style": response_style,
                    "inbound_auth_mode": inbound_auth_mode,
                }
            ),
            status=200,
            content_type="application/json",
        )

    @app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def proxy(path: str) -> Response:
        """Main proxy entrypoint: fault a sampled share, pass the rest through.

        When per-model-family rules are configured, the effective fault_type /
        fault_rate / latency_ms are chosen by matching the request's model id
        against the rules (first match wins); a request whose model matches no
        rule is never faulted.
        """
        auth_error = require_inbound_auth()
        if auth_error is not None:
            return auth_error
        cfg = loader.get()

        rule = None
        model = _extract_model(path)
        if cfg.active:
            rule = cfg.match(model)

        faulted = False
        if rule is not None and rule.fault_rate > 0.0:
            # Strict "<" so fault_rate=0 never selects and 1.0 always selects.
            faulted = random.random() < rule.fault_rate

        if faulted and rule is not None:
            if rule.latency_ms > 0:
                time.sleep(rule.latency_ms / 1000.0)
            shape = FAULT_SHAPES[rule.fault_type]
            body_json, ctype = _error_body(rule.fault_type, response_style)
            resp = Response(body_json, status=shape["http_status"], content_type=ctype)
            for hk, hv in shape["headers"].items():
                resp.headers[hk] = hv
            resp.headers["x-fault-injected"] = rule.fault_type
            resp.headers["x-fault-model"] = model
            resp.headers["x-fault-matched-pattern"] = rule.model_pattern
            resp.headers["x-fault-execution-id"] = cfg.execution_id
            logger.info(
                "fault_injected type=%s model=%s pattern=%s execution=%s path=/%s",
                rule.fault_type,
                model,
                rule.model_pattern,
                cfg.execution_id,
                path,
            )
            return resp

        # Pass-through (baseline) traffic.
        if passthrough_mode == "echo":
            return _echo_success(response_style)
        if bedrock_api_mode == "converse":
            if not model:
                body = {"error": {"message": "request model is required for Bedrock Converse mode", "type": "invalid_request_error", "code": "invalid_request_error", "param": "model"}}
                return Response(json.dumps(body), status=400, content_type="application/json")
            return _forward_bedrock_converse(model, upstream_timeout, upstream_sigv4_region)
        if not upstream_base:
            # Misconfiguration guard: forward mode needs an upstream. Fail open to
            # a clear 200 echo rather than erroring, so the injector never becomes
            # the outage.
            logger.warning("forward mode but no UPSTREAM_BASE_URL; echoing baseline")
            return _echo_success(response_style)
        return _forward_upstream(
            upstream_base,
            "/" + path,
            upstream_timeout,
            upstream_auth_mode,
            upstream_sigv4_service,
            upstream_sigv4_region,
        )

    return app


# Module-level app for gunicorn: `gunicorn app:app`
# Guarded so importing the module (e.g. for tests) never requires INJECTOR_ID.
app = create_app() if os.environ.get("INJECTOR_ID") else None


if __name__ == "__main__":
    # Local/dev entrypoint only; production uses gunicorn (see Dockerfile).
    _app = app or create_app()
    _app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
