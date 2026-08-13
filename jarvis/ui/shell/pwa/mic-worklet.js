/**
 * AudioWorklet processor for the JARVIS PWA.
 *
 * Same 16 kHz mono int16 PCM output as the desktop mic-worklet.js.
 * Sends audio chunks to the main thread; main thread forwards as binary
 * WebSocket frames to the server.
 */
class MicProcessor extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [{ name: 'mute', defaultValue: 0 }];
  }

  constructor() {
    super();
    this.buffer = new Float32Array(1600);  // 100 ms @ 16 kHz
    this.idx = 0;
    this.muted = false;
    this.port.onmessage = (e) => {
      if (e.data.type === 'mute') this.muted = e.data.value;
    };
  }

  process(inputs, outputs) {
    const input = inputs[0];
    if (!input || input.length === 0 || this.muted) return true;
    const channel = input[0];

    for (let i = 0; i < channel.length; i++) {
      this.buffer[this.idx++] = channel[i];
      if (this.idx >= this.buffer.length) {
        // Convert float32 [-1,1] to int16 PCM.
        const pcm = new Int16Array(this.buffer);
        this.port.postMessage({
          type: 'audio',
          pcm: pcm.buffer,
        });
        this.idx = 0;
      }
    }
    return true;
  }
}

registerProcessor('mic-processor', MicProcessor);
