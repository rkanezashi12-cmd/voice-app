// MediaRecorder を包んで、一定時間ごとにチャンクを出す。
//
// 方式 a（既定）: chunkMs ごとに録音を止めて新しい MediaRecorder で録り直す。
//   各チャンクが単体で再生できる完全なファイルになるので、一部が届かなくても残りは使える。
//   区切りの瞬間に数十ミリ秒の欠落が出る可能性がある（テストで rotation_gap として計測する）。
// 方式 b: MediaRecorder の timeslice で分割する。途切れないが、先頭以外のチャンクは単体で再生できず、
//   サーバー側で順に連結して1本に戻す。

export const MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/mp4;codecs=mp4a.40.2",
  "audio/mp4",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/aac",
];

export function pickMimeType(MR) {
  if (!MR || typeof MR.isTypeSupported !== "function") return "";
  for (const candidate of MIME_CANDIDATES) {
    try {
      if (MR.isTypeSupported(candidate)) return candidate;
    } catch {
      // 判定できない形式は飛ばす
    }
  }
  return "";
}

/** サーバーと取り決めたセッション ID: <開始時刻 epoch ms>-<方式>-<乱数6桁> */
export function newSessionId(mode, now = Date.now(), rand = Math.random) {
  const r = Math.floor(rand() * 36 ** 6)
    .toString(36)
    .padStart(6, "0");
  return `${now}-${mode}-${r}`;
}

export class ChunkedRecorder {
  /**
   * @param {{
   *   stream: MediaStream, mode: "a"|"b", chunkMs: number, MediaRecorder: typeof MediaRecorder,
   *   mimeType?: string, audioBitsPerSecond?: number,
   *   onChunk: (c: { sessionId: string, seq: number, blob: Blob, mimeType: string, startedAt: number, endedAt: number }) => void,
   *   onUnexpectedStop?: (info: { reason: string, error?: unknown }) => void,
   *   onEvent?: (type: string, detail?: object) => void,
   *   now?: () => number, setTimeout?: typeof setTimeout, clearTimeout?: typeof clearTimeout,
   *   sessionId?: string,
   * }} opts
   */
  constructor(opts) {
    this.stream = opts.stream;
    this.mode = opts.mode === "b" ? "b" : "a";
    this.chunkMs = opts.chunkMs;
    this.MR = opts.MediaRecorder;
    this.mimeType = opts.mimeType ?? pickMimeType(this.MR);
    this.audioBitsPerSecond = opts.audioBitsPerSecond ?? 64000;
    this.onChunk = opts.onChunk;
    this.onUnexpectedStop = opts.onUnexpectedStop ?? (() => {});
    this.onEvent = opts.onEvent ?? (() => {});
    this.now = opts.now ?? (() => Date.now());
    this._setTimeout = opts.setTimeout ?? ((fn, ms) => setTimeout(fn, ms));
    this._clearTimeout = opts.clearTimeout ?? ((id) => clearTimeout(id));
    this.sessionId = opts.sessionId ?? newSessionId(this.mode, this.now());
    this.seq = 0;
    this.state = "idle"; // idle | recording | stopping | stopped | failed
    this.lastChunkAt = null;
    this._rec = null;
    this._timer = null;
    this._rotating = false;
    this._rotateRequestedAt = null;
    this._segmentStartedAt = null;
    this._stopResolve = null;
  }

  start() {
    if (this.state !== "idle") throw new Error("already started");
    this.state = "recording";
    this.lastChunkAt = this.now();
    if (this.mode === "a") {
      this._startSegment();
    } else {
      const rec = this._newRecorder();
      this._segmentStartedAt = this.now();
      rec.start(this.chunkMs);
    }
  }

  /** 録音を止め、最後のチャンクが出るまで待つ。 */
  stop() {
    if (this.state !== "recording") return Promise.resolve();
    this.state = "stopping";
    if (this._timer !== null) this._clearTimeout(this._timer);
    this._timer = null;
    return new Promise((resolve) => {
      this._stopResolve = resolve;
      // 区切り直しの途中なら、その stop イベント（最後のチャンクの後に来る）で完了する
      if (this._rotating) return;
      const rec = this._rec;
      if (!rec || rec.state === "inactive") {
        this._finishStop();
        return;
      }
      try {
        rec.stop();
      } catch (error) {
        this.onEvent("recorder_stop_error", { message: String(error) });
        this._finishStop();
      }
    });
  }

  _finishStop() {
    this.state = "stopped";
    const resolve = this._stopResolve;
    this._stopResolve = null;
    if (resolve) resolve();
  }

  get recorderState() {
    return this._rec ? this._rec.state : "inactive";
  }

  _options() {
    const options = { audioBitsPerSecond: this.audioBitsPerSecond };
    if (this.mimeType) options.mimeType = this.mimeType;
    return options;
  }

  _newRecorder() {
    const rec = new this.MR(this.stream, this._options());
    this._rec = rec;
    rec.ondataavailable = (e) => this._onData(rec, e);
    rec.onstop = () => this._onStop(rec);
    rec.onerror = (e) => {
      const error = e && e.error ? e.error : e;
      this.onEvent("recorder_error", { message: String(error && error.name ? error.name : error) });
    };
    rec.onpause = () => this.onEvent("recorder_pause");
    rec.onresume = () => this.onEvent("recorder_resume");
    return rec;
  }

  _startSegment() {
    let rec;
    try {
      rec = this._newRecorder();
      this._segmentStartedAt = this.now();
      rec.start();
    } catch (error) {
      this._fail("start_failed", error);
      return;
    }
    if (this._rotateRequestedAt !== null) {
      this.onEvent("rotation_gap", { gap_ms: this.now() - this._rotateRequestedAt, seq: this.seq });
      this._rotateRequestedAt = null;
    }
    this._timer = this._setTimeout(() => this._rotate(), this.chunkMs);
  }

  _rotate() {
    this._timer = null;
    if (this.state !== "recording" || !this._rec || this._rec.state === "inactive") return;
    this._rotating = true;
    this._rotateRequestedAt = this.now();
    try {
      this._rec.stop();
    } catch (error) {
      this._rotating = false;
      this._fail("rotate_failed", error);
    }
  }

  _onData(rec, e) {
    if (!e || !e.data || e.data.size === 0) return;
    const endedAt = this.now();
    const chunk = {
      sessionId: this.sessionId,
      seq: this.seq,
      blob: e.data,
      mimeType: e.data.type || rec.mimeType || this.mimeType || "audio/webm",
      startedAt: this._segmentStartedAt ?? endedAt,
      endedAt,
    };
    this.seq += 1;
    this.lastChunkAt = endedAt;
    this._segmentStartedAt = endedAt;
    this.onChunk(chunk);
  }

  _onStop(rec) {
    if (rec !== this._rec) return;
    if (this.state === "stopping") {
      this._rotating = false;
      this._finishStop();
      return;
    }
    if (this.mode === "a" && this._rotating) {
      this._rotating = false;
      if (this.state === "recording") this._startSegment();
      return;
    }
    // 録音中なのに止まった（マイクの終了、OS による中断など）
    this._fail("recorder_stopped");
  }

  _fail(reason, error) {
    if (this._timer !== null) this._clearTimeout(this._timer);
    this._timer = null;
    this.state = "failed";
    this.onUnexpectedStop({ reason, error });
  }
}
