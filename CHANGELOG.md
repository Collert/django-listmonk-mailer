# Changelog

## 0.1.0 (unreleased)

- First release: a Django 6.1 `MAILERS` backend that sends through listmonk's
  transactional API (`POST /api/tx`), with HTML/plain bodies, attachments,
  headers, per-message overrides and `fail_silently` support.
