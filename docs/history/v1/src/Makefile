# svc_watchdog build: `make host` (native, for unit tests) | `make cross` (mipsel-sf RUT)
CFLAGS ?= -Os -Wall -Wextra -std=gnu99
LDLIBS ?= -ljson-c

host: svc_watchdog
svc_watchdog: svc_watchdog.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS)

# Cross build: CROSS_CC + JSONC_INC/JSONC_LIB are provided by docker_build.sh
# (OpenWrt SDK toolchain gcc + json-c 0.15 headers + prebuilt libjson-c.so.5).
cross: svc_watchdog.mipsel
svc_watchdog.mipsel: svc_watchdog.c
	$(CROSS_CC) $(CFLAGS) -I$(JSONC_INC) -o $@ $< -L$(JSONC_LIB) -ljson-c
	$(CROSS_STRIP) $@
	@ls -la $@

clean:
	rm -f svc_watchdog svc_watchdog.mipsel
