"""Test wake word detection with confidence logging."""
import asyncio
import json
import numpy as np
import sounddevice as sd

RATE = 16000
BLOCK = 1280

async def test_wake_word():
    print("Connecting to openWakeWord...")
    reader, writer = await asyncio.open_connection('localhost', 10400)
    
    # Send detect event (manual Wyoming protocol)
    detect_event = json.dumps({"type": "detect", "detect": {"names": ["hey_jarvis"]}})
    start_event = json.dumps({"type": "audio-start", "audio-start": {"rate": RATE, "width": 2, "channels": 1, "timestamp": 0}})
    
    writer.write((detect_event + "\n").encode())
    writer.write((start_event + "\n").encode())
    await writer.drain()
    
    print("Listening for 'hey jarvis'...")
    print("Please say 'hey jarvis' 5 times now.\n")
    print(f"{'Time':>6s} | {'RMS':>8s} | {'Peak':>8s} | {'Wake':>20s}")
    print("-" * 50)
    
    start = asyncio.get_event_loop().time()
    detections = 0
    audio_queue = asyncio.Queue()
    
    # Start reading detections
    async def read_detections():
        nonlocal detections
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=0.1)
                if not line:
                    break
                data = json.loads(line.decode())
                if data.get("type") == "detection":
                    name = data.get("detection", {}).get("name", "unknown")
                    confidence = data.get("detection", {}).get("confidence", 0.0)
                    elapsed = asyncio.get_event_loop().time() - start
                    print(f"{elapsed:6.1f} | {'':8s} | {'':8s} | {name:>20s} ({confidence:.3f})")
                    detections += 1
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"Read error: {e}")
                break
    
    read_task = asyncio.create_task(read_detections())
    
    # Record and feed audio using callback
    print("Recording...")
    
    def callback(indata, frames, time, status):
        """Callback from sounddevice."""
        if status:
            print(f"Status: {status}")
        # Convert buffer to numpy array
        data = np.frombuffer(indata, dtype=np.int16)
        rms = np.sqrt(np.mean(data.astype(np.float64) ** 2))
        peak = np.max(np.abs(data.astype(np.float64)))
        elapsed = asyncio.get_event_loop().time() - start
        if rms > 150:
            print(f"{elapsed:6.1f} | {rms:8.0f} | {peak:8.0f}")
        audio_queue.put_nowait(data.tobytes())
    
    with sd.RawInputStream(samplerate=RATE, blocksize=BLOCK, device=1,
                           dtype="int16", channels=1, callback=callback):
        # Feed audio to openWakeWord
        while True:
            await asyncio.sleep(0.08)
            try:
                chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
                elapsed = asyncio.get_event_loop().time() - start
                
                # Send audio chunk
                chunk_event = json.dumps({
                    "type": "audio-chunk",
                    "audio-chunk": {
                        "rate": RATE,
                        "width": 2,
                        "channels": 1,
                        "timestamp": int(elapsed * 1000000)
                    }
                })
                writer.write((chunk_event + "\n").encode())
                writer.write(chunk)
                await writer.drain()
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"Error: {e}")
                break
    
    writer.close()
    read_task.cancel()
    try:
        await read_task
    except asyncio.CancelledError:
        pass
    
    print(f"\nTest complete. Detections: {detections}")

if __name__ == "__main__":
    asyncio.run(test_wake_word())
