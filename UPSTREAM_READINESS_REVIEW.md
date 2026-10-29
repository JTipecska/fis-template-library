# Upstream Readiness Review — fis-template-library fork

Review of this fork against the public [aws-samples/fis-template-library](https://github.com/aws-samples/fis-template-library) repo and its [STYLE_GUIDE.md](STYLE_GUIDE.md), to decide what to contribute upstream and what to share with customers.

Reviewed on branch `fis-ssm-ecs-fargate-az-evacuation`.

## Summary

This fork carries **7 experiment directories that are not on `origin/main`**. Six already have upstream PR numbers in their commit history (#30–#45) — they are in flight and need no new action here. **One, `ecs-fargate-az-evacuation`, has no dedicated PR** and is the genuine candidate to prepare for merge/sharing.

## What is net-new in this fork vs. origin/main

| Directory | Upstream PR? | Status | Action |
|---|---|---|---|
| `ecs-fargate-az-evacuation` | **None** | Local only (lives on the `fix/issue-38-...` branch + its remote copy) | **Submit as its own PR** |
| `agentcore-strands-agent-faults` | #39, #45 | In flight upstream | None |
| `database-blocking-locks` | #35 | In flight upstream | None |
| `database-io-exhaustion` | #37, #45 | In flight upstream | None |
| `eks-automode-az-impairment` | #36, #45 | In flight upstream | None |
| `elasticache-az-power-interruption` | #30 | In flight upstream | None |
| `msk-broker-reboot` | #34 | In flight upstream | None |

## Recommendation

**Contribute `ecs-fargate-az-evacuation` as a standalone upstream PR.** It is a distinct, valuable scenario — it drills the *operator/automation evacuation response* to an AZ impairment (a deliberate control-plane change forcing a full service redeploy), which is complementary to the fault-only `ecs-fargate-az-impairment` already upstream. The template is already authored to the gold-standard (`ec2-windows-stop-iis`) format.

### Naming

Keep the directory name **`ecs-fargate-az-evacuation`** — kebab-case, matches the sibling `ecs-fargate-az-impairment`, and all its files already follow the `{experiment-name}-{type}` pattern. No rename needed.

The pairing reads cleanly upstream:
- `ecs-fargate-az-impairment` — inject the AZ fault, observe detection (fault-only).
- `ecs-fargate-az-evacuation` — inject + evacuate the AZ (operator-response drill).

## Style-guide conformance (ecs-fargate-az-evacuation)

Checked against STYLE_GUIDE.md — **passes**:

- ✅ All required files present (README, AWSFIS.json, template JSON, FIS IAM policy, trust relationship) plus SSM automation + SSM role policy + SSM trust (correctly included because it uses `aws:ssm:start-automation-execution`).
- ✅ All JSON valid; SSM automation YAML valid.
- ✅ README has every required section in order: title, template description, exact disclaimer (**ends with a period** — the #45 fix), Hypothesis, Prerequisites, Stop Conditions, Next Steps, Import Experiment.
- ✅ Single combined `## Stop Conditions` section with the exact CloudWatch-alarm paragraph — no forbidden separate `## Observability` heading (the #42 fix).
- ✅ Parameterised with the standard `<YOUR ...>` placeholders; `FIS-Ready=True` tagging; `experimentOptions` present; `stopConditions: [{"source": "none"}]`.
- ✅ IAM policies scoped with `aws:ResourceTag/FIS-Ready: True` conditions (least privilege).

### Minor polish before PR (optional, non-blocking)

1. **`tags` block** in `ecs-fargate-az-evacuation-template.json` uses `"Purpose": "chaos-engineering"` + `"Environment": "test"`, whereas the gold standard uses a `Name`/`Purpose` pair. Harmless, but align with the sibling `ecs-fargate-az-impairment` for consistency if the upstream maintainers prefer uniform tags.
2. **No `images/` diagram** — the README has an excellent ASCII flow diagram, so this is not required. Optional: a rendered PNG of the T+0→T+30m flow would match the richer examples (`cloudfront-impairment`).
3. The experiment is a `fault + control-plane change`; the README already flags this prominently at the top and in the Hypothesis. Keep that framing — it's the differentiator from the fault-only sibling and pre-empts the most likely reviewer question.

## Getting it merge-ready — mechanics

The evacuation work currently sits on `fix/issue-38-ecs-az-impairment-redeploy-doc` tangled with unrelated in-flight experiments. For a clean upstream PR:

1. Branch `fis-ssm-ecs-fargate-az-evacuation` has been created for this work.
2. Open the PR against **`aws-samples/fis-template-library:main`** containing **only** the `ecs-fargate-az-evacuation/` directory — CONTRIBUTING.md asks contributors to focus on the specific change and not bundle unrelated edits.
3. Open a tracking issue first (CONTRIBUTING.md step 4: "open an issue to discuss any significant work").
4. Complete the CONTRIBUTING.md template checklist in the PR description.

> This review file is a working artifact for the fork; **remove it before opening the upstream PR** (it is not part of the experiment and would fail the "focus on the specific change" guidance).

## Customer sharing

For sharing with the customer now (ahead of upstream merge), `ecs-fargate-az-evacuation/` is self-contained and safe to hand over as-is: it already uses placeholder values and carries the full disclaimer, prerequisites, and recovery/stop-condition guidance.
