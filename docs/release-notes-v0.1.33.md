# v0.1.33 - Embedded live run console, Windows portable rebuild

This historical Windows rebuild carries the embedded live run console and the
release-blocker compatibility fix for report-list session creation. The
original v0.1.33 source commit remains the release identity; the compatibility
fix is recorded by the historical build workflow.

## Windows portable download

Download `Smart_Commissioning_App_Windows_Portable.zip`, extract it into a new
empty directory, and run `SmartCommissioningApp.exe`.

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`

The ZIP was built from the annotated v0.1.33 tag, tested with the core and
backend suites, boot-smoked from paths containing spaces, and stamped as
v0.1.33.
