# AWS Fault Injection Service Experiment: Ransomware Impact Simulation

This is an experiment template for use with AWS Fault Injection Service (FIS) and fis-template-library-tooling. This experiment template requires deployment into your AWS account and requires resources in your AWS account to inject faults into.

THIS TEMPLATE WILL INJECT REAL FAULTS! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

> **⚠️ BACKUP DELETION WARNING:** This experiment includes an automation document (`fis-security-ransomware-backup-deletion-attempt`) that will **attempt to permanently delete an EBS snapshot and an AWS Backup recovery point**. If your backup vault does **not** have AWS Backup Vault Lock configured, backups **will be deleted with no possibility of recovery**. Verify your vault has Vault Lock enabled in the AWS Backup console **before running this experiment**.

> **⚠️ KMS KEY POLICY WARNING:** This experiment modifies the KMS key policy to deny `kms:Decrypt` to the instance role. The KMS automation document **automatically restores the original policy** after the 60-minute hold expires, or immediately if the experiment is stopped early. If the automation is killed unexpectedly before the restore step runs, use `python scripts/restore_kms_access.py` as a manual safety net.

## Hypothesis

When a ransomware attack renders application data inaccessible — through file encryption, EBS I/O freeze, and S3 network isolation — and simultaneously attempts to destroy backups, an AWS environment protected with AWS Backup Vault Lock will:

1. Block the backup deletion attempt (CloudTrail records `AccessDeniedException` on `DeleteRecoveryPoint`)
2. Retain a clean, restorable recovery point in the vault-locked backup vault
3. Allow the customer to restore the affected data volume from the vault-locked backup within the 60-minute exercise window

## Prerequisites

Before running this experiment, ensure that:

> **Note on helper scripts:** The `seed_backups.py` and `restore_kms_access.py` scripts referenced below are provided by the companion deployment project that provisions the target resources (EBS data volume, backup vault, KMS key, S3 test objects). They are not part of this template-library directory. If you are wiring these templates into your own environment, create the equivalent recovery points and KMS-encrypted test objects yourself, or substitute the equivalent AWS CLI / console steps described in each prerequisite.

1. You have the necessary permissions to start FIS experiments, pause EBS volumes, disrupt VPC connectivity, and run SSM documents.
2. The IAM role specified in `roleArn` has the permissions defined in `security-recovery-ransomware-impact-simulation-fis-iam-policy.json`.
3. The SSM Automation role specified in `documentParameters.AutomationAssumeRole` has the permissions defined in `security-recovery-ransomware-impact-simulation-ssm-automation-iam-policy.json`.
4. An EC2 instance tagged `FIS-Ready=True` and `Name=fis-security-blue1` is running and registered with AWS Systems Manager.
5. A secondary EBS data volume tagged `FIS-Ready=True` and `Name=fis-security-blue1-data` is attached to Blue1 and mounted at `/data`. This is the volume that will have its I/O paused — the root volume is not affected, so SSM remains operational throughout.
6. The VPC subnet containing Blue1 is tagged `FIS-Ready=True`. The `isolate-s3` action adds NACL deny rules to this subnet.
7. **AWS Backup Vault Lock is enabled** on the vault named `fis-security-backup-vault` (governance or compliance mode). Without Vault Lock, backups will be permanently deleted.
8. Run `python scripts/seed_backups.py` to create **two EBS snapshots and two AWS Backup recovery points** (`Candidate=clean` and `Candidate=post-change`) plus 5 KMS-encrypted S3 test objects. The two recovery points simulate a clean backup (predating the attack) and a potentially-contaminated backup (taken during an attacker's dwell period) — you will need to identify the correct one during Phase 4. The daily backup plan runs at 02:00 UTC but `seed_backups.py` creates them immediately.
9. `scripts/restore_kms_access.py` is available as a **manual safety net** if the KMS automation is killed before its restore step runs. Under normal operation (experiment completes or is stopped via the console), the key policy is restored automatically — you do not need to run this script.

**Detection infrastructure (required for a realistic exercise):**

10. **AWS CloudTrail with management events enabled** — the investigation stage depends on CloudTrail. Without it, `DeleteRecoveryPoint AccessDeniedException` events are not queryable. Verify with: `aws cloudtrail get-event-selectors --trail-name <your-trail>`.
11. **Amazon GuardDuty enabled** — GuardDuty generates findings when the experiment injects anomalous API calls. Having it active gives you a real detection feed to work with during Phase 4.
12. **VPC Flow Logs enabled** on the VPC — needed to trace network-level activity during Phase 2 (S3 isolation). Enable from: VPC Console → Your VPC → Flow Logs → Create.

**Prepare before you start:**

13. **Know your restore runbook** — document the steps to find a recovery point, start a restore job, attach the restored volume, and verify data before running the experiment. Phase 4's 60-minute window is not the time to discover the runbook for the first time.
14. **Note your target RTO** — decide in advance how long a restore should take. You will measure your actual time against it during Phase 4.

## How It Works

The experiment runs in four sequential phases over approximately 78 minutes:

### Phase 1 — File Encryption Simulation (t=0, ~3 min)
FIS sends the `fis-security-ransomware-file-encryption` Run Command document to Blue1. The script renames all `.txt` files in `/data/test-files/` to `.enc` and writes a mock ransom note (`RANSOM_NOTE.txt`). No real encryption is performed — the filename rename makes the data inaccessible to the application without destroying it.

### Phase 2 — Storage and Network Lockout (t≈3 min, 75 min duration, runs concurrently)
Two parallel FIS actions start after Phase 1 completes:
- **`pause-ebs`**: All I/O on the `fis-security-blue1-data` volume is frozen using `aws:ebs:pause-volume-io`. Any process trying to read or write `/data` will hang, exactly as if the volume's data were encrypted.
- **`isolate-s3`**: FIS adds NACL deny rules on the victim subnet blocking all traffic to the S3 regional endpoint (`scope: s3`). This simulates ransomware cutting off cloud storage access.

Both faults remain active for 75 minutes — covering the backup deletion phase and the full customer recovery window.

### Phase 3 — Backup Deletion Attempt + KMS Access Revocation (t≈13 min, ~7 min duration, runs concurrently)
After the storage lockout is established, FIS starts the `fis-security-ransomware-backup-deletion-attempt` SSM Automation. It:
1. Discovers the most recent EBS snapshot tagged `FIS-Ready=True` and attempts `ec2:DeleteSnapshot`
2. Discovers the most recent recovery point in `fis-security-backup-vault` and attempts `backup:DeleteRecoveryPoint`

**Expected outcome with Vault Lock enabled:** Both deletion attempts are blocked with `AccessDeniedException`. CloudWatch metrics `RansomwareTestSnapshotProtected` and `RansomwareTestVaultLockProtected` are published to namespace `FISSecurityExperiments`. CloudTrail records the blocked API calls as proof.

**Expected outcome without Vault Lock:** Deletions succeed. Backups are permanently destroyed.

In parallel, FIS starts the `fis-security-ransomware-kms-access-revocation` SSM Automation. It:
1. Saves the current KMS key policy to SSM Parameter Store (`/fis-security/ransomware/kms-policy-backup`)
2. Injects a Deny statement blocking `kms:Decrypt` for the instance role on the `fis-security-s3-kms-key`

From this point, any `s3:GetObject` on objects encrypted with that key returns HTTP 403 (KMS AccessDeniedException). `s3:ListObjects` still works — the objects are visibly present but cryptographically inaccessible, mirroring a real ransomware key-revocation attack.

The SSM Automation for `revoke-kms-access` handles the full fault lifecycle internally: it saves the key policy, applies the Deny, sleeps for 60 minutes (`aws:sleep PT60M`), then restores the original policy. If the FIS experiment is stopped early, the `onCancel` handler on each post-revocation step jumps directly to the restore step. `scripts/restore_kms_access.py` is only needed if the automation is killed before reaching the restore step.

### Phase 4 — Recovery Exercise Window (t≈20 min, 60 min hold — built into KMS automation)

The data volume remains paused, S3 access remains isolated, and KMS decryption remains denied. You have 60 minutes to run through the following recovery stages. Work through them in order — the steps map directly to the [AWS Cyber Resilience reference architecture](https://aws.amazon.com/blogs/architecture/cyber-resilience-on-aws-a-reference-approach-for-recovery-from-ransomware-and-destructive-events/).

#### Stage 1 — Establish Timeline (minutes 0–10)

1. Open **CloudTrail → Event History** and filter by `Event name = DeleteRecoveryPoint`. You should see `AccessDeniedException` events from `fis-security-ransomware-backup-deletion-attempt`. Record the event timestamp — this is your confirmed "attack time."
2. Open **CloudTrail → Event History** and filter by `Event name = DeleteSnapshot`. Confirm the snapshot deletion attempt is also blocked.
3. Open **GuardDuty → Findings**. Check for any anomalous findings generated during Phases 1–3 (e.g., unusual API calls, network activity).
4. Open **VPC → Flow Logs** for the victim subnet. Note the traffic drop to the S3 endpoint that started in Phase 2.

#### Stage 2 — Validate Recovery Point Candidates (minutes 5–15, overlaps Stage 1)

5. Open **AWS Backup → `fis-security-backup-vault` → Recovery points**.
6. You will see two recovery points created by `seed_backups.py`:
   - `Candidate=clean` — created **before** any attack simulation. This is your safe restore candidate.
   - `Candidate=post-change` — created **after** test files were already written. This simulates a backup taken during an attacker's dwell period. **Do not use this one.**
7. Compare the recovery point creation timestamps to the CloudTrail attack time from step 1. The correct candidate predates the attack.
8. Verify the selected recovery point status is `COMPLETED` (not `PARTIAL` or `FAILED`).

#### Stage 3 — Approval Gate (minute 15)

9. In a real incident, a second approver would sign off before any restore proceeds (ideally via MPA through IAM Identity Center — see *Gaps vs. Production Hardening* below). For this exercise: confirm with a colleague who your approver would be and what information they would need before approving.

#### Stage 4 — Restore + Rebuild + Rotate (minutes 15–55)

10. On the `Candidate=clean` recovery point, click **Restore**. Choose the same Availability Zone as the original volume. Note your restore start time.
11. While the restore is running (typically 5–15 min), confirm the KMS lockout is active:
    ```bash
    # This should succeed (S3 list works — objects are visible but inaccessible)
    aws s3 ls s3://<bucket-name>/test-files/

    # This should return 403 KMS AccessDeniedException
    aws s3 cp s3://<bucket-name>/test-files/ransomware-test-01.txt /tmp/
    ```
12. When the restored volume reaches `available` state, detach the paused data volume from Blue1:
    ```bash
    aws ec2 detach-volume --volume-id <paused-volume-id>
    ```
    Attach the restored volume at `/dev/xvdf`.
13. On Blue1, mount and verify the restore:
    ```bash
    sudo mount /dev/xvdf /data
    ls /data/test-files/
    ```
    You should see the original `.txt` files — not the `.enc` files from Phase 1. This is your **restore success confirmation**. Record the time — this is your measured RTO from experiment start.
14. **Run GuardDuty Malware Protection on the restored volume** before trusting it in production: AWS Console → GuardDuty → Malware Protection → Scan → select the restored volume. Wait for the scan to complete and confirm no threats found.
15. **Simulate the Rotate step** (from the blog's Rebuild-Restore-Rotate framework). In a real incident you would rotate: IAM access keys, database passwords, application API keys, certificates, OAuth tokens, and SSH keys that were active during the compromise window. For this exercise: identify which credentials were in use during the experiment and document what you would rotate.

#### Stage 5 — Cutover (minutes 55–60)

16. Verify that application traffic would be re-routable to the restored volume (DNS records, load balancer target groups, or auto-scaling configuration depending on your architecture).
17. Record your total measured RTO against your pre-declared target.

**To end the experiment early:** Stop it from the FIS console or via `aws fis stop-experiment`. FIS automatically rolls back the EBS I/O pause and removes the NACL deny rules. Stopping the experiment also triggers the `onCancel` handler in the KMS automation, which immediately restores the key policy.

After the 60-minute hold expires, the KMS automation's `restoreKeyPolicy` step runs automatically, and FIS rolls back the EBS I/O pause and NACL rules.

## Post-Experiment Checklist

After the experiment completes (or you stop it early), verify the following before declaring the exercise done:

- [ ] Restored volume attached to Blue1 and original `.txt` files confirmed present at `/data/test-files/`
- [ ] GuardDuty Malware Protection scan on restored volume completed clean
- [ ] KMS key policy automatically restored — confirm the backup parameter is gone: `aws ssm get-parameter --name /fis-security/ransomware/kms-policy-backup` should return `ParameterNotFound`
- [ ] S3 access restored — `aws s3 cp s3://<bucket>/test-files/ransomware-test-01.txt /tmp/` succeeds
- [ ] EBS I/O pause lifted (FIS auto-removes; verify: `sudo dd if=/dev/xvdf of=/dev/null bs=1M count=1` on Blue1 completes without hanging)
- [ ] NACL deny rules removed (FIS auto-removes; verify no deny rules referencing S3 in the subnet NACL)
- [ ] Measured RTO recorded vs. target RTO
- [ ] Observations documented: what was harder than expected, what credentials/runbooks were missing, what would have failed in a real incident

## Gaps vs. Production Hardening

This experiment tests the fundamentals in a single AWS account. The following controls, described in the [AWS Cyber Resilience reference architecture](https://aws.amazon.com/blogs/architecture/cyber-resilience-on-aws-a-reference-approach-for-recovery-from-ransomware-and-destructive-events/), go beyond what can be validated in a single-account CDK stack but are the recommended next steps for production environments.

**1. Compliance mode Vault Lock (stronger than governance)**

This experiment deploys governance mode Vault Lock, which prevents deletion during the retention period but can still be disabled by a sufficiently privileged user. Compliance mode is enforced by the AWS Backup service itself — no IAM policy, including AdministratorAccess, can remove it after the grace period.

Upgrade path after you've finished testing with governance mode:
```bash
aws backup put-backup-vault-lock-configuration \
  --backup-vault-name fis-security-backup-vault \
  --min-retention-days 1 \
  --max-retention-days 30 \
  --changeable-for-days 0   # 0 = immediately permanent (no grace period)
```
**Warning:** This is irreversible. Only run it when you are confident in your retention policy.

**2. Cross-account air-gapped vault (Recovery Account)**

The blog's strongest protection is a dedicated Recovery Account that holds the logically air-gapped vault. Service Control Policies (SCPs) on that account restrict all its identities to backup operations only. Even full root compromise of the Production Account cannot reach recovery points in a different account's vault.

This cannot be simulated in a single-account CDK stack. As a next step: create a dedicated Recovery Account in AWS Organizations, deploy a second backup vault there, and configure cross-account backup copy rules. Verify that a compromised Production Account role cannot call `backup:DeleteRecoveryPoint` against the Recovery Account vault.

**3. Multi-party approval (MPA) gate before restore**

The blog requires a second IAM Identity Center identity to approve any restore job before it proceeds. The approval is recorded as a CloudTrail management event, providing an auditable gate. This exercise asks you to simulate this gate in step 9 of Phase 4. To implement it in production: configure IAM Identity Center with a Restore Approver group and wire approval into your incident response runbook.

**4. Dwell-time contamination**

The `post-change` recovery point created by `seed_backups.py` illustrates an important real-world threat: if an attacker is present in the environment for days before the destructive event, backups taken during that dwell window may carry ransomware indicators or modified files. In this experiment the volume content is identical in both recovery points — only the timestamps differ. In production: maintain backup retention that exceeds your organization's realistic detection window, and always compare recovery point timestamps to the earliest plausible compromise indicator before restoring.

## Optional Additions

These two documents are not part of the core 4-phase experiment and are not wired into `security-recovery-ransomware-impact-simulation-template.json`. They are standalone templates for customers who want to extend the exercise to their own environment. Both follow the `<YOUR ...>` placeholder convention used throughout this library for standalone use.

**1. RDS/database isolation fault (`ransomware-rds-isolation-ssm-automation.yaml`)**

If your application is database-backed, file encryption and S3/EBS lockout alone don't exercise your data-tier recovery path. This SSM Automation document adds an `aws:ssm:start-automation-execution` FIS action that isolates an RDS instance by swapping its security groups for a no-ingress isolation group — reversible, non-destructive, with automatic restore on hold expiry or early stop (same save/restore pattern as the KMS automation above).

This targets a database **you already operate** — point `DBInstanceIdentifier` and `IsolationSecurityGroupId` at your own RDS instance and a pre-created empty-ingress security group in the same VPC. It is not provisioned by this repo's CDK stack, since the demo environment doesn't run an application database.

To wire it into your own FIS experiment template, add an action referencing `fis-security-ransomware-rds-isolation` (mirror the `revoke-kms-access` action in `security-recovery-ransomware-impact-simulation-template.json`) and grant the permissions in the `AllowSSMAutomationExecution` and `AllowRDSSecurityGroupIsolation` statements already added to the IAM policy files in this directory.

Recovery during your own exercise follows the same shape as Phase 4 Stage 4: restore the DB from the latest valid automated snapshot or point-in-time-recovery target, measure the restore duration against your target RTO, and compare the snapshot/restore-point timestamp against the CloudTrail attack time to rule out dwell-time contamination — exactly as done for the EBS volume above.

**2. Pre/post health-check (`ransomware-health-check-ssm-document.yaml`)**

A thin, read-only Run Command document that captures a point-in-time application health snapshot: data volume mount status, test file counts, and an optional HTTP health-check call. It never fails the SSM command, so it's safe to run even while a fault is active.

Run it with `Mode=before` immediately prior to Phase 1 to establish a baseline, and again with `Mode=after` once Stage 4's restore is verified, to confirm the application returns to its pre-attack state rather than relying on file-count inspection alone. Add `ssm:SendCommand` on `fis-security-ransomware-health-check` to your FIS role (already present in `security-recovery-ransomware-impact-simulation-fis-iam-policy.json`) if you call it as its own FIS action rather than running it ad hoc via Run Command.

## Stop Conditions

The experiment does not have any specific stop conditions defined. It will run for the full ~78 minutes unless stopped manually from the console.

Stop conditions are based on an AWS CloudWatch alarm based on an operational or
business metric requiring an immediate end of the fault injection. This template
makes no assumptions about your application and the relevant metrics and does not
include stop conditions by default.

## Checking Results

**CloudWatch Metrics** (namespace: `FISSecurityExperiments`):
- `RansomwareTestVaultLockProtected = 1` → Vault Lock blocked backup deletion ✓
- `RansomwareTestVaultLockProtected = 0` → Backup was deleted — configure Vault Lock ✗
- `RansomwareTestVaultLockProtected = -1` → No recovery point found — run `seed_backups.py` first ⚠️
- `RansomwareTestSnapshotProtected = 1` → EBS snapshot was protected ✓
- `RansomwareTestSnapshotProtected = -1` → No snapshot found — run `seed_backups.py` first ⚠️

**CloudTrail:**
Search for events with `eventName = DeleteRecoveryPoint` and `errorCode = AccessDeniedException`. These confirm Vault Lock blocked the simulated ransomware.

**SSM Automation output:**
In Systems Manager → Automation → execution history, the `reportResults` step output contains a JSON summary including `overall_verdict: BACKUP_PROTECTED` or `BACKUP_VULNERABLE`.

## Next Steps

As you adapt this scenario to your needs, we recommend:

1. Upgrading your backup vault to **compliance mode** Vault Lock — see the upgrade path in *Gaps vs. Production Hardening* above.
2. Configuring a **cross-account Recovery Account** with an air-gapped vault and SCPs — see *Gaps vs. Production Hardening* above.
3. Implementing an **MPA approval gate** via IAM Identity Center before any restore proceeds.
4. Creating a CloudWatch alarm on `RansomwareTestVaultLockProtected` to alert your security team if the experiment ever shows a value of 0 (backup deleted — Vault Lock was not protecting it).
5. Adding a stop condition to this experiment tied to a CloudWatch alarm on a critical application health metric.
6. Using your measured Phase 4 RTO as your declared recovery time objective and tracking it over repeated experiment runs.
7. Enabling **AWS Backup Audit Manager** with the "Resources protected by Vault Lock" control for continuous compliance reporting.

## Import Experiment

You can import the json experiment template into your AWS account via cli or aws cdk. For step by step instructions on how, [click here](https://github.com/aws-samples/fis-template-library-tooling).
