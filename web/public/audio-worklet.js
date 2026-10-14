/**
 * Microphone tap. Forwards raw mono Float32 blocks to the main thread, which
 * resamples them to 16 kHz PCM16 before they reach the STT socket.
 */
class CaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length > 0) {
      this.port.postMessage(new Float32Array(channel));
    }
    return true;
  }
}

registerProcessor('rc-capture', CaptureProcessor);
