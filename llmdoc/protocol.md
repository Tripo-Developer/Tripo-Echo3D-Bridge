# Echo3D desktop protocol

The Python desktop app owns localhost port 60710. Studio connects to `/`; the
Echo3D web integration connects to `/echo3d`. There is one connection per role.
Both send `handshake` with `payload.protocolVersion` 1.0.0 (1.1.0 accepted).
JSON control messages may be text or binary. The handshake succeeds even when
the other frontend is absent. `pong` includes current readiness flags.

Studio sends binary frames: UTF-8 JSON header immediately followed by raw GLB
bytes. Header: `type: file_transfer`, payload fields `fileId`, `fileName`,
`fileType: glb`, `chunkIndex`, `chunkTotal`, `chunkSize`. Indices start at 0.
The limit is 5 MiB per chunk plus 16 KiB header and 256 MiB per model.

Echo3D returns `file_transfer_ack` with `fileId`, `fileIndex`, `success` after
receiving each chunk. Studio must wait for the matching ACK before sending the
next chunk. The Bridge forwards bytes unchanged, never stores or imports them.
After assembly Echo3D sends `file_transfer_complete` with `status: success`.
After its actual operation it sends `import_complete` (or `file_loaded`) with
`fileId`, `success`, `message`. Bridge sends both result aliases to Studio.
Receipt timeout is 30 seconds; final operation timeout is 300 seconds.
Disconnection cancels the transfer. Reconnect and resend with a new fileId.

There is no room protocol, account login or cloud API in the desktop app.
The local demo accepts browser origins, including file:// (`Origin: null`),
on loopback only. Do not expose the listener on a public interface.
