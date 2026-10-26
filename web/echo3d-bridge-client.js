/** Partner-side DCC client. The desktop app has its own native UI. */
export class Echo3DBridgeClient {
  constructor({ url = 'ws://127.0.0.1:60710/', role = 'echo3d', onTransfer } = {}) {
    const endpoint = new URL(url)
    if (!['ws:', 'wss:'].includes(endpoint.protocol)) throw new TypeError('Expected ws or wss URL')
    if (!['studio', 'echo3d'].includes(role)) throw new TypeError('Invalid role')
    endpoint.pathname = role === 'echo3d' ? '/echo3d' : '/'
    this.url = endpoint.href; this.role = role; this.onTransfer = onTransfer
    this.socket = null; this.listeners = new Map(); this.waiters = new Set()
    this.incoming = null; this.sending = false; this.heartbeat = null
  }
  on(type, callback) {
    const list = this.listeners.get(type) || []; list.push(callback); this.listeners.set(type, list)
    return () => this.listeners.set(type, (this.listeners.get(type) || []).filter(fn => fn !== callback))
  }
  emit(type, payload) { for (const fn of this.listeners.get(type) || []) fn(payload) }
  send(type, payload = {}) {
    if (this.socket?.readyState !== WebSocket.OPEN) throw new Error('Bridge disconnected')
    this.socket.send(JSON.stringify({ type, payload }))
  }
  waitFor(type, match = () => true, timeout = 30000) {
    let waiter
    const promise = new Promise((resolve, reject) => {
      const done = (error, payload) => {
        clearTimeout(waiter.timer); this.waiters.delete(waiter)
        error ? reject(error) : resolve(payload)
      }
      waiter = { type, match, done, timer: setTimeout(() => done(new Error(`${type} timeout`)), timeout) }
      this.waiters.add(waiter)
    })
    // Observe early failures even while the sender is waiting for an ACK.
    promise.catch(() => {})
    return { promise, cancel: () => waiter.done(new Error('Operation cancelled')) }
  }
  dispatch(type, payload) {
    for (const waiter of [...this.waiters]) {
      if (waiter.type === type && waiter.match(payload)) waiter.done(null, payload)
      else if (type === 'file_loaded' && !payload.success && waiter.type === 'file_transfer_ack' && waiter.match(payload))
        waiter.done(new Error(payload.errorMessage || payload.message || 'Transfer failed'))
    }
    this.emit(type, payload)
  }
  async connect(timeout = 10000) {
    if (this.socket && this.socket.readyState < WebSocket.CLOSED) throw new Error('Already connecting or connected')
    const socket = this.socket = new WebSocket(this.url); socket.binaryType = 'arraybuffer'
    const handshake = this.waitFor('handshake_ack', () => true, timeout)
    let chain = Promise.resolve()
    socket.onopen = () => this.send('handshake', { clientName: this.role, protocolVersion: '1.0.0' })
    socket.onmessage = event => {
      chain = chain.then(() => this.receive(event.data)).catch(error => {
        this.emit('error', error); socket.close()
      })
    }
    socket.onerror = () => this.emit('error', new Error('WebSocket connection error'))
    socket.onclose = event => {
      clearInterval(this.heartbeat); this.incoming = null
      for (const waiter of [...this.waiters]) waiter.done(new Error('Bridge disconnected'))
      this.emit('close', event)
    }
    try {
      const ack = await handshake.promise
      if (!ack.success) throw new Error(ack.message || 'Handshake failed')
      this.heartbeat = setInterval(() => this.send('ping'), 5000)
      return this
    } catch (error) { socket.close(); throw error }
  }
  async receive(raw) {
    if (typeof raw === 'string') {
      const { type, payload } = JSON.parse(raw)
      if (type === 'transfer_cancelled') this.incoming = null
      this.dispatch(type, payload); return
    }
    const bytes = new Uint8Array(raw)
    let depth = 0, quoted = false, escaped = false, end = -1
    for (let i = 0; i < Math.min(bytes.length, 16384); i++) {
      const c = bytes[i]
      if (quoted) { if (escaped) escaped = false; else if (c === 92) escaped = true; else if (c === 34) quoted = false }
      else if (c === 34) quoted = true
      else if (c === 123) depth++
      else if (c === 125 && --depth === 0) { end = i + 1; break }
    }
    if (end < 0) throw new Error('Missing JSON header')
    const { type, payload: p } = JSON.parse(new TextDecoder().decode(bytes.subarray(0, end)))
    const data = bytes.slice(end)
    if (type !== 'file_transfer' || data.length !== p.chunkSize) throw new Error('Invalid file frame')
    // Without a callback, callers manage their own chunk handling and receipts.
    this.emit(type, { ...p, data })
    if (!this.onTransfer) return
    if (!this.incoming) this.incoming = { fileId: p.fileId, chunks: [], size: 0 }
    const transfer = this.incoming
    if (transfer.fileId !== p.fileId || transfer.chunks.length !== p.chunkIndex) throw new Error('Out-of-order transfer')
    transfer.size += data.length
    if (transfer.size > 256 * 1024 * 1024) throw new Error('Model exceeds 256 MiB')
    transfer.chunks.push(data)
    this.send('file_transfer_ack', { fileId: p.fileId, fileIndex: p.chunkIndex, success: true })
    if (transfer.chunks.length === p.chunkTotal) {
      const blob = new Blob(transfer.chunks, { type: 'model/gltf-binary' })
      this.send('file_transfer_complete', { fileId: p.fileId, status: 'success' })
      // Actual partner import is asynchronous; do not block heartbeat/cancellation.
      Promise.resolve().then(() => this.onTransfer(blob, p)).then(result => {
        if (this.incoming !== transfer) return
        if (!result || typeof result.success !== 'boolean') throw new Error('onTransfer must return { success, message }')
        this.send('import_complete', { ...result, fileId: p.fileId })
        this.incoming = null
      }).catch(error => {
        if (this.incoming !== transfer) return
        this.incoming = null
        if (this.socket?.readyState === WebSocket.OPEN)
          this.send('import_complete', { fileId: p.fileId, success: false, message: error.message })
      })
    }
  }
  async sendTransfer(blob, { fileId = crypto.randomUUID(), fileName = 'model.glb', chunkSize = 5 * 1024 * 1024 } = {}) {
    if (this.role !== 'studio' || this.sending) throw new Error('Only one Studio transfer at a time')
    if (!(blob instanceof Blob) || !blob.size || blob.size > 256 * 1024 * 1024) throw new Error('Model must be 1 byte to 256 MiB')
    if (!Number.isInteger(chunkSize) || chunkSize < 1 || chunkSize > 5 * 1024 * 1024) throw new Error('Invalid chunk size')
    const total = Math.ceil(blob.size / chunkSize)
    if (total > 1024) throw new Error('Too many chunks')
    this.sending = true
    const result = this.waitFor('file_loaded', p => p.fileId === fileId, 300000)
    try {
      for (let i = 0; i < total; i++) {
        const chunk = blob.slice(i * chunkSize, (i + 1) * chunkSize)
        const ack = this.waitFor('file_transfer_ack', p => p.fileId === fileId && (p.fileIndex === i || p.fileIndex === undefined))
        try {
          const header = { type: 'file_transfer', payload: { fileId, fileName, fileType: 'glb', chunkIndex: i, chunkTotal: total, chunkSize: chunk.size } }
          if (this.socket?.readyState !== WebSocket.OPEN) throw new Error('Bridge disconnected')
          this.socket.send(new Blob([JSON.stringify(header), chunk]))
          if (!(await ack.promise).success) throw new Error('Chunk rejected')
        } finally { ack.cancel() }
      }
      const completed = await result.promise
      if (!completed.success) throw new Error(completed.errorMessage || completed.message || 'Echo3D import failed')
      return completed
    } finally { result.cancel(); this.sending = false }
  }
  close() { clearInterval(this.heartbeat); this.socket?.close() }
}
