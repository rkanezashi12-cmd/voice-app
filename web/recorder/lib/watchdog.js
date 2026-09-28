// 時間飛びの検知。
// 画面ロックやアプリ切り替えで JS が止まると、setInterval の間隔が大きく空く。
// その空白を「録音が止まっていたかもしれない時間」として報告する。

export class GapWatchdog {
  /**
   * @param {{ intervalMs?: number, gapThresholdMs?: number, now?: () => number,
   *           setInterval?: typeof setInterval, clearInterval?: typeof clearInterval,
   *           onGap: (gap: { startedAt: number, endedAt: number, durationMs: number }) => void }} opts
   */
  constructor(opts) {
    this.intervalMs = opts.intervalMs ?? 1000;
    this.gapThresholdMs = opts.gapThresholdMs ?? 5000;
    this.now = opts.now ?? (() => Date.now());
    this._setInterval = opts.setInterval ?? ((fn, ms) => setInterval(fn, ms));
    this._clearInterval = opts.clearInterval ?? ((id) => clearInterval(id));
    this.onGap = opts.onGap;
    this._timer = null;
    this._last = null;
  }

  start() {
    this.stop();
    this._last = this.now();
    this._timer = this._setInterval(() => this.tick(), this.intervalMs);
  }

  stop() {
    if (this._timer !== null) this._clearInterval(this._timer);
    this._timer = null;
    this._last = null;
  }

  /** テストから直接呼べるように公開している。 */
  tick() {
    if (this._last === null) return;
    const now = this.now();
    const elapsed = now - this._last;
    this._last = now;
    if (elapsed - this.intervalMs >= this.gapThresholdMs) {
      this.onGap({ startedAt: now - elapsed, endedAt: now, durationMs: elapsed - this.intervalMs });
    }
  }
}

// 音声レベルの監視。iOS では割り込み中にマイクが無音（0）を返し続けることがある。
// ほぼ完全な無音が続いたら報告する（会議室の小さな声は拾えるよう、しきい値はごく小さくする）。
export class SilenceDetector {
  /**
   * @param {{ silenceMs?: number, threshold?: number, now?: () => number,
   *           onSilence: (durationMs: number) => void, onSound?: () => void }} opts
   */
  constructor(opts) {
    this.silenceMs = opts.silenceMs ?? 10000;
    this.threshold = opts.threshold ?? 1e-4;
    this.now = opts.now ?? (() => Date.now());
    this.onSilence = opts.onSilence;
    this.onSound = opts.onSound ?? (() => {});
    this._silentSince = null;
    this._reported = false;
  }

  /** @param {number} peak 直近フレームの最大振幅（0〜1） */
  push(peak) {
    const now = this.now();
    if (peak > this.threshold) {
      if (this._reported) this.onSound();
      this._silentSince = null;
      this._reported = false;
      return;
    }
    if (this._silentSince === null) this._silentSince = now;
    if (!this._reported && now - this._silentSince >= this.silenceMs) {
      this._reported = true;
      this.onSilence(now - this._silentSince);
    }
  }

  reset() {
    this._silentSince = null;
    this._reported = false;
  }
}
