TAILWIND ?= .tools/tailwindcss

.PHONY: css fonts test

css:            ## build static/app.css (Tailwind standalone CLI; no node needed)
	$(TAILWIND) -i boxbutler/web/static/src/input.css -o boxbutler/web/static/app.css --minify

fonts:          ## fetch Fira Sans / Fira Code woff2 (OFL) into static/fonts — run once, files are committed
	scripts/fetch-fonts.sh

test:
	pytest -q
