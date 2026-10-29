# AWS Fault Injection Service Experiment: Bedrock Gateway Egress Fault Injection

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. This experiment template requires deployment into your AWS account and requires resources in your AWS account to inject faults into.

It injects Bedrock-accurate transient errors — HTTP 503 `ServiceUnavailableException`, 429 `ThrottlingException`, and 529 `overloaded_error` — at a temporary gateway provider/upstream target. The normal production path remains a direct gateway-to-Bedrock integration; the proxy is introduced only for the experiment window.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Hypothesis

When a configurable share of Bedrock calls return transient 503 / 429 / 529 errors at the temporary gateway egress target, the application and gateway remain available: retries with backoff succeed, fallbacks to an alternative model or geographic inference profile engage, and users do not see hard failures. When no fault rule matches, the proxy must call the real Bedrock model.

This experiment has separate least-privilege policies for the FIS role, SSM Automation role, and proxy workload role because those principals have different permissions. This follows the repository's existing multi-role experiment pattern; combining those permissions into one policy would weaken least privilege.

## Prerequisites

Before running this experiment, ensure that:

1. You have the necessary permissions to execute the FIS experiment, run SSM Automation, and inspect application and Gateway metrics.
2. The FIS and SSM roles use the policies in this directory.
3. The proxy is deployed on ECS/Fargate or another reachable container runtime and exposes an HTTPS provider endpoint. The proxy must implement `POST /v1/chat/completions` and `GET /v1/models`.
4. The proxy task role has SSM read access to `/bedrock-fis/<InjectorId>/*` and Bedrock invoke access to the approved model or inference-profile ARNs.
5. The selected gateway is configured with the proxy HTTPS endpoint and the same model operation paths used by the application. During the experiment, this target temporarily replaces the direct Bedrock target.
6. You have a CloudWatch alarm on application error rate or latency to use as the FIS stop condition.
7. For customer or production traffic, the proxy endpoint has an approved authentication design. An unauthenticated public endpoint is acceptable only for synthetic non-production proving data.

## How it works

The normal production configuration is a direct gateway-to-Bedrock integration. During the experiment, the selected gateway's provider/upstream endpoint is temporarily changed to the HTTPS ECS/Fargate proxy. The gateway continues using its existing application-facing contract, and the proxy returns a configured fault or creates a fresh SigV4 request to Bedrock.

```text
Normal production:
Application -> AgentCore Gateway -> direct Bedrock provider target -> Bedrock

Experiment window:
Application -> AgentCore Gateway -> direct proxy provider target
                                   -> ECS/Fargate proxy -> Bedrock

FIS -> SSM Automation -> SSM Parameter Store
       /bedrock-fis/{InjectorId}/*
```

The proxy is not an invisible interceptor of the unchanged direct gateway-to-Bedrock connection. It is a temporary provider/upstream substitution. After the experiment, restore the selected gateway's direct Bedrock configuration. The proxy fails open to real Bedrock when inactive or when its SSM control plane is unavailable.

An API Gateway HTTP API with IAM authorization, VPC Link, and a private ALB is **not required by AgentCore or by this experiment**. It is an optional hardened edge variant for environments that require an authenticated/private proxy endpoint. The core experiment requires only an HTTPS endpoint reachable by AgentCore and the provider contract described below.

### Files

| File | Purpose |
|---|---|
| `bedrock-gateway-fault-injection-architecture.md` | Direct-target experiment architecture and optional hardened-edge variant |
| `bedrock-gateway-fault-injection-security-design.md` | Proxy authentication and optional edge security controls |
| `bedrock-gateway-fault-injection-experiment-template.json` | FIS experiment template |
| `bedrock-gateway-fault-injection-automation.yaml` | SSM Automation document (enable → hold → disable, with rollback) |
| `bedrock-gateway-fault-injection-fis-role-iam-policy.json` | IAM policy for the FIS experiment role |
| `bedrock-gateway-fault-injection-ssm-automation-role-iam-policy.json` | IAM policy for the SSM Automation role |
| `bedrock-gateway-fault-injection-injector-task-role-iam-policy.json` | IAM policy for SSM reads and selected Bedrock invocations |
| `bedrock-gateway-fault-injection-agentcore-gateway-role-iam-policy.json` | Optional `execute-api:Invoke` policy for the hardened edge variant |
| `fis-iam-trust-relationship.json` / `ssm-iam-trust-relationship.json` | Trust policies for FIS and SSM |
| `injector/` | ECS/Fargate proxy container (`app.py`, `Dockerfile`, `requirements.txt`) |
| `litellm-test/` | Portable LiteLLM direct/proxy configs, local contract Compose setup, and runtime-independent smoke client |

## Deploy the injector

The injector is a small ECS/Fargate-friendly container. The core experiment does not require API Gateway, VPC Link, or a private ALB. It requires an HTTPS endpoint reachable by AgentCore. For production, use the application's approved HTTPS/authentication edge; for non-production proving, an HTTPS ALB/CloudFront endpoint may be used with synthetic data only.

Build and push the image:

```bash
cd injector
docker build --platform linux/arm64 -t <YOUR ECR REPOSITORY>/bedrock-gateway-fault-injector:<YOUR IMAGE TAG> .
docker push <YOUR ECR REPOSITORY>/bedrock-gateway-fault-injector:<YOUR IMAGE TAG>
```

Create an ECS task definition with:

- A task role containing only SSM read access to `/bedrock-fis/<InjectorId>/*` and the selected Bedrock invoke permissions.
- A separate task execution role for ECR image pulls and CloudWatch Logs.
- Container port `8080` reachable from the HTTPS edge.
- `PASSTHROUGH_MODE=forward`, `UPSTREAM_AUTH_MODE=sigv4`, and `BEDROCK_API_MODE=converse` for current Claude/GPT profile testing.

Run it with these environment variables:

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `INJECTOR_ID` | yes | — | Scopes the `/bedrock-fis/{INJECTOR_ID}/` SSM subtree; must equal the automation's `InjectorId`. |
| `PASSTHROUGH_MODE` | no | `forward` | `forward` proxies non-faulted requests to `UPSTREAM_BASE_URL`; `echo` returns a synthetic success (pure drill, no real upstream). |
| `UPSTREAM_BASE_URL` | when `forward` | — | The real upstream the gateway would otherwise call. For Bedrock Runtime Chat Completions use `https://bedrock-runtime.<region>.amazonaws.com/openai`. |
| `UPSTREAM_AUTH_MODE` | no | `passthrough` | `passthrough` preserves a bearer/non-AWS upstream credential; `sigv4` discards AgentCore's inbound signature and signs the new upstream URL with the injector task role. Use `sigv4` for Bedrock. |
| `BEDROCK_API_MODE` | no | `openai` | `openai` forwards to `UPSTREAM_BASE_URL`; `converse` calls Bedrock Runtime Converse with the task role and translates the response to OpenAI chat format. Use `converse` for current Claude/GPT inference-profile IDs. |
| `UPSTREAM_SIGV4_SERVICE` | when `sigv4` | `bedrock` | AWS SigV4 service name for the upstream. |
| `UPSTREAM_SIGV4_REGION` | when `sigv4` | `AWS_REGION` | Region used to sign the upstream request. |
| `RESPONSE_STYLE` | no | `aws` | `aws` emits Bedrock-native `{"__type","message"}` errors; `openai` emits `{"error":{...}}` for OpenAI-compatible gateways. |
| `CONFIG_TTL_SECONDS` | no | `10` | SSM config cache TTL. |
| `UPSTREAM_TIMEOUT` | no | `60` | Upstream forward timeout (seconds). |
| `MODEL_IDS` | for AgentCore | — | Comma-separated model IDs returned by `GET /v1/models` so AgentCore can discover and synchronize the inference target. |
| `INBOUND_AUTH_MODE` | for restricted LiteLLM traffic | `none` | `bearer` requires a matching `Authorization: Bearer ...` header on provider and status endpoints; `none` is only for isolated non-production proving. |
| `PROXY_API_KEY` | when `INBOUND_AUTH_MODE=bearer` | — | Secret bearer token accepted from LiteLLM. Store it in the runtime secret manager; never commit it or reuse the LiteLLM control-plane key. |

The injector's task role needs read access to its own SSM subtree. When `PASSTHROUGH_MODE=forward` and `UPSTREAM_AUTH_MODE=sigv4` target Bedrock, it also needs `bedrock:InvokeModel` for each model or inference-profile ARN that the test is allowed to invoke. The shipped policy shows the least-privilege current-model matrix used in the live verification: Claude Opus 4.6 and GPT-6.1 Sol inference profiles, plus GPT-OSS 120B, Qwen3 Coder Next, DeepSeek V3.2, and Mistral Large 3. If your selected profile routes across Regions, include the profile ARN and each underlying foundation-model ARN required by the profile.

## Wire your gateway

You only change the gateway's upstream endpoint for the duration of the experiment. The application is untouched.

### Option 1 — LiteLLM (self-hosted)

LiteLLM has two different paths and they must not be conflated:

- **Normal direct Bedrock path:** LiteLLM uses `model: bedrock/<model-id>` (often `bedrock/converse/<model-id>`) and boto3's AWS credential chain to call Bedrock directly.
- **Temporary fault-proxy path:** LiteLLM uses the documented OpenAI-compatible provider with `model: openai/<model-id>`, `api_base` set to the proxy's `/v1` endpoint, and a bearer API key for the proxy.

The application-facing LiteLLM URL and model alias do not change. Only the LiteLLM deployment entry changes for the experiment:

```yaml
# Normal rollback configuration
model_list:
  - model_name: bedrock-claude
    litellm_params:
      model: bedrock/converse/us.anthropic.claude-opus-4-6-v1
      aws_region_name: os.environ/AWS_REGION_NAME

# Temporary experiment configuration, same model_name alias
model_list:
  - model_name: bedrock-claude
    litellm_params:
      model: openai/us.anthropic.claude-opus-4-6-v1
      api_base: https://<PROXY HTTPS ENDPOINT>/v1
      api_key: os.environ/FAULT_PROXY_API_KEY
```

LiteLLM sends `POST /v1/chat/completions` to the proxy. The proxy authenticates the bearer key, applies the SSM rule, and for a non-faulted request creates a new SigV4 Bedrock request with the proxy workload role. It does not reuse LiteLLM's inbound bearer key as an AWS credential. The proxy must run with `INBOUND_AUTH_MODE=bearer`, `PROXY_API_KEY` supplied from a secret, `RESPONSE_STYLE=openai`, and `UPSTREAM_AUTH_MODE=sigv4` when forwarding to Bedrock.

Do not add `api_base` to a `bedrock/...` deployment and assume LiteLLM's boto3 Bedrock client will route through the proxy. The explicit `openai/` provider route is the supported OpenAI-compatible upstream seam. LiteLLM's own deployment runtime—EC2, ECS, or EKS—does not change this YAML. It changes only the network route, secret delivery, and workload identity:

| LiteLLM runtime | Network and secret requirement | Direct-lane IAM | Proxy-lane IAM |
|---|---|---|---|
| EC2 | Private/approved HTTPS route and instance secret delivery | Existing instance-profile Bedrock access | No new LiteLLM Bedrock permission; proxy role invokes Bedrock |
| ECS | Service route/security groups and task secret injection | Existing task-role Bedrock access | No new LiteLLM Bedrock permission; proxy task role invokes Bedrock |
| EKS | Service/Ingress route, NetworkPolicy, and Kubernetes/external Secret | Existing IRSA Bedrock access | No new LiteLLM Bedrock permission; proxy pod/task role invokes Bedrock |

If the direct LiteLLM deployment assumes a separate role, that direct lane still needs `sts:AssumeRole` and the target-role trust policy. That is not required merely to call the proxy. The proxy workload role needs SSM read access to `/bedrock-fis/<InjectorId>/*` and `bedrock:InvokeModel` (plus `bedrock:InvokeModelWithResponseStream` only for streaming), scoped to the selected model/profile ARNs.

The runnable fixtures are in [`litellm-test/`](litellm-test/README.md). Live hosted LiteLLM validation is complete for dedicated private EC2, ECS/Fargate, and EKS/Fargate runtimes in a dedicated non-production AWS account: each runtime passed six real baseline completions, the six-model 503/429/529 matrix, and six-model recovery. LiteLLM preserved HTTP status and body error codes but stripped the proxy's custom fault headers. Runtime-specific findings—including ECS ARM64/GPT-6.1 permissions, EKS x86_64 image and memory requirements, and the non-production private bearer-auth limitation—are recorded in the LiteLLM test README. The public proving edge and unrelated KiroCrew EC2 instances were not used for these runtime tests.

### Option 2 — Amazon Bedrock AgentCore Gateway (AWS-managed)

The normal production target is direct:

```text
Application -> AgentCore Gateway -> direct Bedrock inference provider target -> Bedrock
```

For the experiment window, change only the inference provider target endpoint from Bedrock to the HTTPS ECS/Fargate proxy. The application continues to call the same AgentCore Gateway endpoint, and AgentCore continues to use the same inference-provider contract.

```text
Application -> AgentCore Gateway -> direct proxy inference provider target
                                   -> ECS/Fargate proxy -> Bedrock
```

The proxy must implement the provider operations used by the target:

- `GET /v1/models` for AgentCore model discovery.
- `POST /v1/chat/completions` for OpenAI-compatible inference.
- Any additional provider paths used by the application, such as `/v1/messages`.

For the current Claude and GPT inference-profile IDs, set `BEDROCK_API_MODE=converse`. For models that support the Bedrock OpenAI-compatible endpoint, set `BEDROCK_API_MODE=openai` and `UPSTREAM_BASE_URL=https://bedrock-runtime.<region>.amazonaws.com/openai`. In both cases set `UPSTREAM_AUTH_MODE=sigv4` and `RESPONSE_STYLE=openai`.

Example direct provider target:

```json
{
  "inference": {
    "provider": {
      "endpoint": "https://<PROXY HTTPS ENDPOINT>",
      "operations": [
        {
          "path": "/v1/chat/completions",
          "models": [
            {"model": "us.anthropic.*"},
            {"model": "us.openai.*"},
            {"model": "openai.gpt-oss-*"},
            {"model": "qwen.*"},
            {"model": "deepseek.*"},
            {"model": "mistral.*"}
          ]
        }
      ]
    }
  }
}
```

The proxy endpoint must be HTTPS and reachable by the selected gateway. An API Gateway HTTP API, VPC Link, and private ALB are **optional** edge-hardening components; they are not required by the AgentCore provider-target contract or by the LiteLLM variants.

### Reproduce the real Bedrock forward test

1. Record the existing direct Bedrock provider/upstream configuration for the selected `GatewayType`.
2. Deploy the proxy with `PASSTHROUGH_MODE=forward`, `UPSTREAM_AUTH_MODE=sigv4`, `UPSTREAM_SIGV4_SERVICE=bedrock`, and `RESPONSE_STYLE=openai`. Set `BEDROCK_API_MODE=converse` for current Claude/GPT inference profiles.
3. Configure the selected gateway's temporary provider/upstream endpoint to the proxy HTTPS endpoint. For AgentCore, preserve the provider operation/model mappings and verify `GET /v1/models`; for LiteLLM, preserve the deployment's API contract.
4. Send application requests through the same gateway and require real `chat.completion` responses when no fault rule matches.
5. Write an ordered `rules` list with `fault_rate=1.0`, using disjoint model patterns to map families to 503, 429, and 529. Repeat the same requests and verify the exact status, error envelope, `x-fault-*` headers, `Retry-After`, and configured latency.
6. Set `active=false` and repeat the identical matrix. Require real HTTP 200 Bedrock completions for every model tested.
7. Restore the original direct Bedrock provider/upstream configuration. Leave the proxy resources available until the test owner confirms testing is complete.

The optional hardened edge variant is documented separately and must not be presented as the default gateway architecture:

```text
AgentCore -> API Gateway IAM -> VPC Link -> private ALB -> ECS/Fargate proxy -> Bedrock
```

## Configure and run the experiment

### GatewayType and endpoint setup

Choose one `GatewayType` for each experiment:

| GatewayType | Direct production configuration | Temporary experiment change |
|---|---|---|
| `agentcore` | AgentCore inference provider target -> Bedrock | Set the provider target endpoint to the proxy HTTPS endpoint |
| `llm-proxy-ec2` | LiteLLM on EC2 -> Bedrock | Set the model deployment `api_base`/upstream to the proxy |
| `llm-proxy-ecs` | LiteLLM on ECS -> Bedrock | Set the model deployment `api_base`/upstream to the proxy |
| `llm-proxy-eks` | LiteLLM on EKS -> Bedrock | Set the model deployment `api_base`/upstream to the proxy |

The application remains unchanged. The gateway owner records the original direct Bedrock endpoint, changes the gateway/provider upstream for the experiment window, and restores it afterward. FIS and SSM do not change gateway configuration.

The core setup requires an HTTPS proxy endpoint reachable by the selected gateway. For AgentCore, the proxy supports `GET /v1/models` and the configured inference operation paths. For LiteLLM, configure the same API path and response contract used by the model deployment. API Gateway IAM, VPC Link, and a private ALB are optional edge hardening, not prerequisites.

### The SSM path and `InjectorId`

`/bedrock-fis/{InjectorId}/` is the namespace used by one injector deployment. It is not a
second service or a special AWS-managed path. The container is started with an `INJECTOR_ID`
environment variable; the injector then reads only the matching SSM subtree. The SSM Automation
document receives the same value as its `InjectorId` parameter.

For the live proving-out environment, the value is `ecs-fargate`, so the active subtree is:

```text
/bedrock-fis/ecs-fargate/
```

That subtree currently contains `active`, `rules`, and `execution_id`; the flat-mode parameters
`fault_type`, `fault_rate`, and `latency_ms` are also supported and are written when the automation
runs without per-model rules. For a new deployment, choose a unique value such as
`team-a-gateway` and use it consistently for:

- the injector task's `INJECTOR_ID=team-a-gateway`;
- the FIS/SSM Automation `InjectorId=team-a-gateway`; and
- the SSM inspection or ad-hoc commands at `/bedrock-fis/team-a-gateway/...`.

The injector reads the subtree with a short TTL cache (10 seconds by default). It fails open when
`active` is absent or false, or when SSM cannot be read. The automation writes all settings first,
sets `active=true` last, and writes `active=false` before deleting parameters during normal completion
or rollback.

### One-time setup

The FIS template orchestrates the fault; it does **not** create the gateway, proxy runtime, edge endpoint, IAM roles, or SSM document. Complete these steps once for each test environment:

1. Choose `GatewayType`: `agentcore`, `llm-proxy-ec2`, `llm-proxy-ecs`, or `llm-proxy-eks`. Record the direct Bedrock provider/upstream configuration for rollback.
2. Build and deploy the injector on ECS/Fargate or the customer's approved EC2/ECS/EKS runtime. Give it an HTTPS endpoint reachable by the selected gateway. Do not send customer or production prompts through an unauthenticated public edge.
3. Give the proxy workload identity read access to only `/bedrock-fis/<InjectorId>/*` and invoke access to only the selected Bedrock resources using `bedrock-gateway-fault-injection-injector-task-role-iam-policy.json`.
4. Configure the selected gateway to use the proxy endpoint temporarily:
   - AgentCore: update the direct inference provider target endpoint and preserve the operation/model mappings.
   - LiteLLM on EC2/ECS/EKS: update the model deployment's `api_base` or upstream URL.
5. Create the SSM Automation document and its assume role. The assume role trusts `ssm.amazonaws.com` and uses `bedrock-gateway-fault-injection-ssm-automation-role-iam-policy.json`:

   ```bash
   aws ssm create-document \
     --name <YOUR AUTOMATION DOCUMENT NAME> \
     --document-type Automation \
     --document-format YAML \
     --content file://bedrock-gateway-fault-injection-automation.yaml \
     --region <YOUR REGION>
   ```

   If the document already exists, use `aws ssm update-document` with the same content instead.
6. Create the FIS role using `fis-iam-trust-relationship.json` and
   `bedrock-gateway-fault-injection-fis-role-iam-policy.json`. Replace the placeholders in
   `bedrock-gateway-fault-injection-experiment-template.json`, including `GatewayType`, the
   automation document ARN, role ARNs, account, Region, injector id, and the CloudWatch stop-condition alarm ARN.
7. Import the completed FIS JSON with the [FIS Template Library Tooling](https://github.com/aws-samples/fis-template-library-tooling).

### Run a bounded FIS experiment

The FIS action starts the Automation document. Its `documentParameters` are strings, so `Rules`
must be passed as an escaped JSON string, not as a JSON array nested directly in the outer FIS
JSON. The supported parameters are:

| Parameter | Purpose |
|---|---|
| `GatewayType` | `agentcore` / `llm-proxy-ec2` / `llm-proxy-ecs` / `llm-proxy-eks`; setup context only. The gateway owner changes and restores the endpoint. |
| `InjectorId` | Selects the `/bedrock-fis/{InjectorId}/` subtree; must match `INJECTOR_ID`. |
| `FaultType` | Global fallback fault: `503`, `429`, or `529`. |
| `FaultRate` | Global fallback probability from `0.0` to `1.0`. |
| `LatencyMs` | Global fallback delay before the error, in milliseconds. |
| `Rules` | Optional ordered JSON list of per-model-family rules; first match wins. Empty means flat global mode. |
| `DurationSeconds` | Active fault window before the automation disables and cleans up. |
| `AutomationAssumeRole` | ARN of the SSM Automation assume role. |

A flat-mode example is already present in the template:

```json
"documentParameters": "{\"GatewayType\": \"<YOUR GATEWAY TYPE>\", \"InjectorId\": \"<YOUR INJECTOR ID>\", \"FaultType\": \"503\", \"FaultRate\": \"0.5\", \"LatencyMs\": \"0\", \"Rules\": \"\", \"DurationSeconds\": \"900\", \"AutomationAssumeRole\": \"arn:aws:iam::<YOUR AWS ACCOUNT>:role/<YOUR SSM AUTOMATION IAM ROLE NAME>\"}"
```

To use per-model rules through FIS, replace the empty `Rules` value with an escaped string such as:

```json
"Rules": "[{\"model_pattern\":\"us.anthropic.*\",\"fault_type\":\"503\",\"fault_rate\":\"1.0\"},{\"model_pattern\":\"us.openai.*\",\"fault_type\":\"429\",\"fault_rate\":\"1.0\"},{\"model_pattern\":\"qwen.qwen3-*\",\"fault_type\":\"529\",\"fault_rate\":\"1.0\",\"latency_ms\":\"300\"}]"
```

After importing the template, start it with the FIS CLI or the tooling:

```bash
aws fis start-experiment \
  --experiment-template-id <YOUR EXPERIMENT TEMPLATE ID> \
  --region <YOUR REGION>
```

Inspect the active namespace while testing:

```bash
aws ssm get-parameters-by-path \
  --path /bedrock-fis/<YOUR INJECTOR ID>/ \
  --with-decryption \
  --region <YOUR REGION>
```

For a quick non-FIS harness check, write the same parameters directly with `aws ssm put-parameter`
as shown in **Tuning the fault parameters**. This tests the injector path but does not test FIS
role assumption, stop conditions, or Automation rollback.

## Tuning the fault parameters

The fault is controlled by six SSM parameters under `/bedrock-fis/{InjectorId}/`, which the SSM Automation document writes from its `documentParameters`. You can also set them directly with `aws ssm put-parameter` for ad-hoc testing. The injector re-reads them every `CONFIG_TTL_SECONDS` (default 10), so a change takes effect within one TTL.

| SSM parameter | `documentParameters` key | Values | Meaning |
|---|---|---|---|
| `active` | (set by the automation) | `true` \| `false` | Fault switch. Written `true` last on enable, `false` first on disable. |
| `fault_type` | `FaultType` | `503` \| `429` \| `529` | Which error to emit. |
| `fault_rate` | `FaultRate` | `0.0`–`1.0` | Probability each request is faulted. |
| `latency_ms` | `LatencyMs` | integer ≥ 0 | Delay (ms) applied before the error. |
| `rules` | `Rules` | JSON list | Ordered per-model-family rules; first `model_pattern` match wins. When non-empty, it overrides the flat fault parameters for matched models. |
| `execution_id` | (set by the automation) | string | Correlation id, echoed in the `x-fault-execution-id` header. |

### Which error to inject, and when

| Fault | HTTP | Error shape (`aws` style) | Real-world meaning | Inject it to test |
|---|---|---|---|---|
| **503** | 503 | `ServiceUnavailableException` | Service temporarily out of capacity on shared on-demand infra. The official Bedrock guidance says **retry 503 with exponential backoff + jitter**. | The primary case. That your retry path backs off and recovers, and that a gateway (LiteLLM/AgentCore) propagates the 503 as retryable rather than collapsing it into a generic error. |
| **429** | 429 | `ThrottlingException` | Request exceeded an **account quota** (RPM/TPM) or rate limit. Carries `Retry-After`. | That you honour `Retry-After`, throttle your own send rate, and/or request a quota increase path — not that you hammer harder. |
| **529** | 529 | `overloaded_error` | Model backend temporarily **overloaded** (Anthropic-style); often surfaces to callers as a 503. Guidance pairs it with 503 for retry. | The same retry/backoff path as 503, plus that your client recognises the less-common 529 code. |

Recommended coverage: **always run 503 first** (it is the dominant transient signal and the one customers actually hit), then 429, then 529. 503 and 529 should trigger the same retry/backoff behaviour; 429 should trigger rate-limiting/`Retry-After` behaviour — if your app treats 429 the same as 503 and just retries immediately, that is a finding.

### Rate, latency, and sequencing

- **`fault_rate`** — start low (`0.1`) to observe partial degradation and confirm the healthy fraction still flows, then raise toward `1.0` for a full-outage drill of the pinned placement. `1.0` is the right value to prove a specific error path deterministically; `0.2`–`0.5` is more realistic for a capacity event.
- **`latency_ms`** — set non-zero (e.g. `2000`–`5000`) to exercise client/gateway **timeouts and cooling-off** before the error returns; leave `0` to test pure error handling. Keep it below your client's timeout if you want the error observed, or above it if you specifically want to trigger timeout handling.
- **Duration and sequencing between fault types** — the automation runs a single `FaultType` for `DurationSeconds`, then cleanly disables. To rehearse a **worsening or shifting** incident (e.g. 429 → 503 → 529, or escalating rate), chain several `aws:ssm:start-automation-execution` actions in the FIS template with `aws:fis:wait` actions between them, each invoking the automation with a different `FaultType`/`FaultRate` and a `startAfter` on the preceding wait. This mirrors the staged pattern used by `sqs-queue-impairment`. Keep each stage's `DurationSeconds` within the FIS action `maxDuration` (`PT1H`), and size the gaps (`aws:fis:wait`) to let your retries/alarms react between stages.

A single stage is enough for a basic drill. Use the multi-stage chain only when the demo needs to show behaviour as conditions change over time.

## Current model examples

The following IDs were selected from a live AWS Bedrock model-catalog query. Re-run that query before a future experiment; model availability changes by Region and provider.

| Family | Current example ID | Injector pattern | Suggested fault |
|---|---|---|---|
| Claude | `us.anthropic.claude-opus-4-6-v1` | `us.anthropic.*` | 503 `ServiceUnavailableException` |
| GPT | `us.openai.gpt-6.1-sol` | `us.openai.*` | 429 `ThrottlingException` |
| GPT / open-weight | `openai.gpt-oss-120b-1:0` | `openai.gpt-oss-*` | 529 `overloaded_error` + latency |
| Open-weight: Qwen | `qwen.qwen3-coder-next` | `qwen.qwen3-*` | 503 `ServiceUnavailableException` |
| Open-weight: DeepSeek | `deepseek.v3.2` | `deepseek.*` | 429 `ThrottlingException` |
| Open-weight: Mistral | `mistral.mistral-large-3-675b-instruct` | `mistral.*` | 529 `overloaded_error` + latency |

These are examples for the fault harness, not a claim that every model is enabled for every customer account. Confirm access with the customer's own Bedrock catalog and use inference-profile IDs where the gateway configuration requires them (for example `us.anthropic.claude-opus-4-6-v1` or `us.openai.gpt-6.1-sol`).

## Per-model-family faults (different HTTP status per model family)

A single running injector can return a **different fault for each model family at the same time**. Instead of (or in addition to) the global `fault_type`/`fault_rate`, write a `rules` parameter holding an ordered JSON list; for each request the injector reads the `model` field (from the JSON body, or the `/model/{id}/invoke` path on the Bedrock native API) and applies the **first rule whose `model_pattern` matches** (case-insensitive glob). A request whose model matches no rule is never faulted.

```bash
# Claude -> 503, GPT profile -> 429, GPT-OSS/Qwen/DeepSeek/Mistral mapped independently, everything else passes through
aws ssm put-parameter --name /bedrock-fis/<InjectorId>/active --value true --type String --overwrite
aws ssm put-parameter --name /bedrock-fis/<InjectorId>/rules --type String --overwrite --value '[
  {"model_pattern":"us.anthropic.*",                 "fault_type":"503","fault_rate":1.0},
  {"model_pattern":"us.openai.*",                   "fault_type":"429","fault_rate":1.0},
  {"model_pattern":"openai.gpt-oss-*",              "fault_type":"529","fault_rate":1.0,"latency_ms":300},
  {"model_pattern":"qwen.qwen3-*",                  "fault_type":"503","fault_rate":1.0},
  {"model_pattern":"deepseek.*",                    "fault_type":"429","fault_rate":1.0},
  {"model_pattern":"mistral.*",                     "fault_type":"529","fault_rate":1.0,"latency_ms":300}
]'
```

Each rule field: `model_pattern` (glob, required), `fault_type` (`503`/`429`/`529`, required), `fault_rate` (`0.0`–`1.0`, default `1.0`), `latency_ms` (default `0`). First match wins, so order from most specific to most general and put a trailing `{"model_pattern":"*", ...}` only if you want a catch-all.

Faulted responses carry `x-fault-model` (the model that was matched) and `x-fault-matched-pattern` (which rule fired) headers, alongside `x-fault-injected` and `x-fault-execution-id`, so you can see exactly why each request was faulted. If `rules` is absent, the injector uses the flat global `fault_type`/`fault_rate` for all models (backward compatible). `GET /injector-status` echoes the active rule set.

### Multiple parallel executions

Because each rule is scoped to a model-family pattern, several fault campaigns can run **concurrently against the same injector** without interfering — each owns a disjoint slice of model space. Two ways to drive this:

- **One `rules` list, many families (simplest).** Put every family's rule in the single `rules` parameter, as above. The injector evaluates all of them per request. This is one experiment that faults several families differently at once.
- **Several FIS experiments / SSM writers in parallel.** Give each team or experiment its **own injector** (its own `InjectorId` ⇒ its own `/bedrock-fis/{InjectorId}/*` subtree and its own gateway wiring), and run their FIS experiments simultaneously. Experiment A faults `us.anthropic.*` with 503 while experiment B faults `us.openai.*` with 429, each with independent start/stop/duration and its own stop-condition alarm. Use separate injectors (not one shared `rules` key) when the campaigns must start and stop independently, since concurrent writers to a single `rules` parameter would overwrite each other.

The demo value: show a multi-model application degrade **asymmetrically** — e.g. Claude traffic throws 503 and should fail over to a second profile, while Nova traffic throws 429 and should back off — proving each path's resilience independently and at the same time.

## Direct provider-target live verification

Verified in a dedicated non-production AWS account and Region using a separate AgentCore Gateway and a direct provider target:

```text
Application -> AgentCore Gateway -> direct inference provider target
                                   -> HTTPS ECS/Fargate proxy -> Bedrock Runtime
```

The direct target used a temporary HTTPS endpoint; no API Gateway, VPC Link, or private ALB was used in this target path. The target reached `READY` after model discovery through `GET /v1/models`.

With the proxy inactive, a real `openai.gpt-oss-120b-1:0` request returned HTTP 200 `chat.completion`. With the SSM rules active, the same direct target returned:

- GPT-OSS -> HTTP 503 `ServiceUnavailableException`
- Qwen3 -> HTTP 429 `ThrottlingException`
- DeepSeek -> HTTP 529 `overloaded_error`

The responses included the expected fault headers and `Retry-After`. The public proving service was restored to its safe echo-mode task definition after the test; the direct-test Gateway and target resources remain available. No resources were deleted.

## Optional hardened edge


When the selected `GatewayType` requires a private authenticated proxy endpoint, API Gateway HTTP API with AWS_IAM authorization, VPC Link, and a private ALB is one implementation:

```text
Gateway -> API Gateway IAM -> VPC Link -> private ALB -> ECS/Fargate proxy -> Bedrock
```

The core direct-target experiment does not require this edge. The selected gateway still needs an HTTPS proxy endpoint and an approved authentication design for restricted traffic.

Verified in a dedicated non-production AWS account and Region:

```text
Signed application request
  -> AgentCore Gateway
  -> API Gateway HTTP API (AWS_IAM)
  -> VPC Link
  -> private ALB
  -> ECS/Fargate proxy
  -> Bedrock Runtime
```

The authenticated lane used an API Gateway HTTP API with IAM authorization, a VPC Link, a private ALB, and an ECS service.
- Injector namespace: `/bedrock-fis/ecs-auth/`
- AgentCore Gateway target: `current-model-injector`, status `READY`
- AgentCore target endpoint: the API Gateway root, not the old CloudFront endpoint

An unsigned request to the API Gateway route returned HTTP 403 before reaching the proxy. AgentCore then synchronized successfully after the proxy exposed `GET /v1/models` from the explicit `MODEL_IDS` environment variable.

With faults disabled, all six current model examples returned real HTTP 200 `chat.completion` responses through AgentCore and Bedrock:

| Model family | Model | Result |
|---|---|---:|
| Anthropic | `us.anthropic.claude-opus-4-6-v1` | 200 real completion |
| OpenAI | `us.openai.gpt-6.1-sol` | 200 real completion; 16 output tokens required |
| OpenAI open-weight | `openai.gpt-oss-120b-1:0` | 200 real completion |
| Qwen | `qwen.qwen3-coder-next` | 200 real completion |
| DeepSeek | `deepseek.v3.2` | 200 real completion |
| Mistral | `mistral.mistral-large-3-675b-instruct` | 200 real completion |

The deterministic authenticated fault matrix then returned:

| Model family | Expected | Actual |
|---|---:|---:|
| Claude Opus 4.6 | 503 | 503 `ServiceUnavailableException` |
| GPT-6.1 Sol | 429 | 429 `ThrottlingException` |
| GPT-OSS 120B | 529 | 529 `overloaded_error` |
| Qwen3 Coder Next | 503 | 503 `ServiceUnavailableException` |
| DeepSeek V3.2 | 429 | 429 `ThrottlingException` |
| Mistral Large 3 | 529 | 529 `overloaded_error` |

Every injected response included `x-fault-injected`, `x-fault-model`, `x-fault-matched-pattern`, `x-fault-execution-id`, and `Retry-After`. The GPT-OSS 529 request also used the configured 300 ms delay and completed in approximately 827 ms end to end.

After setting `/bedrock-fis/ecs-auth/active=false` and waiting for the five-second cache TTL, the identical six requests again returned real HTTP 200 Bedrock completions. This proves authenticated target access, model discovery, real Bedrock forwarding, deterministic fault injection, and recovery through the same AgentCore Gateway path.

The live matrix used direct SSM writes to isolate the request path. The repository SSM Automation and FIS templates are statically validated but were not started through a registered FIS experiment in this account.

The authenticated ECS service, API Gateway, VPC Link, private ALB, NAT gateway, IAM roles, AgentCore target, ECR image, and SSM parameters remain running for continued evaluation. No resources were deleted. The new authenticated fault namespace is left in the safe `active=false` state.

## Injector image specification

The injector is a small, single-process HTTP service. Specs for running it on any container runtime:

| Spec | Value |
|---|---|
| Base image | `public.ecr.aws/docker/library/python:3.12-slim` |
| Runtime | Python 3.12, Flask + gunicorn (`gunicorn app:app`) |
| Listening port | `8080` (override with `PORT`) |
| Architecture | Build for the architecture you will run on. `linux/arm64` (e.g. Graviton / Fargate ARM) and `linux/amd64` are both supported — build natively for your target; cross-arch emulation is not required. |
| CPU / memory | 0.25 vCPU / 0.5 GB is ample; it is I/O-bound on the upstream forward. Scale `GUNICORN_WORKERS`/`GUNICORN_THREADS` (defaults 2 / 8) for higher concurrency. |
| User | Runs as non-root (uid 10001). |
| Health check | `GET /healthz` → `200` (never faulted, never forwarded). Dockerfile `HEALTHCHECK` already wired. |
| Status endpoint | `GET /injector-status` → current cached config (active, fault_type, fault_rate, latency_ms, execution_id) for observability. |
| AWS dependency | Always: read-only access to its own `/bedrock-fis/{INJECTOR_ID}/*` SSM parameters. In `sigv4` forward mode: `bedrock:InvokeModel` on the explicitly allowed Bedrock model ARNs. |
| Fail-open | If SSM is unreachable or `active` is absent/false, every request passes through — the injector can only *add* faults, never cause an outage of its own. |
| Egress | In `forward` mode, outbound to `UPSTREAM_BASE_URL`. In `echo` mode, none. |

Build and run:

```bash
cd injector
# Build natively for your target architecture (example: arm64).
docker build --platform linux/arm64 -t bedrock-gateway-fault-injector:latest .

docker run -p 8080:8080 \
  -e INJECTOR_ID=<your-injector-id> \
  -e AWS_REGION=<your-region> \
  -e PASSTHROUGH_MODE=forward \
  -e UPSTREAM_BASE_URL=https://bedrock-runtime.<your-region>.amazonaws.com/openai \
  -e UPSTREAM_AUTH_MODE=sigv4 \
  -e UPSTREAM_SIGV4_SERVICE=bedrock \
  -e UPSTREAM_SIGV4_REGION=<your-region> \
  -e RESPONSE_STYLE=openai \
  bedrock-gateway-fault-injector:latest
```

Runtime options to pick a host: run it alongside/behind your gateway (ECS/Fargate task, EKS pod, or EC2). Prefer ECS/Fargate for a predictable managed container runtime and stable ALB/CloudFront endpoint; co-locating with the gateway is also appropriate for private LiteLLM deployments.

## Stop Conditions

This template references a placeholder CloudWatch alarm ARN for your application's error-rate or latency alarm as its stop condition; replace it with your own, or set the stop condition `source` to `none` to disable. Because the injector fails open, stopping the experiment (or the automation's rollback) returns the gateway to clean pass-through within the injector's cache TTL.

Stop conditions are based on an AWS CloudWatch alarm based on an operational or 
business metric requiring an immediate end of the fault injection. This 
template makes no assumptions about your application and the relevant metrics 
and does not include stop conditions by default.

## Next Steps
As you adapt this scenario to your needs, we recommend:
1. Creating CloudWatch alarms on your application's end-to-end error rate and p99 latency, and wiring one as the experiment stop condition.
2. Observing retry counts, success-after-retry, and fallback-model / second-profile invocations during the run.
3. Using CloudTrail `additionalEventData.inferenceRegion` to show which Region served each call, demonstrating that CRIS does not fail over off an impaired placement.
4. Starting at a low `FaultRate` and short `DurationSeconds`, then increasing as confidence grows.
5. Running `503` first (the primary transient signal), then `429` and `529`, to cover the full set of errors the official Bedrock guidance says to retry.

## Import Experiment
You can import the json experiment template into your AWS account via cli or aws cdk. For step by step instructions on how, [click here](https://github.com/aws-samples/fis-template-library-tooling).
