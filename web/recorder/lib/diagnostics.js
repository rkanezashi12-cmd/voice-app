// 診断イベント。停止検知・送信失敗などを記録し、まとめてサーバーのログに送る。
// 文字起こし本文や音声は含めない（数値と短い文字列だけ）。

export class Diagnostics {
  /**
   * @param {{ send: (events: object[]) => Promise<boolean>, now?: () => number,
   *           onLog?: (entry: object) => void, maxBuffer?: number, batchSize?: number }} opts
   */
  constructor(opts) {
    this.send = opts.send;
    this.now = opts.now ?? (() => Date.now());
    this.onLog = opts.onLog ?? (() => {});
    this.maxBuffer = opts.maxBuffer ?? 1000;
    this.batchSize = opts.batchSize ?? 100;
    this.pending = [];
    this.history = [];
    this.counters = {};
    this._flushing = false;
  }

  log(type, detail = {}, sessionId = null) {
    const entry = { type, at: this.now(), session_id: sessionId, detail: sanitize(detail) };
    this.pending.push(entry);
    if (this.pending.length > this.maxBuffer) this.pending.splice(0, this.pending.length - this.maxBuffer);
    this.history.push(entry);
    if (this.history.length > 300) this.history.shift();
    this.counters[type] = (this.counters[type] ?? 0) + 1;
    this.onLog(entry);
    return entry;
  }

  async flush() {
    if (this._flushing || this.pending.length === 0) return;
    this._flushing = true;
    try {
      while (this.pending.length > 0) {
        const batch = this.pending.slice(0, this.batchSize);
        const ok = await this.send(batch);
        if (!ok) break;
        this.pending.splice(0, batch.length);
      }
    } catch {
      // 送れなければ次の機会に送る
    } finally {
      this._flushing = false;
    }
  }
}

function sanitize(detail) {
  const out = {};
  let n = 0;
  for (const [key, value] of Object.entries(detail ?? {})) {
    if (n >= 20) break;
    if (!/^[A-Za-z0-9_]{1,40}$/.test(key)) continue;
    if (value === null || typeof value === "number" || typeof value === "boolean") {
      out[key] = value;
    } else {
      out[key] = String(value).slice(0, 120);
    }
    n += 1;
  }
  return out;
}

/** 画面に出す「診断結果」。テスト結果の記録表にそのまま貼れる形にする。 */
export function buildSummary({ counters, startedAt, now, uploaded, pending, failedAttempts, gaps, mimeType, mode,
  chunkSeconds, wakeLock, userAgent, rotationGaps }) {
  const totalGapMs = gaps.reduce((a, g) => a + g.durationMs, 0);
  const sortedRotation = [...rotationGaps].sort((a, b) => a - b);
  return {
    user_agent: userAgent,
    mime_type: mimeType,
    mode,
    chunk_seconds: chunkSeconds,
    wake_lock: wakeLock,
    elapsed_seconds: startedAt != null ? Math.round((now - startedAt) / 1000) : 0,
    chunks_uploaded: uploaded,
    chunks_pending: pending,
    upload_failures: failedAttempts,
    interruptions: counters.interrupted ?? 0,
    timer_gaps: gaps.length,
    timer_gap_total_seconds: Math.round(totalGapMs / 1000),
    hidden_count: counters.hidden ?? 0,
    track_mute_count: counters.track_mute ?? 0,
    silence_warnings: counters.silence ?? 0,
    rotation_gap_ms_median: sortedRotation.length ? sortedRotation[Math.floor(sortedRotation.length / 2)] : null,
    rotation_gap_ms_max: sortedRotation.length ? sortedRotation[sortedRotation.length - 1] : null,
  };
}
