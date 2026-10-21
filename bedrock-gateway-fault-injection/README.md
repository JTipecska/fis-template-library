# AWS Fault Injection Service Experiment: Bedrock Gateway Fault Injection (LiteLLM and AgentCore Gateway)

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. It injects Bedrock-accurate transient errors — HTTP 503 `ServiceUnavailableException`, 429 `ThrottlingException`, and 529 `overloaded_error` — into the upstream calls made by an LLM gateway in front of Amazon Bedrock, so you can validate that the gateway's and the application's retry, fallback, and error-translation logic behave as intended. It targets two gateway shapes: self-hosted **LiteLLM** and AWS-managed **Amazon Bedrock AgentCore Gateway**.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Hypothesis

When a configurable share of our Bedrock calls return transient 503 / 429 / 529 errors, our gateway and application remain available: retries with backoff succeed, fallbacks to an alternative model or geographic inference profile engage, and users do not see hard failures. Cross-region inference (CRIS) does not by itself move traffic off an impaired placement — our own resilience logic is what recovers the request.

## How it works

The fault is injected **without changing the calling application**. The application keeps calling its gateway exactly as before. Only the gateway's *upstream endpoint* is pointed at a small provider-agnostic injector container for the duration of the experiment. The injector reads its on/off state from AWS SSM Parameter Store (`/bedrock-fis/{InjectorId}/*`), which an FIS-orchestrated SSM Automation document toggles with guaranteed rollback.

```
application --> gateway (LiteLLM or AgentCore) --> [injector] --> Amazon Bedrock
                                                      ^
                          FIS -> SSM Automation -> SSM Parameter Store
                          (/bedrock-fis/{InjectorId}/active, fault_rate, fault_type, ...)
```

The injector **fails open**: while inactive, or if SSM is unreachable, every request passes through untouched. SSM can only *add* faults, never cause an outage of its own. Each faulted response carries `x-fault-injected` and `x-fault-execution-id` headers for correlation.

### Files

| File | Purpose |
|---|---|
| `bedrock-gateway-fault-injection-experiment-template.json` | FIS experiment template |
| `bedrock-gateway-fault-injection-automation.yaml` | SSM Automation document (enable → hold → disable, with rollback) |
| `bedrock-gateway-fault-injection-fis-role-iam-policy.json` | IAM policy for the FIS experiment role |
| `bedrock-gateway-fault-injection-ssm-automation-role-iam-policy.json` | IAM policy for the SSM Automation role |
| `bedrock-gateway-fault-injection-injector-task-role-iam-policy.json` | IAM policy for the injector container's task role (reads its own SSM subtree) |
| `fis-iam-trust-relationship.json` / `ssm-iam-trust-relationship.json` | Trust policies for the above roles |
| `injector/` | The provider-agnostic injector container (`app.py`, `Dockerfile`, `requirements.txt`) |

## Prerequisites

Before running this experiment, ensure that:

1. You have the necessary permissions to execute the FIS experiment and to run SSM Automation.
2. The IAM role in the experiment template `roleArn` has the permissions in `bedrock-gateway-fault-injection-fis-role-iam-policy.json`, and the SSM Automation role has those in `bedrock-gateway-fault-injection-ssm-automation-role-iam-policy.json`.
3. You have built and deployed the injector container (see **Deploy the injector**) somewhere your gateway can reach it, with its task role granting the policy in `bedrock-gateway-fault-injection-injector-task-role-iam-policy.json`.
4. Your gateway (LiteLLM or AgentCore Gateway) is wired to the injector (see **Wire your gateway**).
5. You are running in a non-production account, and you have a CloudWatch alarm on your application's error rate or latency to use as a stop condition.

## Deploy the injector

The injector is a small single-process container that runs anywhere a container runs — ECS/Fargate, EKS, EC2, or outside AWS. Build and push it:

```bash
cd injector
docker build -t <YOUR REGISTRY>/bedrock-gateway-fault-injector:latest .
docker push <YOUR REGISTRY>/bedrock-gateway-fault-injector:latest
```

Run it with these environment variables:

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `INJECTOR_ID` | yes | — | Scopes the `/bedrock-fis/{INJECTOR_ID}/` SSM subtree; must equal the automation's `InjectorId`. |
| `PASSTHROUGH_MODE` | no | `forward` | `forward` proxies non-faulted requests to `UPSTREAM_BASE_URL`; `echo` returns a synthetic success (pure drill, no real upstream). |
| `UPSTREAM_BASE_URL` | when `forward` | — | The real upstream the gateway would otherwise call (e.g. the Bedrock OpenAI-compatible endpoint). |
| `RESPONSE_STYLE` | no | `aws` | `aws` emits Bedrock-native `{"__type","message"}` errors; `openai` emits `{"error":{...}}` for OpenAI-compatible gateways. |
| `CONFIG_TTL_SECONDS` | no | `10` | SSM config cache TTL. |
| `UPSTREAM_TIMEOUT` | no | `60` | Upstream forward timeout (seconds). |

The injector's task role needs only read access to its own SSM subtree (`bedrock-gateway-fault-injection-injector-task-role-iam-policy.json`).

## Wire your gateway

You only change the gateway's upstream endpoint for the duration of the experiment. The application is untouched.

### Option 1 — LiteLLM (self-hosted)

Point a model deployment's `api_base` at the injector. In `config.yaml`:

```yaml
model_list:
  - model_name: bedrock-claude
    litellm_params:
      model: bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0
      # Normally the Bedrock endpoint; for the experiment, the injector:
      api_base: http://<INJECTOR HOST>:8080
```

Run the injector with `RESPONSE_STYLE=aws` and `PASSTHROUGH_MODE=forward`, `UPSTREAM_BASE_URL` set to the real Bedrock endpoint LiteLLM would have called. Injecting a 503 **upstream of LiteLLM** lets you verify LiteLLM correctly propagates the retryable error to the client (LiteLLM has had known bugs translating Bedrock 503/429 into its OpenAI/Anthropic error envelope, so this is a meaningful check).

Alternatively, for a drill that does not need a real upstream, set `PASSTHROUGH_MODE=echo`.

### Option 2 — Amazon Bedrock AgentCore Gateway (AWS-managed)

You cannot modify the managed gateway itself, so inject *behind* it: register an inference-provider target (or an HTTP passthrough target) whose endpoint is the injector, and have the injector forward to the real provider. Set `RESPONSE_STYLE=openai` (AgentCore inference targets are OpenAI-compatible on `/v1/chat/completions`) and `PASSTHROUGH_MODE=forward` with `UPSTREAM_BASE_URL` set to the real provider endpoint. The application keeps calling the same AgentCore Gateway endpoint — only the target's upstream is the injector.

## Configure and run the experiment

1. Create the SSM Automation document from `bedrock-gateway-fault-injection-automation.yaml`.
2. In `bedrock-gateway-fault-injection-experiment-template.json`, replace the placeholders (`<YOUR REGION>`, `<YOUR AWS ACCOUNT>`, `<YOUR AUTOMATION DOCUMENT NAME>`, `<YOUR INJECTOR ID>`, role and alarm ARNs). The `documentParameters` control the fault:
   - `FaultType` — `503` | `429` | `529`
   - `FaultRate` — probability in `[0.0, 1.0]` of faulting each request
   - `LatencyMs` — optional delay before the error (exercises timeouts/cooling-off)
   - `DurationSeconds` — how long faults stay active before clean disable
3. Import and run the experiment template using the [FIS Template Library Tooling](https://github.com/aws-samples/fis-template-library-tooling).

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
