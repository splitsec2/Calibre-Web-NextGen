### Fixed

- **Kobo exact resume allows time for cold alignment.** With Share my place on, a proven Kobo place can open the web reader exactly even when the alignment cache is cold. This conversion has a bounded 250 ms default wait; stalled conversions still return the percentage offer. An explicit `CWA_KOBO_RESUME_TIMEOUT_SECONDS` overrides the default.
