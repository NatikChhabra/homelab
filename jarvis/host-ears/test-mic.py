"""Quick mic input test - measures RMS levels."""
import sounddevice as sd
import numpy as np
import time

print("Available input devices:")
for i, d in enumerate(sd.query_devices()):
    if d['max_input_channels'] > 0:
        print(f"  {i}: {d['name']} (channels={d['max_input_channels']}, rate={d['default_samplerate']})")

print("\nTesting mic input for 5 seconds...")
print("Please say 'hey jarvis' normally during this test.\n")

# Use device 1 (Microphone Array)
dev = 1
if sd.query_devices()[dev]['default_samplerate'] != 16000:
    print(f"Note: Device {dev} runs at {sd.query_devices()[dev]['default_samplerate']} Hz, resampling to 16kHz")

rec = sd.InputStream(device=dev, samplerate=16000, channels=1, dtype='int16')
rec.start()

print(f"Recording from: {sd.query_devices()[dev]['name']}")
print(f"{'Time':>6s} | {'RMS':>8s} | {'Peak':>8s}")
print("-" * 35)

start = time.time()
while time.time() - start < 5:
    time.sleep(0.1)
    # Get buffer
    try:
        data, overflowed = rec.read(1600)  # 0.1s at 16kHz
        if overflowed:
            continue
        rms = np.sqrt(np.mean(data.astype(float) ** 2))
        peak = np.max(np.abs(data.astype(float)))
        elapsed = time.time() - start
        print(f"{elapsed:6.1f} | {rms:8.0f} | {peak:8.0f}")
    except:
        pass

rec.stop()
rec.close()
print("\nTest complete.")
