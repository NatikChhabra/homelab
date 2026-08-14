"""Check mic volume and levels."""
import sounddevice as sd
import numpy as np
import time

print("=== Mic Input Test ===")
print(f"Available devices:\n")

for i, d in enumerate(sd.query_devices()):
    if d['max_input_channels'] > 0:
        print(f"  {i}: {d['name']}")
        print(f"      Channels: {d['max_input_channels']}, Rate: {d['default_samplerate']} Hz")

print("\n" + "="*50)
print("Testing mic input for 5 seconds...")
print("Please say 'hey jarvis' normally during this test.\n")

# Test device 1 (Microphone Array)
dev = 1
rec = sd.InputStream(device=dev, samplerate=16000, channels=1, dtype='int16')
rec.start()

print(f"Device: {sd.query_devices()[dev]['name']}")
print(f"{'Time':>6s} | {'RMS':>8s} | {'Peak':>8s} | {'Level':>6s}")
print("-" * 45)

rms_values = []
peak_values = []
start = time.time()

while time.time() - start < 5:
    time.sleep(0.01)
    try:
        data, overflowed = rec.read(160)  # 10ms chunks
        if overflowed:
            continue
        rms = np.sqrt(np.mean(data.astype(float) ** 2))
        peak = np.max(np.abs(data.astype(float)))
        rms_values.append(rms)
        peak_values.append(peak)
        elapsed = time.time() - start
        # Only print every 0.1s
        if int(elapsed * 10) != int((elapsed - 0.01) * 10):
            print(f"{elapsed:6.1f} | {rms:8.0f} | {peak:8.0f}")
    except Exception as e:
        print(f"Error: {e}")
        break

rec.stop()
rec.close()

print("\n" + "="*50)
print("STATISTICS:")
print(f"  RMS:  min={min(rms_values):.0f}, max={max(rms_values):.0f}, avg={np.mean(rms_values):.0f}")
print(f"  Peak: min={min(peak_values):.0f}, max={max(peak_values):.0f}, avg={np.mean(peak_values):.0f}")
print(f"\n  Current SILENCE_RMS threshold: 500")
print(f"  Current 'too quiet' threshold: 250 (500 * 0.5)")
print(f"\n  Recommendation: Increase Windows mic volume to {int(1000/max(np.mean(rms_values), 1))}x")
