// 録音停止の警告音。
// iOS では利用者の操作なしに音を鳴らせないため、「録音開始」を押したときに unlock() で
// audio 要素を一度再生しておき、後から警告音を再生できるようにする。

function toneWavDataUri({ freq = 880, ms = 250, gapMs = 120, repeat = 3, rate = 8000 } = {}) {
  const toneSamples = Math.floor((rate * ms) / 1000);
  const gapSamples = Math.floor((rate * gapMs) / 1000);
  const total = (toneSamples + gapSamples) * repeat;
  const buffer = new Uint8Array(44 + total);
  const view = new DataView(buffer.buffer);
  const writeStr = (offset, s) => [...s].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + total, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate, true);
  view.setUint16(32, 1, true);
  view.setUint16(34, 8, true);
  writeStr(36, "data");
  view.setUint32(40, total, true);
  let p = 44;
  for (let r = 0; r < repeat; r++) {
    for (let i = 0; i < toneSamples; i++) buffer[p++] = 128 + Math.round(110 * Math.sin((2 * Math.PI * freq * i) / rate));
    for (let i = 0; i < gapSamples; i++) buffer[p++] = 128;
  }
  let binary = "";
  for (let i = 0; i < buffer.length; i++) binary += String.fromCharCode(buffer[i]);
  return `data:audio/wav;base64,${btoa(binary)}`;
}

export class Alarm {
  constructor() {
    this.silent = toneWavDataUri({ freq: 0, ms: 50, gapMs: 0, repeat: 1 });
    this.beep = toneWavDataUri();
    this.audio = typeof Audio !== "undefined" ? new Audio() : null;
    this._timer = null;
    this.unlocked = false;
  }

  /** 利用者の操作（クリック）の中で呼ぶ。 */
  async unlock() {
    if (!this.audio) return false;
    try {
      this.audio.src = this.silent;
      await this.audio.play();
      this.audio.pause();
      this.unlocked = true;
    } catch {
      this.unlocked = false;
    }
    return this.unlocked;
  }

  /** 警告を鳴らし続ける（stop() まで数秒おき）。鳴らせたかどうかを返す。 */
  async start() {
    this.stop();
    const ok = await this._play();
    this._timer = setInterval(() => this._play(), 3000);
    if (typeof navigator !== "undefined" && typeof navigator.vibrate === "function") {
      navigator.vibrate([400, 150, 400, 150, 400]);
    }
    return ok;
  }

  stop() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
    if (this.audio) this.audio.pause();
  }

  async _play() {
    if (!this.audio) return false;
    try {
      this.audio.src = this.beep;
      this.audio.currentTime = 0;
      await this.audio.play();
      return true;
    } catch {
      return false;
    }
  }
}
