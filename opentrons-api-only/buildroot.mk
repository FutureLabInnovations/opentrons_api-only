################################################################################
#
# python-opentrons-api
#
################################################################################
include $(BR2_EXTERNAL_OPENTRONS_MONOREPO_PATH)/scripts/python.mk


define OTAPI_CALL_PBU
	$(shell python $(BR2_EXTERNAL_OPENTRONS_MONOREPO_PATH)/scripts/python_build_utils.py api $(or $(OPENTRONS_PROJECT),robot-stack) $(1))
endef

PYTHON_OPENTRONS_API_VERSION = $(call OTAPI_CALL_PBU,get_version)
PYTHON_OPENTRONS_API_LICENSE = Apache-2
PYTHON_OPENTRONS_API_LICENSE_FILES = $(BR2_EXTERNAL_OPENTRONS_MONOREPO_PATH)/LICENSE
PYTHON_OPENTRONS_API_SETUP_TYPE = hatch
PYTHON_OPENTRONS_API_SITE_METHOD = local
PYTHON_OPENTRONS_API_SITE = $(BR2_EXTERNAL_OPENTRONS_MONOREPO_PATH)
PYTHON_OPENTRONS_API_SUBDIR = api
PYTHON_OPENTRONS_API_POST_INSTALL_TARGET_HOOKS = PYTHON_OPENTRONS_API_INSTALL_VERSION PYTHON_OPENTRONS_API_INSTALL_RELEASE_NOTES
PYTHON_OPENTRONS_API_DEPENDENCIES = host-python-hatch-vcs-tunable host-python-hatch-dependency-coversion
PYTHON_OPENTRONS_API_ENV = \
  HATCH_VCS_TUNABLE_TAG_PATTERN="$(call git_tag_regex_for_project,$(PROJECT))" \
  HATCH_VCS_TUNABLE_RAW_OPTIONS="$(call hatch_raw_options_for_project,$(PROJECT));root=$(shell realpath --relative-to=$(PYTHON_OPENTRONS_API_BUILDDIR) $(BR2_EXTERNAL_OPENTRONS_MONOREPO_PATH))"

define PYTHON_OPENTRONS_API_INSTALL_VERSION
	echo '$(call OTAPI_CALL_PBU,dump_br_version)' > $(BINARIES_DIR)/opentrons-api-version.json
endef

ot_api_name := python-opentrons-api

define PYTHON_OPENTRONS_API_INSTALL_RELEASE_NOTES
	$(INSTALL) -D -m 0644 $(BR2_EXTERNAL_OPENTRONS_MONOREPO_PATH)/api/release-notes.md $(BINARIES_DIR)/release-notes.md
endef
export OPENTRONS_GIT_DIR=$(BR2_EXTERNAL_OPENTRONS_MONOREPO_PATH)

# Calling inner-python-package directly instead of using python-package macro
# because our directory layout doesn’t conform to buildroot’s expectation of
# having the directory name be the package name
$(eval $(call inner-python-package,$(ot_api_name),$(call UPPERCASE,$(ot_api_name)),$(call UPPERCASE,$(ot_api_name)),target))

