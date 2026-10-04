// Mic capture worklet: resample the graph rate toward 16 kHz, emit
// 100 ms s16le frames (1600 samples) to the main thread.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / 16000;   // 1 when the graph runs at 16 kHz
    this.pos = 0; this.buf = [];
  }
  process(inputs) {
    const ch = inputs[0][0];
    if (!ch) return true;
    if (this.step === 1) {
      for (let i = 0; i < ch.length; i++) this.buf.push(ch[i]);
    } else {
      // linear resample toward 16 kHz (browsers ignoring the rate hint)
      let pos = this.pos;
      while (pos < ch.length) {
        const i = pos | 0, f = pos - i;
        const a = ch[i], b = ch[Math.min(i + 1, ch.length - 1)];
        this.buf.push(a + (b - a) * f);
        pos += this.step;
      }
      this.pos = pos - ch.length;
    }
    while (this.buf.length >= 1600) {
      const frame = this.buf.splice(0, 1600);
      const pcm = new Int16Array(1600);
      for (let j = 0; j < 1600; j++) {
        pcm[j] = Math.max(-32768, Math.min(32767, frame[j] * 32767));
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}
registerProcessor("capture", Capture);
