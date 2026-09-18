"""
Project 4: Pocket DJ

A handheld 3-pad musical instrument:
- Pad 0 (GPIO 21): Plays a rhythmic beat loop across the pads.
- Pad 1 (GPIO 7): Plays a mid pitch note with green LED pulse.
- Pad 2 (GPIO 6): Plays a high pitch note with blue LED pulse.
"""

import time
from machine import Pin, PWM
import neopixel
from debounced_button import DebouncedButton

PIN_SPEAKER = 10
PIN_PIXELS = 20
NUM_PIXELS = 3

PIN_PAD0 = 21
PIN_PAD1 = 7
PIN_PAD2 = 6

DUTY = 512  # 50% duty cycle for speaker volume

# Definition of sound and visual properties for each pad
# (frequency in Hz, duration in ms, pixel index 0-2, RGB color)
PADS = (
    {"freq": 262, "duration_ms": 150, "pixel": 0, "color": (30, 0, 0)},   # C4 (Low / Red)
    {"freq": 330, "duration_ms": 150, "pixel": 1, "color": (0, 30, 0)},   # E4 (Mid / Green)
    {"freq": 392, "duration_ms": 150, "pixel": 2, "color": (0, 0, 30)},   # G4 (High / Blue)
)

# 4-step rhythm loop played by Pad 0: (pad_index, rest_after_ms)
LOOP = (
    (0, 80),
    (2, 60),
    (1, 80),
    (2, 120),
)

pixels = neopixel.NeoPixel(Pin(PIN_PIXELS, Pin.OUT), NUM_PIXELS)


def clear_pixels():
    for i in range(NUM_PIXELS):
        pixels[i] = (0, 0, 0)
    pixels.write()


def play_note(freq, duration_ms, pixel_idx, color):
    """Play a PWM pitch on the speaker while illuminating the corresponding pixel."""
    pixels[pixel_idx] = color
    pixels.write()

    pwm = PWM(Pin(PIN_SPEAKER), freq=freq, duty=DUTY)
    time.sleep_ms(duration_ms)
    pwm.deinit()

    pixels[pixel_idx] = (0, 0, 0)
    pixels.write()


def play_pad(pad_idx):
    pad = PADS[pad_idx]
    print(f"Pad {pad_idx} pressed ({pad['freq']} Hz)")
    play_note(pad["freq"], pad["duration_ms"], pad["pixel"], pad["color"])


def on_pad_0(_):
    print("Pad 0: Playing rhythm loop!")
    for pad_idx, gap_ms in LOOP:
        play_pad(pad_idx)
        time.sleep_ms(gap_ms)


def on_pad_1(_):
    play_pad(1)


def on_pad_2(_):
    play_pad(2)


print("Project 4: Pocket DJ")
print("Pad 0: GPIO 21 | Pad 1: GPIO 7 | Pad 2: GPIO 6")
print("Speaker: GPIO 10 | LED Strip: GPIO 20")
print("Press any button to play music!")

clear_pixels()

# Set up debounced buttons (using 80 ms for responsive instrument triggering)
button0 = DebouncedButton(PIN_PAD0, on_pad_0, debounce_ms=80)
button1 = DebouncedButton(PIN_PAD1, on_pad_1, debounce_ms=80)
button2 = DebouncedButton(PIN_PAD2, on_pad_2, debounce_ms=80)

try:
    while True:
        time.sleep_ms(100)
except KeyboardInterrupt:
    clear_pixels()
    print("Pocket DJ stopped.")
