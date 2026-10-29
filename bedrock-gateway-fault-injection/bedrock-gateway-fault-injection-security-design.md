# Bedrock Gateway Fault Injection: Security Design

## Security objective

Allow only the intended gateway integration to reach the fault proxy, allow the proxy to invoke only approved Bedrock models, and allow FIS to change only the proxy's bounded fault configuration. The required authentication edge depends on `GatewayType`; the core FIS experiment does not assume API Gateway.

## Gateway parameterization and trust boundaries

`GatewayType` is one of `agentcore`, `llm-proxy-ec2`, `llm-proxy-ecs`, or `llm-proxy-eks`. It identifies who owns the upstream endpoint change:

1. **Application boundary:** the application keeps using its existing gateway endpoint and authentication.
2. **Gateway boundary:** AgentCore changes its inference-provider target endpoint, or LiteLLM changes the model deployment's upstream/API base, for the controlled experiment window.
3. **Proxy edge boundary:** the proxy HTTPS endpoint uses the approved service-to-service authentication design for the gateway. Network reachability alone is not authorization for restricted traffic.
4. **Runtime boundary:** the ECS/Fargate, EC2, or EKS workload identity uses only SSM read and approved Bedrock invoke permissions.
5. **Experiment-control boundary:** FIS assumes its FIS role, starts SSM Automation, and passes the Automation role. The Automation role writes only `/bedrock-fis/{InjectorId}/*`.

## LiteLLM-specific trust boundary

For the LiteLLM variants, the temporary upstream must be an OpenAI-compatible `/v1` endpoint. LiteLLM sends its deployment `api_key` as a bearer token to the proxy. Configure the proxy with `INBOUND_AUTH_MODE=bearer` and a separate `PROXY_API_KEY` secret. The proxy must not log that token or forward it as a Bedrock credential. The proxy strips the inbound `Authorization` header and signs the Bedrock request with its own workload identity.

The direct LiteLLM deployment and the proxy deployment therefore have separate IAM responsibilities:

- Direct lane: EC2 instance profile, ECS task role, or EKS IRSA identity can invoke Bedrock as configured by the customer.
- Proxy lane: the LiteLLM runtime needs network access to the proxy and access to the proxy-key secret, while the proxy runtime needs SSM read and scoped Bedrock invoke permissions.
- If direct LiteLLM uses `aws_role_name`, the LiteLLM identity needs `sts:AssumeRole` and the target role must trust it. This is not required for proxy calls.

## Required IAM roles

### Gateway and proxy authentication

For `agentcore`, configure the documented AgentCore outbound credential provider for the provider target. If the proxy endpoint is restricted, it must validate the configured caller identity. For `llm-proxy-ec2`, `llm-proxy-ecs`, and `llm-proxy-eks`, use the existing approved service-to-service authentication mechanism for that deployment. Do not treat an ALB security group or an allowlisted IP as caller authentication.

The optional API Gateway edge variant uses `execute-api:Invoke` on the AgentCore Gateway execution role and is documented separately. It is not required by the direct provider-target experiment.

### Workload task or pod role

The proxy workload identity requires:

- `ssm:GetParameter`, `ssm:GetParameters`, and `ssm:GetParametersByPath` on `arn:aws:ssm:<YOUR REGION>:<YOUR AWS ACCOUNT>:parameter/bedrock-fis/<YOUR INJECTOR ID>/*`.
- `bedrock:InvokeModel` on the exact foundation-model and inference-profile ARNs under test.
- `bedrock:InvokeModelWithResponseStream` only when streaming is required.
- `kms:Decrypt` only when SSM parameters use a customer-managed KMS key, scoped to that key.

The workload identity must not have `ssm:PutParameter`, `ssm:DeleteParameter`, `iam:*`, `ecs:*`, or broad `bedrock:*` permissions.

### SSM Automation role

The Automation role can write, read, and delete parameters only below the selected `InjectorId` path. It does not modify the gateway configuration, ECS/EC2/EKS service, network controls, or Bedrock permissions.

### FIS experiment role

The FIS role can start, stop, and inspect the named SSM Automation document and read the configured CloudWatch alarm for the stop condition. It can pass only the named SSM Automation role to `ssm.amazonaws.com`.

## Optional hardened edge controls

When a deployment requires a private authenticated proxy endpoint, API Gateway HTTP API with `AWS_IAM`, VPC Link, and a private ALB is one supported implementation. In that variant:

- Use an HTTP API route with `AWS_IAM` authorization.
- Use a VPC Link integration to the private ALB.
- Enable access logging without request bodies or prompts.
- Verify an unsigned request receives 401/403 before testing a signed request.

These controls are not required for the core direct provider-target experiment. Do not add them solely because the gateway is AgentCore.

## Network controls

- Place the proxy workload in the gateway's approved network boundary. For ECS/EKS, prefer private tasks or pods; for EC2, restrict the instance security group and host exposure.
- Require HTTPS at the provider/upstream endpoint.
- Permit inbound traffic only from the gateway or its approved edge, not from arbitrary callers.
- Permit workload egress only to required AWS endpoints, including Bedrock Runtime, SSM, ECR, and CloudWatch Logs. Use VPC endpoints where appropriate.
- Do not use a public unauthenticated forward-mode endpoint for customer or production prompts.

## Fault-safety controls

- `active=false` is the safe state.
- SSM writes `active=true` last and writes `active=false` first during rollback.
- A missing or unreadable SSM configuration fails open to real Bedrock pass-through.
- A malformed rule is ignored or fails closed for that rule; it must not broaden the fault scope.
- Forward mode requires an approved authentication and HTTPS design for the selected `GatewayType`.
- The experiment must have a CloudWatch stop condition tied to application impact.
- The proxy must emit a correlation ID for injected responses without logging prompt content.

## Verification gates

Before enabling forward mode:

1. Confirm the AWS account, Region, and `GatewayType`.
2. Confirm the gateway's direct Bedrock endpoint and save the rollback configuration.
3. Confirm the gateway-specific proxy endpoint is HTTPS and reachable from the gateway.
4. Confirm the proxy workload identity can invoke only the selected Bedrock resources.
5. Confirm a no-fault request returns a real Bedrock completion through the selected gateway.
6. Enable one deterministic fault and confirm the configured 503/429/529 is returned through the selected gateway.
7. Disable the fault and confirm the identical request returns a real Bedrock completion again.
8. Confirm gateway, proxy, SSM, and Bedrock logs contain no prompt or secret values.
9. Restore the direct Bedrock endpoint and keep proving resources until the test owner explicitly confirms completion.

## Verification result

The optional hardened-edge variant in a dedicated non-production AWS account and Region verified unsigned API Gateway denial (HTTP 403), AgentCore `execute-api` synchronization to `READY`, private VPC Link/ALB/ECS routing, six real Bedrock completions, deterministic 503/429/529 responses, and six-model recovery after `active=false`.

Those results validate the proxy's provider contract and fault behavior. The API Gateway/VPC Link/private ALB components are not required for the direct AgentCore provider-target or LiteLLM experiment variants.

## Production change and rollback authority

Changing the selected gateway's direct Bedrock endpoint or provider target to the proxy changes the production data path and requires the application/gateway owner, the normal production change process, and security review. The FIS experiment role must not be used as implicit authorization to make that gateway change.

Rollback is the gateway-specific configuration change back to direct Bedrock plus setting the proxy's `active` parameter to `false`. Resource deletion is separate and is not authorized by this experiment.
