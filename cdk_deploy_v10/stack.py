"""V10 CDK stack — deploys idre-reports-bot to EC2 on Amazon Linux 2023.

Differences from cdk_deploy/stack.py (v4/v5 baseline):
- Installs python3.11 explicitly (V10 uses PEP 604 X|Y union syntax).
- Drops chromadb / sentence-transformers / pytorch index URL (not imported in V10).
- Refined exclude list (drops tests/, local/, planning artifacts, runtime JSONLs).
- Systemd service sets OTEL_SDK_DISABLED=true (no collector on EC2).
- fetch_secrets writes env var keys in UPPER_SNAKE_CASE.
- DO NOT exclude *.pem — global-bundle.pem is the RDS SSL CA bundle.
"""
import os
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_iam as iam, aws_s3_assets as s3_assets
from constructs import Construct


class ReportsBotStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── Upload app code to S3 ─────────────────────────────────────────────
        app_asset = s3_assets.Asset(
            self, "AppCode",
            path=os.path.join(os.path.dirname(__file__), ".."),
            exclude=[
                # Secrets / local dev state
                ".env", ".env.*",
                # CDK
                "cdk_deploy", "cdk_deploy_v10", "cdk.out",
                # Version control + IDE
                ".git", ".gitignore", ".claude",
                # Caches + virtualenvs
                "__pycache__", "*.pyc", "venv", ".venv", ".pytest_cache",
                # Tests not needed at runtime
                "tests",
                # Planning / dev artifacts
                "local", "_*.py", "*.docx", "*.zip",
                "analyze_db.py", "audit_report_comparison.py", "e2e_test.py",
                "generate_doc.py", "_verify_pw_search.py",
                # Runtime-generated (rebuilt or excluded)
                "*.log", "*.jsonl",
                "data/materialized_results", "data/query_frequency.json",
                "data/error_knowledge_base.json", "data/saved_queries.json",
                "data/audit_log.jsonl", "data/feedback_log.jsonl",
                "data/anomaly_window.json", "data/correction_success_log.json",
                "data/confluence_cache",
                # Windows artifacts
                "Thumbs.db", "desktop.ini",
                # DO NOT exclude *.pem — global-bundle.pem is the RDS SSL CA
            ],
        )

        # ── IAM role ──────────────────────────────────────────────────────────
        role = iam.Role(
            self, "Ec2Role",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )
        role.add_to_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=["arn:aws:secretsmanager:us-east-1:<aws-account-id>:secret:idre/reports-bot/secrets*"],
        ))
        app_asset.grant_read(role)

        # ── VPC + Security group ──────────────────────────────────────────────
        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)
        sg = ec2.SecurityGroup(self, "Sg", vpc=vpc, allow_all_outbound=True,
                                description="IDRE Reports Bot V10")
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(8501), "Streamlit")
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(22), "SSH")

        # ── EC2 instance ──────────────────────────────────────────────────────
        instance = ec2.Instance(
            self, "Ec2",
            instance_type=ec2.InstanceType("t3.medium"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=sg,
            role=role,
            instance_name="idre-reports-bot",
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(30),
                ),
            ],
        )

        # ── Startup script ────────────────────────────────────────────────────
        script = f"""#!/bin/bash
set -e
exec > /var/log/idre-setup.log 2>&1

echo "=== Installing system packages (Python 3.11 explicit for V10) ==="
dnf update -y
dnf install -y python3.11 python3.11-pip gcc unzip

echo "=== Downloading app from S3 ==="
aws s3 cp s3://{app_asset.s3_bucket_name}/{app_asset.s3_object_key} /tmp/app.zip
mkdir -p <HOME>/app
unzip -o /tmp/app.zip -d <HOME>/app

echo "=== Fetching secrets and writing .env (env keys must match what bot reads in app.py + config/settings.py) ==="
cat << 'PYEOF' > /tmp/fetch_secrets.py
import subprocess
import json
import sys

try:
    res = subprocess.check_output([
        'aws', 'secretsmanager', 'get-secret-value',
        '--secret-id', 'idre/reports-bot/secrets',
        '--region', 'us-east-1',
        '--query', 'SecretString',
        '--output', 'text'
    ])
    secret_str = res.decode('utf-8').strip()

    try:
        data = json.loads(secret_str)
    except Exception:
        import re
        data = {{}}
        for m in re.finditer(r'"?(\\w+)"?\\s*:\\s*"?([^",}}]+)"?', secret_str):
            data[m.group(1).strip()] = m.group(2).strip()

    # If gemini_api_key key is missing, fall back to the raw secret (v4/v5 behavior:
    # supports the case where the whole secret value IS the Gemini API key as plain text).
    gemini  = data.get('gemini_api_key', secret_str)
    db_pass = data.get('db_password', '')
    app_pw  = data.get('app_password', 'IDRE_RB_135679')

    with open('<HOME>/app/.env', 'w') as f:
        f.write(f"Gemini_API_Key=<redacted>")
        f.write("DB_HOST=<rds-endpoint>\\n")
        f.write("DB_PORT=3306\\n")
        f.write("DB_NAME=idre_stage\\n")
        f.write("DB_USER=app_idre_rw\\n")
        f.write(f"DB_PASSWORD=<redacted>")
        f.write("DB_SSL_CA=./global-bundle.pem\\n")
        f.write(f"APP_PASSWORD=<redacted>")
        f.write("OTEL_SDK_DISABLED=true\\n")

    print("Successfully wrote .env file.")
except Exception as e:
    print(f"Failed to fetch secrets: {{e}}", file=sys.stderr)
    sys.exit(1)
PYEOF

python3.11 /tmp/fetch_secrets.py

echo "=== Installing Python dependencies (python3.11; no pytorch index) ==="
cd <HOME>/app
python3.11 -m venv <HOME>/app/venv
<HOME>/app/venv/bin/python -m pip install --upgrade pip
<HOME>/app/venv/bin/python -m pip install -r requirements.txt

echo "=== Setting file permissions ==="
chown -R ec2-user:ec2-user <HOME>/app

echo "=== Creating systemd service ==="
cat > /etc/systemd/system/streamlit.service << 'EOF'
[Unit]
Description=IDRE Reports Bot V10 (Streamlit)
After=network.target

[Service]
User=ec2-user
WorkingDirectory=<HOME>/app
ExecStart=<HOME>/app/venv/bin/python -m streamlit run app.py \\
    --server.port=8501 \\
    --server.address=0.0.0.0 \\
    --server.headless=true \\
    --server.enableCORS=false
Restart=always
RestartSec=10
Environment=HOME=<HOME>
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=OTEL_SDK_DISABLED=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable streamlit
systemctl start streamlit

echo "=== Setup complete ==="
echo "App running at http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4):8501"
"""
        instance.add_user_data(script)

        # ── Outputs ───────────────────────────────────────────────────────────
        cdk.CfnOutput(self, "PublicIp",   value=instance.instance_public_ip)
        cdk.CfnOutput(self, "AppUrl",     value=f"http://{instance.instance_public_ip}:8501")
        cdk.CfnOutput(self, "InstanceId", value=instance.instance_id)
