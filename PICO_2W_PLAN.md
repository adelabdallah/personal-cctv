# Pico 2W glance camera

A Raspberry Pi Pico 2W, an SPI camera that emits JPEG, and a USB power supply can feed this app as a glance camera. The Pico posts one low-resolution JPEG about once a second, and the existing viewer shows that picture.

At one frame a second you can see that someone is standing at a door. A person walking through the view can pass between pictures.

## Hardware

- Raspberry Pi Pico 2W. 520 KB RAM, onboard 2.4 GHz Wi-Fi.
- An SPI camera that compresses JPEG on the module, such as an Arducam with an OV2640. SPI on that module tops out at 8 MHz. A CSI camera such as the Raspberry Pi Camera Module has no socket on a Pico.
- A USB power supply. The OV2640 module draws about 70 mA at 5 V while capturing, and the Pico adds Wi-Fi on top.

A 320×240 JPEG is tens of kilobytes and fits in the Pico's RAM. The camera emits the JPEG. The Pico reads that buffer and posts it.

Use the C SDK for the firmware. It has room for the JPEG and a secure request together. Keep the connection open. A fresh HTTPS handshake on every frame misses the one-second pace.

A measured Pico W with an OV2640 already moved 1600×1200 JPEGs at about 2 frames a second, so a smaller frame once a second sits inside that result.

## What must change in the app

The viewer can stay. `/stream/{camera_id}` repeats the latest JPEG as MJPEG, and `/snapshot/{camera_id}` returns that JPEG. A Pico frame belongs in the same slot.

Today each camera is a local device the server opens, `opencv` or `picamera2`, and the process listens on `127.0.0.1`. The browser login is a session cookie. The Pico needs an upload route of its own.

- [ ] Add a camera backend that stores JPEGs posted by the Pico
- [ ] Accept a JPEG on that route and store it as that camera's latest frame
- [ ] Authenticate the POST with a device token, separate from the viewer session cookie
- [ ] Give the Pico a stable address: a named Cloudflare tunnel, or a LAN address. A quick tunnel changes URL every start.
- [ ] Record when the frame arrived. The current slot has no time, so a quiet Pico leaves the last picture on screen with no sign that the camera has stalled

## Out of scope

- Recording, motion detection, and audio
- Treating this board as the Pi Zero 2 W CSI camera in [PI_ZERO_2_CCTV_PLAN.md](PI_ZERO_2_CCTV_PLAN.md)
- Pushing the Pico past about one frame a second
