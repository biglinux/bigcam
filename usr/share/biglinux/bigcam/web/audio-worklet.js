"use strict";
// Resample the actual browser sample rate, retaining phase across variable-size
// render quanta. Emit 20 ms S16LE/mono packets; never assume a 128-sample block.
class BigCamPCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.phase = 0; this.sum = 0; this.count = 0;
    this.output = new ArrayBuffer(640); this.view = new DataView(this.output); this.offset = 0;
  }
  process(inputs) {
    const channels = inputs[0];
    if (!channels || !channels.length || !channels[0].length) return true;
    const ratio = 16000 / sampleRate;
    for (let i = 0; i < channels[0].length; ++i) {
      let sample = 0;
      for (const channel of channels) sample += channel[i] || 0;
      this.sum += sample / channels.length; ++this.count;
      this.phase += ratio;
      while (this.phase >= 1) {
        const value = this.count ? this.sum / this.count : sample / channels.length;
        this.view.setInt16(this.offset, Math.max(-32768, Math.min(32767, Math.round(value * 32768))), true);
        this.offset += 2; this.phase -= 1; this.sum = 0; this.count = 0;
        if (this.offset === 640) {
          this.port.postMessage(this.output, [this.output]);
          this.output = new ArrayBuffer(640); this.view = new DataView(this.output); this.offset = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("bigcam-pcm", BigCamPCM);
