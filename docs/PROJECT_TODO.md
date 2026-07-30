# Project TODO

- [pending] Generate and validate the declared `toolbox` mirror from the verified [`sync-source-lock.json`](../sync-source-lock.json), publish its receipt-bound immutable release, then update the private overlay from that exact toolbox release without editing generated files directly.
- [blocked-high-risk] Replace the flat private-control quarantine with a separately reviewed segmented-retention contract before production admission. The design must migrate exchange journals to exact version-3 root/segment/evidence locators, preserve legacy-flat recovery without cross-segment name search, bind immutable receipts, and enforce segment plus global entry/logical/allocated-byte ceilings; do not delete or relocate current retained evidence as an interim fix.
