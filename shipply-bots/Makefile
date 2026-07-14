.DEFAULT_GOAL := help

.PHONY: help install test validate clean

help: ## Show all targets and their summaries
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}' | sort

install: ## Install the bot package in editable mode
	pip install -e bots/shipply-scout

test: ## Run the bot tests
	pytest bots/shipply-scout/tests

validate: ## Compile and test the bot
	python -m compileall bots/shipply-scout
	pytest bots/shipply-scout/tests

clean: ## Remove caches and virtual environment
	rm -rf .venv .pytest_cache
	find bots/shipply-scout -type d -name __pycache__ -exec rm -rf {} +
