# Host unit tests for the pure firmware sections; no ESP-IDF needed.
# mbedtls is found via pkg-config, then Homebrew, then a bare -lmbedcrypto.
# `make test` runs every test binary; `make png` runs render_png.

CC ?= cc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O1 -g
INCLUDES := -I../main

# Stand-ins for the ESP-IDF headers input.c needs; searched after -I../main.
IDF_STUB_INCLUDES := -Iidf_stub

MBEDTLS_CFLAGS := $(shell pkg-config --cflags mbedcrypto 2>/dev/null)
MBEDTLS_LIBS := $(shell pkg-config --libs mbedcrypto 2>/dev/null)
ifeq ($(strip $(MBEDTLS_LIBS)),)
  BREW_MBEDTLS := $(shell brew --prefix mbedtls@3 2>/dev/null || brew --prefix mbedtls 2>/dev/null)
  ifneq ($(strip $(BREW_MBEDTLS)),)
    MBEDTLS_CFLAGS := -I$(BREW_MBEDTLS)/include
    MBEDTLS_LIBS := -L$(BREW_MBEDTLS)/lib -lmbedcrypto
  else
    MBEDTLS_LIBS := -lmbedcrypto
  endif
endif

ZLIB_CFLAGS := $(shell pkg-config --cflags zlib 2>/dev/null)
ZLIB_LIBS := $(shell pkg-config --libs zlib 2>/dev/null)
ifeq ($(strip $(ZLIB_LIBS)),)
  ZLIB_LIBS := -lz
endif

MAIN_SRC := ../main
BUILD := build

TEST_BINS := $(TESTS:%=$(BUILD)/%)

# Asset image for render_png (built by tools/mkassets.py).
ASSETS ?= ../../build/assets.bin

.PHONY: all test png emoji_png clean
all: $(TEST_BINS)

test: $(TEST_BINS)
	@status=0; \
	for t in $(TEST_BINS); do \
		echo "--- running $$t ---"; \
		./$$t || status=1; \
	done; \
	exit $$status

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/cbor.o: $(MAIN_SRC)/cbor.c $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/cbor.c

$(BUILD)/test_cbor.o: test_cbor.c $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_cbor.c

$(BUILD)/test_cbor: $(BUILD)/cbor.o $(BUILD)/test_cbor.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/cbor.o $(BUILD)/test_cbor.o

# test_auth also links cbor.c to decode large-n vectors.
$(BUILD)/auth.o: $(MAIN_SRC)/auth.c $(MAIN_SRC)/auth.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/auth.c

$(BUILD)/test_auth.o: test_auth.c $(MAIN_SRC)/auth.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_auth.c

$(BUILD)/test_auth: $(BUILD)/auth.o $(BUILD)/cbor.o $(BUILD)/test_auth.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/auth.o $(BUILD)/cbor.o $(BUILD)/test_auth.o

# test_setup links the real mbedtls (HKDF, AES-256-GCM).
$(BUILD)/setup.o: $(MAIN_SRC)/setup.c $(MAIN_SRC)/setup.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) $(MBEDTLS_CFLAGS) -c -o $@ $(MAIN_SRC)/setup.c

$(BUILD)/test_setup.o: test_setup.c $(MAIN_SRC)/setup.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) $(MBEDTLS_CFLAGS) -c -o $@ test_setup.c

$(BUILD)/test_setup: $(BUILD)/setup.o $(BUILD)/cbor.o $(BUILD)/test_setup.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/setup.o $(BUILD)/cbor.o $(BUILD)/test_setup.o $(MBEDTLS_LIBS)

# test_input builds input.c with -DESP_PLATFORM against idf_stub.c.
$(BUILD)/input.o: $(MAIN_SRC)/input.c $(MAIN_SRC)/input.h | $(BUILD)
	$(CC) $(CFLAGS) -DESP_PLATFORM -DPAGER_WAKE_BUTTON_ENABLED=1 $(INCLUDES) $(IDF_STUB_INCLUDES) -c -o $@ $(MAIN_SRC)/input.c

$(BUILD)/idf_stub.o: idf_stub.c | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) $(IDF_STUB_INCLUDES) -c -o $@ idf_stub.c

$(BUILD)/test_input.o: test_input.c $(MAIN_SRC)/input.h | $(BUILD)
	$(CC) $(CFLAGS) -DESP_PLATFORM -DPAGER_WAKE_BUTTON_ENABLED=1 $(INCLUDES) $(IDF_STUB_INCLUDES) -c -o $@ test_input.c

$(BUILD)/test_input: $(BUILD)/input.o $(BUILD)/idf_stub.o $(BUILD)/test_input.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/input.o $(BUILD)/idf_stub.o $(BUILD)/test_input.o

$(BUILD)/msg.o: $(MAIN_SRC)/msg.c $(MAIN_SRC)/msg.h $(MAIN_SRC)/auth.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/msg.c

$(BUILD)/test_msg.o: test_msg.c $(MAIN_SRC)/msg.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_msg.c

$(BUILD)/test_msg: $(BUILD)/msg.o $(BUILD)/test_msg.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/msg.o $(BUILD)/test_msg.o

# test_lock links the real mbedtls (lock_pbkdf2()).
$(BUILD)/lock.o: $(MAIN_SRC)/lock.c $(MAIN_SRC)/lock.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) $(MBEDTLS_CFLAGS) -c -o $@ $(MAIN_SRC)/lock.c

$(BUILD)/test_lock.o: test_lock.c $(MAIN_SRC)/lock.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) $(MBEDTLS_CFLAGS) -c -o $@ test_lock.c

$(BUILD)/test_lock: $(BUILD)/lock.o $(BUILD)/cbor.o $(BUILD)/test_lock.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/lock.o $(BUILD)/cbor.o $(BUILD)/test_lock.o $(MBEDTLS_LIBS)

$(BUILD)/loc.o: $(MAIN_SRC)/loc.c $(MAIN_SRC)/loc.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/loc.c

$(BUILD)/test_loc.o: test_loc.c $(MAIN_SRC)/loc.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_loc.c

$(BUILD)/test_loc: $(BUILD)/loc.o $(BUILD)/cbor.o $(BUILD)/test_loc.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/loc.o $(BUILD)/cbor.o $(BUILD)/test_loc.o

# test_cafetch links the real mbedtls (cafetch_validate_body() hashes with SHA-256).
$(BUILD)/cafetch.o: $(MAIN_SRC)/cafetch.c $(MAIN_SRC)/cafetch.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) $(MBEDTLS_CFLAGS) -c -o $@ $(MAIN_SRC)/cafetch.c

$(BUILD)/test_cafetch.o: test_cafetch.c $(MAIN_SRC)/cafetch.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) $(MBEDTLS_CFLAGS) -c -o $@ test_cafetch.c

$(BUILD)/test_cafetch: $(BUILD)/cafetch.o $(BUILD)/test_cafetch.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/cafetch.o $(BUILD)/test_cafetch.o $(MBEDTLS_LIBS)

$(BUILD)/test_gfx_assets.o: test_gfx_assets.c $(MAIN_SRC)/gfx.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_gfx_assets.c

$(BUILD)/test_gfx_assets: $(BUILD)/gfx.o $(BUILD)/test_gfx_assets.o
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD)/astpart.o: $(MAIN_SRC)/astpart.c $(MAIN_SRC)/astpart.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/astpart.c

$(BUILD)/test_astpart.o: test_astpart.c $(MAIN_SRC)/astpart.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_astpart.c

$(BUILD)/test_astpart: $(BUILD)/astpart.o $(BUILD)/test_astpart.o
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD)/catrust.o: $(MAIN_SRC)/catrust.c $(MAIN_SRC)/catrust.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/catrust.c

$(BUILD)/test_catrust.o: test_catrust.c $(MAIN_SRC)/catrust.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_catrust.c

# test_otapipe: vendored detools and miniz tinfl; $$OTA_FIXTURES / $$OTA_IMAGES select
# real-artifact cases, which print SKIP when absent.
DETOOLS_DEFS := -DDETOOLS_CONFIG_FILE_IO=0 -DDETOOLS_CONFIG_COMPRESSION_LZMA=0 -DDETOOLS_CONFIG_COMPRESSION_CRLE=0 -DDETOOLS_CONFIG_COMPRESSION_HEATSHRINK=0
OTA_INCLUDES := -I../components/detools -Ithird_party/miniz

$(BUILD)/detools.o: ../components/detools/detools.c ../components/detools/detools.h | $(BUILD)
	$(CC) $(CFLAGS) $(DETOOLS_DEFS) -I../components/detools -c -o $@ ../components/detools/detools.c

$(BUILD)/miniz_tinfl.o: third_party/miniz/miniz_tinfl.c | $(BUILD)
	$(CC) $(CFLAGS) -Ithird_party/miniz -c -o $@ third_party/miniz/miniz_tinfl.c

$(BUILD)/otapipe.o: $(MAIN_SRC)/otapipe.c $(MAIN_SRC)/otapipe.h | $(BUILD)
	$(CC) $(CFLAGS) $(DETOOLS_DEFS) $(INCLUDES) $(OTA_INCLUDES) $(MBEDTLS_CFLAGS) -c -o $@ $(MAIN_SRC)/otapipe.c

$(BUILD)/test_otapipe.o: test_otapipe.c $(MAIN_SRC)/otapipe.h | $(BUILD)
	$(CC) $(CFLAGS) $(DETOOLS_DEFS) $(INCLUDES) $(OTA_INCLUDES) $(MBEDTLS_CFLAGS) -c -o $@ test_otapipe.c

$(BUILD)/test_otapipe: $(BUILD)/otapipe.o $(BUILD)/detools.o $(BUILD)/miniz_tinfl.o $(BUILD)/test_otapipe.o
	$(CC) $(CFLAGS) -o $@ $^ $(MBEDTLS_LIBS) -lz

$(BUILD)/ota.o: $(MAIN_SRC)/ota.c $(MAIN_SRC)/gfx.h $(MAIN_SRC)/ota.h $(MAIN_SRC)/cafetch.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/ota.c

$(BUILD)/test_ota.o: test_ota.c $(MAIN_SRC)/ota.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_ota.c

$(BUILD)/test_ota: $(BUILD)/ota.o $(BUILD)/cbor.o $(BUILD)/test_ota.o
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD)/test_catrust: $(BUILD)/catrust.o $(BUILD)/cbor.o $(BUILD)/test_catrust.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/catrust.o $(BUILD)/cbor.o $(BUILD)/test_catrust.o

$(BUILD)/cfg.o: $(MAIN_SRC)/cfg.c $(MAIN_SRC)/cfg.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/cfg.c

$(BUILD)/test_cfg.o: test_cfg.c $(MAIN_SRC)/cfg.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_cfg.c

$(BUILD)/test_cfg: $(BUILD)/cfg.o $(BUILD)/cbor.o $(BUILD)/test_cfg.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/cfg.o $(BUILD)/cbor.o $(BUILD)/test_cfg.o

$(BUILD)/sms.o: $(MAIN_SRC)/sms.c $(MAIN_SRC)/sms.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/sms.c

$(BUILD)/test_sms.o: test_sms.c $(MAIN_SRC)/sms.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_sms.c

$(BUILD)/test_sms: $(BUILD)/sms.o $(BUILD)/cbor.o $(BUILD)/test_sms.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/sms.o $(BUILD)/cbor.o $(BUILD)/test_sms.o

# render_png links zlib for the PNG IDAT chunk.
$(BUILD)/gfx.o: $(MAIN_SRC)/gfx.c $(MAIN_SRC)/gfx.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/gfx.c

$(BUILD)/render_png.o: render_png.c $(MAIN_SRC)/gfx.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) $(ZLIB_CFLAGS) -c -o $@ render_png.c

$(BUILD)/render_png: $(BUILD)/gfx.o $(BUILD)/render_png.o $(BUILD)/devinfo_fmt.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/gfx.o $(BUILD)/render_png.o $(BUILD)/devinfo_fmt.o $(ZLIB_LIBS)

# `make emoji_png ASSETS=<emoji image>` renders the emoji sheet.
$(BUILD)/render_emoji.o: render_emoji.c render_png.c $(MAIN_SRC)/gfx.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) $(ZLIB_CFLAGS) -c -o $@ render_emoji.c

$(BUILD)/render_emoji: $(BUILD)/gfx.o $(BUILD)/render_emoji.o $(BUILD)/devinfo_fmt.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/gfx.o $(BUILD)/render_emoji.o $(BUILD)/devinfo_fmt.o $(ZLIB_LIBS)

emoji_png: $(BUILD)/render_emoji
	./$(BUILD)/render_emoji $(ASSETS)

png: $(BUILD)/render_png
	./$(BUILD)/render_png $(ASSETS)

clean:
	rm -rf $(BUILD)

$(BUILD)/scr_chat.o: $(MAIN_SRC)/scr_chat.c | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/scr_chat.c

$(BUILD)/test_chat_layout.o: test_chat_layout.c | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_chat_layout.c

$(BUILD)/test_chat_layout: $(BUILD)/scr_chat.o $(BUILD)/test_chat_layout.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/scr_chat.o $(BUILD)/test_chat_layout.o

$(BUILD)/scr_home.o: $(MAIN_SRC)/scr_home.c $(MAIN_SRC)/msg.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/scr_home.c

$(BUILD)/test_home_list.o: test_home_list.c $(MAIN_SRC)/msg.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_home_list.c

$(BUILD)/test_home_list: $(BUILD)/scr_home.o $(BUILD)/test_home_list.o $(BUILD)/msg.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/scr_home.o $(BUILD)/test_home_list.o $(BUILD)/msg.o

$(BUILD)/archive.o: $(MAIN_SRC)/archive.c $(MAIN_SRC)/archive.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/archive.c

$(BUILD)/test_archive.o: test_archive.c $(MAIN_SRC)/archive.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_archive.c

$(BUILD)/test_archive: $(BUILD)/archive.o $(BUILD)/test_archive.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/archive.o $(BUILD)/test_archive.o

$(BUILD)/carrier.o: $(MAIN_SRC)/carrier.c $(MAIN_SRC)/carrier.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/carrier.c

$(BUILD)/test_carrier.o: test_carrier.c $(MAIN_SRC)/carrier.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_carrier.c

$(BUILD)/test_carrier: $(BUILD)/carrier.o $(BUILD)/test_carrier.o
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD)/coverage.o: $(MAIN_SRC)/coverage.c $(MAIN_SRC)/coverage.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/coverage.c

$(BUILD)/test_coverage.o: test_coverage.c $(MAIN_SRC)/coverage.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_coverage.c

$(BUILD)/test_coverage: $(BUILD)/coverage.o $(BUILD)/test_coverage.o
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD)/net_connect_guard.o: $(MAIN_SRC)/net_connect_guard.c $(MAIN_SRC)/net_connect_guard.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/net_connect_guard.c

$(BUILD)/test_net_connect_guard.o: test_net_connect_guard.c $(MAIN_SRC)/net_connect_guard.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_net_connect_guard.c

$(BUILD)/test_net_connect_guard: $(BUILD)/net_connect_guard.o $(BUILD)/test_net_connect_guard.o
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD)/net_probe_guard.o: $(MAIN_SRC)/net_probe_guard.c $(MAIN_SRC)/net_probe_guard.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/net_probe_guard.c

$(BUILD)/test_net_probe_guard.o: test_net_probe_guard.c $(MAIN_SRC)/net_probe_guard.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_net_probe_guard.c

$(BUILD)/test_net_probe_guard: $(BUILD)/net_probe_guard.o $(BUILD)/test_net_probe_guard.o
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD)/resub_verdict.o: $(MAIN_SRC)/resub_verdict.c $(MAIN_SRC)/resub_verdict.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/resub_verdict.c

$(BUILD)/test_resub_verdict.o: test_resub_verdict.c $(MAIN_SRC)/resub_verdict.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_resub_verdict.c

$(BUILD)/test_resub_verdict: $(BUILD)/resub_verdict.o $(BUILD)/test_resub_verdict.o
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD)/publish_quiet.o: $(MAIN_SRC)/publish_quiet.c $(MAIN_SRC)/publish_quiet.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/publish_quiet.c

$(BUILD)/test_publish_quiet.o: test_publish_quiet.c $(MAIN_SRC)/publish_quiet.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_publish_quiet.c

$(BUILD)/test_publish_quiet: $(BUILD)/publish_quiet.o $(BUILD)/test_publish_quiet.o
$(BUILD)/accel.o: $(MAIN_SRC)/accel.c $(MAIN_SRC)/accel.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/accel.c

$(BUILD)/test_accel_rate.o: test_accel_rate.c $(MAIN_SRC)/accel.h $(MAIN_SRC)/loc.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_accel_rate.c

$(BUILD)/test_accel_rate: $(BUILD)/accel.o $(BUILD)/loc.o $(BUILD)/cbor.o $(BUILD)/test_accel_rate.o
# test_bookpull links the real mbedtls: book.c needs cafetch.c, which uses mbedtls_sha256().
$(BUILD)/book.o: $(MAIN_SRC)/book.c $(MAIN_SRC)/book.h $(MAIN_SRC)/cafetch.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/book.c

$(BUILD)/bookpull.o: $(MAIN_SRC)/bookpull.c $(MAIN_SRC)/bookpull.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/bookpull.c

$(BUILD)/test_bookpull.o: test_bookpull.c $(MAIN_SRC)/book.h $(MAIN_SRC)/bookpull.h $(MAIN_SRC)/auth.h \
                         $(MAIN_SRC)/cafetch.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) $(MBEDTLS_CFLAGS) -c -o $@ test_bookpull.c

$(BUILD)/test_bookpull: $(BUILD)/book.o $(BUILD)/bookpull.o $(BUILD)/auth.o $(BUILD)/cbor.o \
                       $(BUILD)/cafetch.o $(BUILD)/test_bookpull.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/book.o $(BUILD)/bookpull.o $(BUILD)/auth.o $(BUILD)/cbor.o \
	    $(BUILD)/cafetch.o $(BUILD)/test_bookpull.o $(MBEDTLS_LIBS)

$(BUILD)/clockfmt.o: $(MAIN_SRC)/clockfmt.c $(MAIN_SRC)/clockfmt.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/clockfmt.c

$(BUILD)/test_clock.o: test_clock.c $(MAIN_SRC)/clockfmt.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_clock.c

$(BUILD)/test_clock: $(BUILD)/clockfmt.o $(BUILD)/test_clock.o
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD)/disp_flip.o: $(MAIN_SRC)/disp_flip.c $(MAIN_SRC)/disp_flip.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/disp_flip.c

$(BUILD)/test_disp_flip.o: test_disp_flip.c $(MAIN_SRC)/disp_flip.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_disp_flip.c

$(BUILD)/test_disp_flip: $(BUILD)/disp_flip.o $(BUILD)/test_disp_flip.o
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD)/battstat.o: $(MAIN_SRC)/battstat.c $(MAIN_SRC)/battstat.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/battstat.c

$(BUILD)/test_battstat.o: test_battstat.c $(MAIN_SRC)/battstat.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_battstat.c

$(BUILD)/test_battstat: $(BUILD)/battstat.o $(BUILD)/cbor.o $(BUILD)/test_battstat.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/battstat.o $(BUILD)/cbor.o $(BUILD)/test_battstat.o

$(BUILD)/refreshpol.o: $(MAIN_SRC)/refreshpol.c $(MAIN_SRC)/refreshpol.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/refreshpol.c

$(BUILD)/test_refreshpol.o: test_refreshpol.c $(MAIN_SRC)/refreshpol.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_refreshpol.c

$(BUILD)/test_refreshpol: $(BUILD)/refreshpol.o $(BUILD)/test_refreshpol.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/refreshpol.o $(BUILD)/test_refreshpol.o

$(BUILD)/temp_parse.o: $(MAIN_SRC)/temp_parse.c $(MAIN_SRC)/temp_parse.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/temp_parse.c

$(BUILD)/test_temp.o: test_temp.c $(MAIN_SRC)/temp_parse.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_temp.c

$(BUILD)/test_temp: $(BUILD)/temp_parse.o $(BUILD)/cbor.o $(BUILD)/test_temp.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/temp_parse.o $(BUILD)/cbor.o $(BUILD)/test_temp.o

$(BUILD)/devinfo_fmt.o: $(MAIN_SRC)/devinfo_fmt.c $(MAIN_SRC)/devinfo_fmt.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/devinfo_fmt.c

$(BUILD)/test_devinfo.o: test_devinfo.c $(MAIN_SRC)/devinfo_fmt.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_devinfo.c

$(BUILD)/test_devinfo: $(BUILD)/devinfo_fmt.o $(BUILD)/test_devinfo.o
	$(CC) $(CFLAGS) -o $@ $(BUILD)/devinfo_fmt.o $(BUILD)/test_devinfo.o
