// 送信待ちキュー。
// 録音したチャンクはまず端末内（IndexedDB）に保存し、送信に成功したら消す。
// 通信が切れても、ページを再読み込みしても、残ったものを順に再送する。

export class NonRetryableError extends Error {
  constructor(message) {
    super(message);
    this.name = "NonRetryableError";
  }
}

export class UploadQueue {
  /**
   * @param {{
   *   store: { put(item: object): Promise<void>, delete(key: string): Promise<void>, list(): Promise<object[]> },
   *   uploader: (item: object) => Promise<void>,
   *   onChange?: (stats: object) => void,
   *   onFatal?: (error: Error) => void,
   *   sleep?: (ms: number, signal: { aborted: boolean, wake?: () => void }) => Promise<void>,
   *   baseBackoffMs?: number, maxBackoffMs?: number,
   * }} opts
   */
  constructor(opts) {
    this.store = opts.store;
    this.uploader = opts.uploader;
    this.onChange = opts.onChange ?? (() => {});
    this.onFatal = opts.onFatal ?? (() => {});
    this.baseBackoffMs = opts.baseBackoffMs ?? 2000;
    this.maxBackoffMs = opts.maxBackoffMs ?? 60000;
    this._sleep = opts.sleep ?? defaultSleep;
    this._running = false;
    this._loop = null;
    this._waker = null;
    this.stats = { pending: 0, uploaded: 0, failedAttempts: 0, consecutiveFailures: 0, lastError: null };
  }

  async add(item) {
    await this.store.put(item);
    this.stats.pending += 1;
    this._emit();
    this.kick();
  }

  /** 送信を再開する（オンライン復帰・画面復帰のとき呼ぶ）。待機中のバックオフも打ち切る。 */
  kick() {
    if (this._waker) this._waker();
  }

  async start() {
    if (this._running) return;
    this._running = true;
    this.stats.pending = (await this.store.list()).length;
    this._emit();
    this._loop = this._run();
  }

  async stop() {
    this._running = false;
    this.kick();
    if (this._loop) await this._loop;
    this._loop = null;
  }

  /** キューが空になるまで待つ。タイムアウトしたら false。 */
  async drain(timeoutMs = 120000, pollMs = 200) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if ((await this.store.list()).length === 0) return true;
      this.kick();
      await new Promise((r) => setTimeout(r, pollMs));
    }
    return false;
  }

  _emit() {
    this.onChange({ ...this.stats });
  }

  /** ms 待つか、kick() で起こされるまで待つ。ms が null なら kick() まで待つ。 */
  async _wait(ms) {
    const signal = { aborted: false };
    const woke = new Promise((resolve) => {
      this._waker = () => {
        signal.aborted = true;
        if (signal.wake) signal.wake();
        resolve();
      };
    });
    await (ms === null ? woke : Promise.race([this._sleep(ms, signal), woke]));
    this._waker = null;
  }

  async _run() {
    while (this._running) {
      const items = await this.store.list();
      this.stats.pending = items.length;
      if (items.length === 0) {
        await this._wait(null);
        continue;
      }
      const item = items[0];
      try {
        await this.uploader(item);
        await this.store.delete(item.key);
        this.stats.uploaded += 1;
        this.stats.pending = Math.max(0, this.stats.pending - 1);
        this.stats.consecutiveFailures = 0;
        this.stats.lastError = null;
        this._emit();
      } catch (err) {
        this.stats.failedAttempts += 1;
        this.stats.consecutiveFailures += 1;
        this.stats.lastError = String(err && err.message ? err.message : err);
        this._emit();
        if (err instanceof NonRetryableError) {
          this._running = false;
          this.onFatal(err);
          return;
        }
        const backoff = Math.min(this.maxBackoffMs, this.baseBackoffMs * 2 ** (this.stats.consecutiveFailures - 1));
        await this._wait(backoff);
      }
    }
  }
}

function defaultSleep(ms, signal) {
  return new Promise((resolve) => {
    const id = setTimeout(resolve, ms);
    signal.wake = () => {
      clearTimeout(id);
      resolve();
    };
  });
}

// ---- 保存先 ----

/** テスト用・IndexedDB が使えないときの退避先（ページを閉じると消える）。 */
export class MemoryStore {
  constructor() {
    this.items = new Map();
    this.meta = null;
  }
  async getMeta() {
    return this.meta;
  }
  async putMeta(meta) {
    this.meta = { ...meta };
  }
  async put(item) {
    this.items.set(item.key, item);
  }
  async delete(key) {
    this.items.delete(key);
  }
  async list() {
    return [...this.items.values()].sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  }
}

/** IndexedDB。キーは "<recordId>/<sessionId>/<seq 6桁>" なので、辞書順が録音順になる。 */
export class IdbStore {
  constructor(recordId, dbName = "meeting-recorder") {
    this.recordId = recordId;
    this.dbName = dbName;
    this._db = null;
  }

  static available() {
    return typeof indexedDB !== "undefined";
  }

  async _open() {
    if (this._db) return this._db;
    this._db = await new Promise((resolve, reject) => {
      const req = indexedDB.open(this.dbName, 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains("chunks")) db.createObjectStore("chunks", { keyPath: "key" });
        if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta", { keyPath: "recordId" });
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return this._db;
  }

  async _tx(storeName, mode, fn) {
    const db = await this._open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(storeName, mode);
      const store = tx.objectStore(storeName);
      let result;
      const req = fn(store);
      if (req) req.onsuccess = () => (result = req.result);
      tx.oncomplete = () => resolve(result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  }

  async put(item) {
    await this._tx("chunks", "readwrite", (s) => s.put(item));
  }

  async delete(key) {
    await this._tx("chunks", "readwrite", (s) => s.delete(key));
  }

  async list() {
    const prefix = `${this.recordId}/`;
    const range = IDBKeyRange.bound(prefix, `${prefix}￿`);
    const items = await this._tx("chunks", "readonly", (s) => s.getAll(range));
    return items ?? [];
  }

  async getMeta() {
    return (await this._tx("meta", "readonly", (s) => s.get(this.recordId))) ?? null;
  }

  async putMeta(meta) {
    await this._tx("meta", "readwrite", (s) => s.put({ ...meta, recordId: this.recordId }));
  }
}

export function chunkKey(recordId, sessionId, seq) {
  return `${recordId}/${sessionId}/${String(seq).padStart(6, "0")}`;
}
