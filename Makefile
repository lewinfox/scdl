FLY_APP ?= scdl-lewinfox
BROWSER ?= firefox

.PHONY: help yt-cookies yt-cookies-check yt-cookies-revoke dev secrets

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

## --- YouTube cookies -------------------------------------------------------
##
## The DRM fallback searches YouTube, which challenges unauthenticated
## requests. Cookies get us past that, but Google invalidates them fairly
## aggressively, so expect to re-run this periodically — the UI shows a banner
## when they expire or get rejected.
##
## Use a throwaway Google account. Even scoped to youtube.com these are Google
## account credentials, and they end up in a Fly secret.

yt-cookies:  ## Re-export YouTube cookies (BROWSER=firefox) and push to Fly
	@command -v flyctl >/dev/null || { echo "flyctl not on PATH"; exit 1; }
	@echo "Exporting from $(BROWSER), scoping to youtube.com, pushing to $(FLY_APP)…"
	@# The jar goes browser -> filter -> flyctl down a pipe. It is never
	@# written anywhere we would then have to remember to shred.
	@command -v uv >/dev/null || { echo "need uv (https://docs.astral.sh/uv/)"; exit 1; }
	@COOKIES="$$(uv run --quiet --with yt-dlp python scripts/yt-cookies.py --browser $(BROWSER))" \
		&& [ -n "$$COOKIES" ] \
		&& flyctl secrets set SCDL_YT_COOKIES="$$COOKIES" -a $(FLY_APP)
	@echo "Done. Check the banner in the UI has cleared."

yt-cookies-check:  ## Report what Fly currently holds
	@flyctl secrets list -a $(FLY_APP) | grep -q SCDL_YT_COOKIES \
		&& echo "SCDL_YT_COOKIES is set on $(FLY_APP)" \
		|| echo "SCDL_YT_COOKIES is NOT set on $(FLY_APP) — fallback runs unauthenticated"

yt-cookies-revoke:  ## Remove the cookies from Fly (the fallback keeps working, unauthenticated)
	@echo "This removes SCDL_YT_COOKIES from $(FLY_APP) and restarts the machine."
	@echo "It does NOT sign the session out at Google — do that at"
	@echo "  https://myaccount.google.com/device-activity"
	@echo "if you believe the cookies leaked."
	@printf "Remove the secret? [y/N] " && read ans && [ "$$ans" = "y" ]
	@flyctl secrets unset SCDL_YT_COOKIES -a $(FLY_APP)

## --- Local -----------------------------------------------------------------

dev:  ## Run the app locally in Docker with live-mounted source
	docker compose up --build

secrets:  ## List the secrets configured on Fly
	@flyctl secrets list -a $(FLY_APP)
