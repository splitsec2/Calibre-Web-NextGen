### Added

- **Choose the books each Kobo keeps from Devices.** Kobo rows now offer the same account, library and shelf choices as KOReader. Each Kobo removes only its own delivered books when they leave its scope, including archived, hidden and no-longer-visible books.

### Fixed

- **Keep each Kobo’s saved place, highlights and notes through removal.** Removal acknowledgements keep other devices’ deliveries and reading journals. A known re-download restores the device’s own saved place, unless a newer shared place wins. Reading activity without a later annotation upload holds removal for three eligible syncs, then logs a warning and removes it on the fourth. Permanent deletion retains only minimal UUID-bound capture evidence until acknowledgement; its existing content purge still applies.
