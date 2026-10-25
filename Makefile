DEPLOY_HOST ?= 185.183.156.64
DEPLOY_USER ?= deploy
DEPLOY_PATH ?= /var/www/voiceover-studio

.PHONY: deploy deploy-status deploy-dry-run

deploy:
	ssh -t "$(DEPLOY_USER)@$(DEPLOY_HOST)" 'cd "$(DEPLOY_PATH)" && .venv/bin/python deploy/update.py'

deploy-status:
	ssh -t "$(DEPLOY_USER)@$(DEPLOY_HOST)" 'cd "$(DEPLOY_PATH)" && .venv/bin/python deploy/update.py --status'

deploy-dry-run:
	ssh -t "$(DEPLOY_USER)@$(DEPLOY_HOST)" 'cd "$(DEPLOY_PATH)" && .venv/bin/python deploy/update.py --dry-run'
