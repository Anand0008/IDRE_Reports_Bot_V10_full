# V10 CDK Deploy — `idre-reports-bot`

**Account:** `822804284820`
**Region:** `us-east-1`
**Stack name:** `idre-reports-bot` (deploys OVER the existing v4/v5 stack — EC2 instance is REPLACED; public IP changes)
**Instance:** `t3.medium` + 30 GB EBS, Amazon Linux 2023, Python 3.11

## Prerequisites checklist

Before running `make deploy`, confirm:

1. **AWS CLI configured** for account `822804284820` (`aws sts get-caller-identity` returns it).
2. **CDK CLI installed** (`cdk --version` works; `npm install -g aws-cdk` if missing).
3. **CDK bootstrapped** for the target account/region: `make bootstrap` (idempotent — re-runs are safe).
4. **Secrets Manager key exists:** `idre/reports-bot/secrets` in `us-east-1` with keys `gemini_api_key`, `db_password`, `app_password`. (Same secret v4/v5 used — should already exist.)
5. **RDS security group ingress** allows traffic from the NEW EC2 security group (CDK creates a new SG per stack — RDS-side may need manual add post-deploy if the DB connection fails).
6. **Local Python 3.11** for `preflight.sh` (`py -3.11 --version` on Windows; `python3.11 --version` on Linux/Mac).
7. **Git Bash** (Windows users) — `preflight.sh` is a bash script.

## Deploy command sequence

```bash
cd cdk_deploy_v10

# 1. Pre-flight — runs syntax check, full test suite, pip dry-run, asset size estimate
bash preflight.sh

# 2. Bootstrap (one-time per account/region; idempotent)
make bootstrap

# 3. Synth — show what'll be deployed
make synth

# 4. Diff — show changes vs currently-deployed stack
make diff

# 5. Deploy
make deploy

# After successful deploy:
# - PublicIp / AppUrl printed as CFN outputs
# - First-request latency ~30s (module imports + DB pool warmup)
```

## Post-deploy verification

```bash
# Health check (Streamlit's built-in)
curl http://${PublicIp}:8501/_stcore/health
# Expected: "ok"

# Tail journalctl on the instance
make logs    # uses SSM Session Manager — no SSH key needed
```

## Rollback

```bash
make destroy  # cdk destroy --force; removes the stack entirely
```

After `make destroy`, the v10 stack is gone. The v4/v5 stack that was REPLACED on deploy is also gone (the deploy was an in-place stack update; the original stack version is not preserved).

## Snapshot for archive

After a successful deploy, capture a self-contained snapshot:

```bash
make snapshot   # creates local/deploy/<timestamp>-v10-snapshot/
```

Fills `DEPLOY_NOTES.md` template — you complete it manually with PublicIp, StackId, deploy timestamp.

## Known deferred items

- **Persistent EBS for logs** — current setup loses `audit_log.jsonl` + `feedback_log.jsonl` when CFN replaces the EC2. Acceptable for v10 fresh deploy; add a separate EBS with `DeletionPolicy=Retain` later.
- **CloudWatch Logs agent** — systemd journal stays local. SSH/SSM + `journalctl -u streamlit -f` is the current observability story.
- **HTTPS / ALB / IP allowlist** — port 8501 is open to `0.0.0.0/0` with `APP_PASSWORD` gate only. Future hardening.
- **Gemini rate limits** — Gemini 3.1 Pro tier-limited RPM. Pre-existing concern.
