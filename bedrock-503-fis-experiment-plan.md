# bedrock-gateway-fault-injection — experiment plan (matches the built experiment)

Goal: give Mark a demo + test harness that injects Bedrock `ServiceUnavailableException` (503),
`ThrottlingException` (429), and the 529 overloaded signal into a Bedrock-calling workload that
sits behind an LLM gateway, so he can show whether the gateway's and the application's retry /
fallback / region-spread / error-translation logic actually works — **without changing the
application**.

Scope decision (2026-10-21): target **self-hosted LiteLLM** and **AWS-managed AgentCore Gateway**
only. Direct SDK-to-Bedrock injection (DNS/endpoint-redirect of a raw caller) is explicitly **out
of scope** for this build.

## Why gateway-based, and why no application change
Every call already flows through the customer's gateway, so the gateway's upstream endpoint is the
one place we can intercept without touching the application or its configuration. We point that
upstream at a small provider-agnostic injector for the experiment window; the application keeps
calling its gateway exactly as before. We do **not** rely on a native FIS action — FIS has no
Bedrock target and the generic `aws:fis:inject-api-*` actions only accept `ec2`/`kinesis`. FIS's
role here is orchestration: start/stop the SSM automation, enforce the CloudWatch stop condition,
and guarantee rollback.

## Which errors, and why
Per the official Bedrock reactive wording (Harbinger, 2026-10-03), the transient errors a resilient
caller is told to retry are **HTTP 503 and 529**, with backoff+jitter and an attempt cap; 429
`ThrottlingException` is the capacity/quota signal. The injector emits:
- `ServiceUnavailableException` — HTTP 503 (primary ask)
- HTTP 529 — "overloaded" transient signal called out alongside 503
- `ThrottlingException` — HTTP 429 (quota/capacity pressure, the CRIS pinning failure mode)
- optional: `LatencyMs` pre-error delay, to exercise timeouts / cooling-off

## Architecture (as built)
```
application --> gateway (LiteLLM or AgentCore) --> [injector] --> Amazon Bedrock
                                                      ^
                          FIS -> SSM Automation -> SSM Parameter Store
                          (/bedrock-fis/{InjectorId}/active, fault_rate, fault_type, latency_ms, execution_id)
```
- The injector is a small container (Flask + gunicorn) that runs anywhere a container runs —
  ECS/Fargate, EKS, EC2, or outside AWS.
- It **fails open**: inactive, or SSM unreachable, means every request passes through. SSM can only
  *add* faults, never cause an outage of its own.
- Faulted responses carry `x-fault-injected` and `x-fault-execution-id` headers for correlation.
- Two envelope styles: `aws` (`{"__type","message"}`, native Bedrock) and `openai`
  (`{"error":{...}}`, for OpenAI-compatible gateways/targets).
- Two pass-through modes: `forward` (proxy the real upstream, keeps prompt cache warm, lets you
  test the gateway's own 503->envelope translation) and `echo` (pure drill, no real upstream).

## Wiring per gateway (no application change)
- **LiteLLM (self-hosted):** point a model deployment's `api_base` at the injector. Injecting a 503
  *upstream* of LiteLLM also tests whether LiteLLM correctly propagates the retryable error to the
  client (it has had known bugs translating Bedrock 503/429 into its OpenAI/Anthropic envelope),
  which is a sharper resilience check than the app alone.
- **AgentCore Gateway (AWS-managed):** you cannot modify the managed gateway, so inject *behind* it —
  register an inference-provider or HTTP passthrough **target** whose endpoint is the injector; the
  injector forwards to the real provider. Control-plane config only; the app still calls the same
  gateway endpoint.

## Files (as built)
```
bedrock-gateway-fault-injection/
  README.md                                                     # gold-standard structure; both wirings
  AWSFIS.json
  bedrock-gateway-fault-injection-experiment-template.json      # FIS: start SSM automation + CW-alarm stop condition
  bedrock-gateway-fault-injection-automation.yaml               # SSM doc: enable -> hold -> disable, with rollback
  bedrock-gateway-fault-injection-fis-role-iam-policy.json
  bedrock-gateway-fault-injection-ssm-automation-role-iam-policy.json
  bedrock-gateway-fault-injection-injector-task-role-iam-policy.json
  fis-iam-trust-relationship.json
  ssm-iam-trust-relationship.json
  injector/
    app.py                                                      # provider-agnostic fault injector
    Dockerfile
    requirements.txt
```
Note on the three IAM policies: the STYLE_GUIDE shows a single `{experiment}-iam-policy.json`, but
this experiment has three distinct principals (the FIS role, the SSM Automation role, and the
injector's container task role), so it ships one least-privilege policy per principal — the same
multi-policy pattern several existing experiments use (e.g. `database-io-exhaustion`).

## Parameters (automation documentParameters)
- `FaultType` — `503` | `429` | `529`
- `FaultRate` — 0.0–1.0 share of requests faulted
- `LatencyMs` — optional pre-error delay (ms)
- `DurationSeconds` — active window before clean disable
- `InjectorId` — scopes the SSM subtree; must equal the injector `INJECTOR_ID` env var

## Stop conditions & safety
- CloudWatch alarm stop condition on the app's own error rate / p99 latency (don't run open-loop).
- Non-prod account only; confirm the account before deploy.
- Guaranteed cleanup: the SSM doc writes `active=false` first then deletes params on both the happy
  path and on cancel/failure; the injector fails open, so a cancelled run leaves clean pass-through.

## Observability (what the demo should show)
- Client/gateway-side: retry count, success-after-retry, fallback-model / second-profile invocations,
  end-to-end latency.
- CloudTrail `additionalEventData.inferenceRegion` to show which Region served each call
  (demonstrates CRIS does NOT move you off an impaired Region — the core teaching point).
- Bedrock CloudWatch metrics and the injector's own `x-fault-injected` / `x-fault-execution-id`
  headers and `/injector-status` endpoint.

## Demo narrative (ties to the CRIS thread)
1. Baseline: steady traffic through the gateway, warm cache, all served from the pinned Region.
2. Inject 503/429 at the injector. Show CRIS does **not** fail over — the official wording says it
   won't — so the workload keeps hitting the constrained placement.
3. Show the gateway's / app's own resilience (retry/backoff, fallback to a second model or
   geographic profile) is what actually recovers it — resilience is the caller's/gateway's job.
4. (Forward-looking, internal only) this is precisely what the re:Invent first-party Bedrock Gateway's
   retry/fallback/region-spread is meant to automate, and it converges with AgentCore Gateway.

## Verification status
- Static: `app.py` compiles; all JSON valid; automation YAML valid.
- Functional: 11/11 injector smoke tests pass (fail-open pass-through, 503/429/529 status + envelope
  + `Retry-After`/`x-fault-injected` headers, `rate=0` never faults, `/healthz` never faults,
  fail-open on SSM error).
- NOT yet verified: Docker image build, and a live end-to-end run against a real LiteLLM / AgentCore
  Gateway with a real injector task role. Those require a real deploy.
