# Host unit tests for the pure firmware sections; no ESP-IDF needed.
# mbedtls is found via pkg-config, then Homebrew, then a bare -lmbedcrypto.
# `make test` runs every test binary; `make png` runs render_png.

CC ?= cc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O1 -g
INCLUDES := -I../main

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

$(BUILD)/input.o: $(MAIN_SRC)/input.c $(MAIN_SRC)/input.h | $(BUILD)

$(BUILD)/test_input.o: test_input.c $(MAIN_SRC)/input.h | $(BUILD)


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

$(BUILD)/catrust.o: $(MAIN_SRC)/catrust.c $(MAIN_SRC)/catrust.h $(MAIN_SRC)/cbor.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ $(MAIN_SRC)/catrust.c

$(BUILD)/test_catrust.o: test_catrust.c $(MAIN_SRC)/catrust.h | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDES) -c -o $@ test_catrust.c

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
