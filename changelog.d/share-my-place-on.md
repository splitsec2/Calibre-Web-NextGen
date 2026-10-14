### Changed

- **Place sharing is now a choice for each device.** Every device starts with **Share my place** off, including your existing devices and the web reader. Each keeps its own place and read status. To share again, open **Account → Devices** and tick **Share my place** on each device you want to sync.
- **Each device gets back only its own highlights from CWNG.** When CWNG sends highlights to an e-reader, it now sends only the ones made on that device, not ones from the web reader or your other devices. Every device's highlights and notes still show together in the web reader, labelled by device. Older highlights without a recorded device remain available to readers of their original kind.
- **Kobo's own cloud sync can still share highlights between Kobos for a while.** Until CWNG has fully imported a book's highlights from your Kobo, CWNG passes that book's highlights through from Kobo's cloud, so a highlight made on another Kobo signed in to the same Kobo account can still appear.
- **KOReader needs the paired CWNG plugin for per-device control and device-only highlights.** A Kobo synced to CWNG also supports per-device control. E-readers that CWNG cannot identify keep their existing sync behaviour.
- **Administrators can temporarily restore the previous place and read-status sync behaviour with `CWNG_SHARE_PLACE_GATES=0`.** This emergency switch leaves device-only highlights in effect.

### Fixed

- **An identified KOReader device cannot change or remove another device's highlights.** Copies left on the device from earlier syncs leave their owner's highlights and notes unchanged.
- **Open without saving progress keeps your reading status unchanged**, including when the web reader has Share my place turned on.
- **The web reader keeps your place private if CWNG cannot register it.** If CWNG temporarily cannot register the web reader, its reading place stays private while Hardcover progress updates continue as they do for an unchecked web reader.
