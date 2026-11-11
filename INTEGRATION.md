# Tripo to echo3D: cloud upload receiver with echo3D errors in the Bridge

This adds the echo3D upload workflow on top of the local bridge transfer: a cloud receiver uploads delivered models to echo3D and shows the actual echo3D error in the Bridge transfer panel and Logs, not only in the receiver terminal. No delivery ID, diagnose or reconciliation command is needed for normal uploads. The Bridge app itself is unchanged; the existing v0.2.2 install already displays the receiver's failure reason.

## Setup on macOS / Linux

Stop only the old receiver with Ctrl+C. Keep the existing Bridge running and its local cache and cloud-receipts.sqlite3 intact. Clone this repository, then open Terminal inside the Tripo-Echo3D-Bridge folder.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-cloud.txt
.venv/bin/python cloud_receiver.py
```

Enter collection key, account email, User Authentication Key and optional Security Key locally when prompted. Type UPLOAD to start. Keep this Terminal open and send models from Tripo as before. Close the browser demo receiver if it is connected: only one echo3D receiver can connect at a time.

On Windows: create the environment with `py -3 -m venv .venv` and use `.venv\Scripts\python.exe` instead of `.venv/bin/python` in the next commands.

## What you will see

Success prints `ACCEPTED by echo3D. Entry ...`. That entry ID is output, not input. Acceptance does not mean thumbnail generation or conversion has finished.

A rejected request now shows the HTTP status and echo3D response detail in the Bridge transfer panel and Logs, for example `Delivery failed: HTTP 400; echo3D: {"error": "Duplicate content"}; inspect the collection before retrying. Reconnect echo3D to retry.` The receiver terminal prints the same detail. Response text is capped, terminal control characters are removed, and supplied credentials / common credential fields are redacted before display. Do not post keys or unreviewed logs.

## Upload behavior

- ALL pending Bridge files and new files are processed, not one selected delivery. Uploads use the collection's normal storage/processing allowance. Existing queued copies may be rejected as duplicates.
- After an error, that file remains pending and the receiver can continue to other queued files. It does not automatically repeat a POST for the same delivery.
- The default existing receipt database is reused unchanged. It lives in the per-user application data folder, not the repository folder. If you previously used a custom --ledger path, keep that same option; do not start with an empty ledger. An accepted receipt avoids another upload for that delivery. An uncertain receipt blocks its automatic retry until collection state has been checked. Do not delete receipts or clear the queue to work around a block.
- Ctrl+C stops the receiver. If you interrupt an upload, check the collection before any retry: the server may already have accepted it.

If echo3D rejects a model as a duplicate, the Bridge now says so. A duplicate means that content already exists in the collection. Inspect the existing asset before retrying; do not delete it merely to clear the error. Replacing an existing asset remains your deliberate choice on the collection side, not a required step. No recovery action is required to upload a genuinely new file. Your local queued copies have not been checked; do not clear them blindly.

## Verification and limits

The upload receiver is new on the echo3D side; the local Bridge transfer flow, desktop app source and queued-file handling are unchanged from the original demo. 19 receiver tests, 8 protocol tests, 7 error/redaction tests and a real local HTTP + WebSocket queue-flow test passed (35 total). The latter runs an echo3D duplicate rejection (HTTP 400) followed by a successful next delivery through the real Bridge relay, and confirms the Bridge-facing delivery message carries the actual echo3D error text, redacted, while preserving both local files and receipts. The flow was verified manually on macOS against a live echo3D collection: transfer through the Bridge, a successful upload, and a duplicate rejection displayed in the Bridge. The Bridge-facing error display was also validated by rendering the shipped desktop source against a local test rejection; the installed macOS binary was not independently re-exercised. Windows is untested. End-to-end transfer triggered from Tripo Studio itself has not been verified. Local test servers are not echo3D production.

Python 3.10+, GLB v2 only, up to 256 MiB, one receiver at a time. The local Bridge has no pairing/origin controls; use a trusted computer. Do not expose port 60710 publicly.

Sources: built on this repository's bridge demo; echo3D upload API: https://docs.echo3d.com/api/upload
