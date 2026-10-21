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
- ``PASSTHROUGH_MODE=forward`` (default): proxy the request unchanged to
  ``UPSTREAM_BASE_URL`` and stream the response back. Use when this injector sits
  in front of a real upstream (the gateway's true Bedrock/OpenAI endpoint).
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
class InjectorConfig:
    """A cached snapshot of the injector configuration for one injector id.

    The default-constructed instance is the fail-open pass-through config:
    inactive, no fault rate. Produced from the ``/bedrock-fis/{injector_id}/*``
    SSM parameters.

    Attributes:
        active: Whether fault injection is active for this snapshot.
        fault_rate: Probability in ``[0.0, 1.0]`` that a request is faulted.
        fault_type: One of ``"503" | "429" | "529"``.
        latency_ms: Optional artificial delay (ms) applied before the error, to
            exercise client/gateway timeouts and cooling-off behaviour.
        execution_id: SSM Automation execution id, for per-run correlation.
        fetched_at: ``time.monotonic()`` when this snapshot was fetched.
    """

    active: bool = False
    fault_rate: float = 0.0
    fault_type: str = "503"
    latency_ms: int = 0
    execution_id: str = ""
    fetched_at: float = 0.0

    @classmethod
    def passthrough(cls, fetched_at: Optional[float] = None) -> "InjectorConfig":
        """Return the fail-open pass-through configuration."""
        return cls(fetched_at=time.monotonic() if fetched_at is None else fetched_at)


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

        return InjectorConfig(
            active=active,
            fault_rate=fault_rate,
            fault_type=fault_type,
            latency_ms=latency_ms,
            execution_id=params.get("execution_id", ""),
            fetched_at=fetched_at,
        )


# ---------------------------------------------------------------------------
# Forwarding (pass-through) of non-faulted requests
# ---------------------------------------------------------------------------
def _forward_upstream(upstream_base: str, path: str, timeout: float) -> Response:
    """Forward the current request unchanged to the real upstream and relay the response.

    Preserves method, path, query string, body and headers (minus hop-by-hop and
    Host, which are set for the upstream connection). This keeps any upstream
    SigV4/bearer auth that the gateway already applied intact, because the body
    and signed headers are passed through byte-for-byte.
    """
    url = upstream_base.rstrip("/") + path
    if request.query_string:
        url = f"{url}?{request.query_string.decode('utf-8')}"

    # Strip hop-by-hop headers and Host; everything else (incl. Authorization /
    # x-amz-* signing headers the gateway set) is forwarded untouched.
    skip = {"host", "content-length", "connection", "keep-alive", "transfer-encoding"}
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}

    body = request.get_data()
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


# ---------------------------------------------------------------------------
# Flask application factory
# ---------------------------------------------------------------------------
def create_app(loader: Optional[SSMConfigLoader] = None) -> Flask:
    """Build the Flask app.

    Environment:
        INJECTOR_ID      : scopes the /bedrock-fis/{injector_id}/ SSM subtree. Required.
        UPSTREAM_BASE_URL: real upstream to forward pass-through traffic to
                           (required when PASSTHROUGH_MODE=forward).
        PASSTHROUGH_MODE : "forward" (default) | "echo".
        RESPONSE_STYLE   : "aws" (default) | "openai" — error/echo envelope shape.
        CONFIG_TTL_SECONDS: SSM cache TTL (default 10).
        UPSTREAM_TIMEOUT : upstream forward timeout seconds (default 60).
    """
    app = Flask(__name__)

    injector_id = os.environ.get("INJECTOR_ID", "").strip()
    passthrough_mode = os.environ.get("PASSTHROUGH_MODE", "forward").strip().lower()
    response_style = os.environ.get("RESPONSE_STYLE", "aws").strip().lower()
    upstream_base = os.environ.get("UPSTREAM_BASE_URL", "").strip()
    ttl = int(os.environ.get("CONFIG_TTL_SECONDS", "10"))
    upstream_timeout = float(os.environ.get("UPSTREAM_TIMEOUT", "60"))

    if loader is None:
        if not injector_id:
            raise RuntimeError("INJECTOR_ID must be set to scope the SSM parameter subtree")
        loader = SSMConfigLoader(f"/bedrock-fis/{injector_id}/", ttl=ttl)

    @app.get("/healthz")
    def healthz() -> Response:
        """Liveness probe — always 200, never faulted, never forwarded."""
        return Response(json.dumps({"status": "ok"}), status=200, content_type="application/json")

    @app.get("/injector-status")
    def injector_status() -> Response:
        """Report the current (cached) injector config for observability/demo."""
        cfg = loader.get()
        return Response(
            json.dumps(
                {
                    "active": cfg.active,
                    "fault_rate": cfg.fault_rate,
                    "fault_type": cfg.fault_type,
                    "latency_ms": cfg.latency_ms,
                    "execution_id": cfg.execution_id,
                    "passthrough_mode": passthrough_mode,
                    "response_style": response_style,
                }
            ),
            status=200,
            content_type="application/json",
        )

    @app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def proxy(path: str) -> Response:
        """Main proxy entrypoint: fault a sampled share, pass the rest through."""
        cfg = loader.get()

        faulted = False
        if cfg.active and cfg.fault_rate > 0.0:
            # Strict "<" so fault_rate=0 never selects and 1.0 always selects.
            faulted = random.random() < cfg.fault_rate

        if faulted:
            if cfg.latency_ms > 0:
                time.sleep(cfg.latency_ms / 1000.0)
            shape = FAULT_SHAPES[cfg.fault_type]
            body_json, ctype = _error_body(cfg.fault_type, response_style)
            resp = Response(body_json, status=shape["http_status"], content_type=ctype)
            for hk, hv in shape["headers"].items():
                resp.headers[hk] = hv
            resp.headers["x-fault-injected"] = cfg.fault_type
            resp.headers["x-fault-execution-id"] = cfg.execution_id
            logger.info(
                "fault_injected type=%s execution=%s path=/%s",
                cfg.fault_type,
                cfg.execution_id,
                path,
            )
            return resp

        # Pass-through (baseline) traffic.
        if passthrough_mode == "echo":
            return _echo_success(response_style)
        if not upstream_base:
            # Misconfiguration guard: forward mode needs an upstream. Fail open to
            # a clear 200 echo rather than erroring, so the injector never becomes
            # the outage.
            logger.warning("forward mode but no UPSTREAM_BASE_URL; echoing baseline")
            return _echo_success(response_style)
        return _forward_upstream(upstream_base, "/" + path, upstream_timeout)

    return app


# Module-level app for gunicorn: `gunicorn app:app`
# Guarded so importing the module (e.g. for tests) never requires INJECTOR_ID.
app = create_app() if os.environ.get("INJECTOR_ID") else None


if __name__ == "__main__":
    # Local/dev entrypoint only; production uses gunicorn (see Dockerfile).
    _app = app or create_app()
    _app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
