# ytmp3 -- the flags that make downloads actually work.
#
# YouTube answers anonymous downloads with "Sign in to confirm you're not a
# bot", so every target that fetches audio passes a browser session. Override
# any of these on the command line:  make tui BROWSER=safari

BROWSER   ?= chrome
MUSIC_DIR ?= /Volumes/Flash/music/songs
LIMIT     ?= 6
CONCURRENCY ?= 3

RUN  := uv run python -m ytmp3.cli
AUTH := --music-dir "$(MUSIC_DIR)" --cookies-from-browser $(BROWSER) \
        --limit $(LIMIT) --concurrency $(CONCURRENCY)

.DEFAULT_GOAL := tui
.PHONY: tui sync status search web bulk test check help

tui:  ## open the TUI (default)
	$(RUN) $(AUTH) tui

sync:  ## download every catalog track not already on disk
	$(RUN) $(AUTH) sync

status:  ## what's downloaded, what's missing
	$(RUN) --music-dir "$(MUSIC_DIR)" status

search:  ## catalog search, offline:  make search Q="four tet"
	@test -n "$(Q)" || { echo 'usage: make search Q="four tet"' >&2; exit 2; }
	$(RUN) --music-dir "$(MUSIC_DIR)" search $(Q)

web:  ## ranked YouTube search, no download:  make web Q="bicep glue"
	@test -n "$(Q)" || { echo 'usage: make web Q="bicep glue"' >&2; exit 2; }
	$(RUN) $(AUTH) web $(Q)

bulk:  ## search a file of queries, review, download:  make bulk F=wishlist.txt
	@test -n "$(F)" || { echo 'usage: make bulk F=wishlist.txt' >&2; exit 2; }
	$(RUN) $(AUTH) bulk "$(F)"

test:  ## the offline suite
	uv run python -m pytest -q

check:  ## are the external tools the downloader needs actually here?
	@for tool in uv yt-dlp ffmpeg deno; do \
		path=$$(command -v $$tool) \
			&& printf '  ok   %-8s %s\n' "$$tool" "$$path" \
			|| printf '  MISSING %-8s\n' "$$tool"; \
	done
	@echo
	@echo "deno is the JavaScript runtime yt-dlp needs for YouTube's signature"
	@echo "challenge; without it format selection fails with 'Only images are"
	@echo "available' and no audio can be downloaded.  brew install deno"

help:  ## this list
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) \
		| sed 's/:.*##/\t/' \
		| awk -F'\t' '{printf "  make %-8s %s\n", $$1, $$2}'
