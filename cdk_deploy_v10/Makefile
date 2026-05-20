# V10 CDK deploy targets — run from cdk_deploy_v10/
ACCOUNT := 822804284820
REGION  := us-east-1
STACK   := idre-reports-bot

.PHONY: bootstrap synth diff deploy destroy logs snapshot

bootstrap:
	cdk bootstrap aws://$(ACCOUNT)/$(REGION)

synth:
	cdk synth

diff:
	cdk diff

deploy:
	cdk deploy --require-approval never

destroy:
	cdk destroy --force

logs:
	@echo "Use AWS SSM Session Manager to connect:"
	@echo "  aws ssm start-session --target <INSTANCE_ID> --region $(REGION)"
	@echo ""
	@echo "Once in the session:"
	@echo "  sudo journalctl -u streamlit -f"
	@echo ""
	@echo "Get INSTANCE_ID from the InstanceId CFN output:"
	@echo "  aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) --query \"Stacks[0].Outputs\""

snapshot:
	@TS=$$(date +%Y-%m-%d-%H%M); \
	DEST=../../local/deploy/$$TS-v10-snapshot; \
	echo "Creating snapshot at $$DEST"; \
	mkdir -p $$DEST; \
	rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' --exclude='cdk.out' --exclude='cdk.context.json' . $$DEST/cdk_deploy_v10/; \
	rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' --exclude='venv' --exclude='.pytest_cache' --exclude='data/audit_log.jsonl' --exclude='data/feedback_log.jsonl' --exclude='.git' --exclude='.claude' --exclude='local' --exclude='tests' --exclude='_*.py' --exclude='*.docx' --exclude='*.zip' --exclude='cdk_deploy' --exclude='cdk_deploy_v10' ../ $$DEST/bot/; \
	printf "# V10 Deploy Snapshot\n\n**Captured:** $$TS\n**Stack:** $(STACK)\n**Account:** $(ACCOUNT)\n**Region:** $(REGION)\n\n## Fill in after deploy\n- PublicIp:\n- AppUrl:\n- InstanceId:\n- Deploy timestamp (UTC):\n\n## How to re-deploy from this snapshot\n\nThis snapshot is a frozen copy; to re-deploy, copy ./bot/ + ./cdk_deploy_v10/ to a working directory, cd into cdk_deploy_v10/, and run preflight.sh + make deploy.\n" > $$DEST/DEPLOY_NOTES.md; \
	echo "Snapshot ready. Zip with: cd ../../local/deploy && 7z a -r $$TS-v10-snapshot.zip $$TS-v10-snapshot/"
