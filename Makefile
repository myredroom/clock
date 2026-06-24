VERSION  := $(shell python3 -c "import re; m=re.search(r'^\s*version\s*=\s*[\"\'](.*?)[\"\']', open('pyproject.toml').read(), re.MULTILINE); print(m.group(1))")
PKG      := desktop-clock
PKG_     := desktop_clock
BUILD    := packaging/$(PKG)_$(VERSION)
DEB      := packaging/$(PKG)_$(VERSION).deb
DIST     := $(BUILD)/usr/lib/python3/dist-packages/$(PKG_)-$(VERSION).dist-info

.PHONY: deb clean

deb:
	@echo "Building $(PKG) version $(VERSION)..."

	# Recreate build directory cleanly
	rm -rf $(BUILD)
	mkdir -p $(BUILD)/DEBIAN
	mkdir -p $(BUILD)/usr/bin
	mkdir -p $(BUILD)/usr/share/$(PKG)
	mkdir -p $(BUILD)/usr/share/applications
	mkdir -p $(BUILD)/usr/share/icons/hicolor/48x48/apps
	mkdir -p $(BUILD)/usr/share/icons/hicolor/256x256/apps
	mkdir -p $(BUILD)/usr/share/doc/$(PKG)
	mkdir -p $(DIST)

	# Copy application
	cp clock.py $(BUILD)/usr/share/$(PKG)/

	# RTC wake helper + scoped passwordless sudoers (for "wake from sleep" alarms)
	mkdir -p $(BUILD)/usr/lib/$(PKG)
	mkdir -p $(BUILD)/etc/sudoers.d
	cp packaging/rtcwake-helper $(BUILD)/usr/lib/$(PKG)/rtcwake-helper
	chmod 755 $(BUILD)/usr/lib/$(PKG)/rtcwake-helper
	cp packaging/sudoers-desktop-clock $(BUILD)/etc/sudoers.d/$(PKG)
	chmod 440 $(BUILD)/etc/sudoers.d/$(PKG)
	cp packaging/postinst $(BUILD)/DEBIAN/postinst
	cp packaging/postrm  $(BUILD)/DEBIAN/postrm
	chmod 755 $(BUILD)/DEBIAN/postinst $(BUILD)/DEBIAN/postrm

	# Copy icons
	cp packaging/icons/desktop-clock-48.png  $(BUILD)/usr/share/icons/hicolor/48x48/apps/$(PKG).png
	cp packaging/icons/desktop-clock-256.png $(BUILD)/usr/share/icons/hicolor/256x256/apps/$(PKG).png

	# Write control file
	@printf 'Package: $(PKG)\nVersion: $(VERSION)\nArchitecture: all\nMaintainer: Brendan <myredroom@gmail.com>\nDepends: python3, python3-gi, python3-cairo, python3-xlib, gir1.2-gtk-3.0, gir1.2-pango-1.0, gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0\nDescription: Analog/digital desktop clock\n A Python GTK3 desktop clock with analog and digital display modes,\n alarms, timers, snap-to-corner, and per-monitor position memory.\n Runs as a lightweight always-on-top overlay with a system tray icon.\n HomieLab Tracker integration requires a local HomieLab Tracker instance.\n' \
		> $(BUILD)/DEBIAN/control

	# Write dist-info so importlib.metadata can report the installed version
	@printf 'Metadata-Version: 2.1\nName: $(PKG)\nVersion: $(VERSION)\nSummary: Analog/digital desktop clock with alarms, timers and snap-to-corner\nAuthor-email: Brendan <myredroom@gmail.com>\nLicense: MIT\n' \
		> $(DIST)/METADATA
	@printf 'dpkg\n' > $(DIST)/INSTALLER

	# Write launcher
	@printf '#!/bin/bash\nexec python3 /usr/share/$(PKG)/clock.py "$$@"\n' \
		> $(BUILD)/usr/bin/$(PKG)
	chmod 755 $(BUILD)/usr/bin/$(PKG)

	# Write .desktop file
	@printf '[Desktop Entry]\nName=Desktop Clock\nComment=Analog/digital desktop clock with alarms and timers\nExec=$(PKG)\nIcon=$(PKG)\nTerminal=false\nType=Application\nCategories=Utility;Clock;\nStartupNotify=false\n' \
		> $(BUILD)/usr/share/applications/$(PKG).desktop

	# Write copyright
	@printf 'Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\nUpstream-Name: $(PKG)\nUpstream-Contact: Brendan <myredroom@gmail.com>\n\nFiles: *\nCopyright: 2026 Brendan\nLicense: MIT\n' \
		> $(BUILD)/usr/share/doc/$(PKG)/copyright

	# Build .deb
	dpkg-deb --root-owner-group --build $(BUILD) $(DEB)
	@echo "Done: $(DEB)"

clean:
	rm -rf packaging/$(PKG)_*/
	rm -f packaging/$(PKG)_*.deb
