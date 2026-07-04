.PHONY: help ci check sagi-operator-install-app sagi-operator-smoke sagi-operator-local-smoke sagi-operator-release-package sagi-operator-published-smoke

PYTHON ?= python3
VERSION ?=
BUILD ?=
BASE_URL ?=
PUBLISHED_BASE_URL ?= https://github.com/fjsr3125/sagi-check-app/releases/latest/download
PUBLISHED_LATEST_URL ?= $(PUBLISHED_BASE_URL)/latest.json
PUBLISHED_DOWNLOAD_DMG ?= 0
PUBLISHED_FIRST_LAUNCH ?= 0

help:
	@printf '%s\n' 'sagi-check-app commands'
	@printf '%s\n' '  make ci                              CI用の軽量検証'
	@printf '%s\n' '  make check                           Python構文チェック'
	@printf '%s\n' '  make sagi-operator-install-app       distへ.appを作成'
	@printf '%s\n' '  make sagi-operator-smoke             member-ready配布前チェック'
	@printf '%s\n' '  make sagi-operator-local-smoke       秘密設定なしのローカル広域チェック'
	@printf '%s\n' '  make sagi-operator-release-package   DMG/ZIP/latest.jsonを生成'
	@printf '%s\n' '  make sagi-operator-published-smoke   公開済みlatest.json/DMG URLを確認'

ci: check
	$(PYTHON) -m py_compile scripts/install_sagi_operator_app.py scripts/sagi_operator_release_check.py scripts/package_sagi_operator_release.py scripts/verify_published_release.py ops_dashboard/update_check.py

check:
	$(PYTHON) -m compileall -q scripts ops_dashboard

sagi-operator-install-app:
	SAGI_OPERATOR_REQUIRE_INSTAGRAM_PACKAGE=$${SAGI_OPERATOR_REQUIRE_INSTAGRAM_PACKAGE:-0} SAGI_OPERATOR_REQUIRE_MEMBERS_CONFIG=$${SAGI_OPERATOR_REQUIRE_MEMBERS_CONFIG:-0} SAGI_OPERATOR_REQUIRE_SHEETS_BRIDGE_CONFIG=$${SAGI_OPERATOR_REQUIRE_SHEETS_BRIDGE_CONFIG:-0} SAGI_OPERATOR_REQUIRE_CAPTURE_TOOLS=$${SAGI_OPERATOR_REQUIRE_CAPTURE_TOOLS:-0} $(PYTHON) scripts/install_sagi_operator_app.py

sagi-operator-smoke:
	$(PYTHON) scripts/sagi_operator_release_check.py --member-first-launch

sagi-operator-local-smoke:
	SAGI_OPERATOR_ALLOW_MISSING_PRIVATE_ASSETS=1 $(PYTHON) scripts/sagi_operator_release_check.py --allow-missing-private-assets --member-first-launch

sagi-operator-release-package:
	SAGI_OPERATOR_REQUIRE_INSTAGRAM_PACKAGE=1 SAGI_OPERATOR_REQUIRE_MEMBERS_CONFIG=1 SAGI_OPERATOR_REQUIRE_SHEETS_BRIDGE_CONFIG=1 SAGI_OPERATOR_REQUIRE_CAPTURE_TOOLS=1 $(PYTHON) scripts/package_sagi_operator_release.py $(if $(VERSION),--version "$(VERSION)",) $(if $(BASE_URL),--base-url "$(BASE_URL)",)

sagi-operator-published-smoke:
	$(PYTHON) scripts/verify_published_release.py --latest-url "$(PUBLISHED_LATEST_URL)" --base-url "$(PUBLISHED_BASE_URL)" --check-assets $(if $(filter 1 true yes,$(PUBLISHED_DOWNLOAD_DMG)),--download-dmg,) $(if $(filter 1 true yes,$(PUBLISHED_FIRST_LAUNCH)),--first-launch,) $(if $(VERSION),--version "$(VERSION)",) $(if $(BUILD),--build "$(BUILD)",)
