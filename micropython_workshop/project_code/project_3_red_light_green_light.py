"""
Project 3: Red Light, Green Light

Single-player reflex game:
- Press during GREEN to score and record reaction time.
- Press during RED and the round ends immediately.
"""

import random
import time
from machine import Pin
import neopixel
from debounced_button import DebouncedButton

PIN_PIXELS = 20
NUM_PIXELS = 3
PIN_BUTTON = 21
LED_RED = 0
LED_YELLOW = 1
LED_GREEN = 2

TARGET_GREEN_HITS = 5
GREEN_MIN_MS = 700
GREEN_MAX_MS = 1700
RED_MIN_MS = 900
RED_MAX_MS = 2200
YELLOW_MIN_MS = 500
YELLOW_MAX_MS = 1400

LOOP_DELAY_MS = 10

COLOR_OFF = (0, 0, 0)
COLOR_IDLE = (0, 0, 12)
COLOR_GREEN = (0, 18, 0)
COLOR_RED = (20, 0, 0)
COLOR_YELLOW = (16, 10, 0)

pixels = neopixel.NeoPixel(Pin(PIN_PIXELS, Pin.OUT), NUM_PIXELS)
button = None

state = "idle"
score = 0
windows_done = 0
best_reaction_ms = None
phase_deadline_ms = 0
green_started_ms = 0

def set_all(color):
    for i in range(NUM_PIXELS):
        pixels[i] = color
    pixels.write()


def show_idle():
    set_all(COLOR_OFF)
    pixels[LED_YELLOW] = COLOR_IDLE
    pixels.write()


def flash(color, count, on_ms=120, off_ms=80):
    for _ in range(count):
        set_all(color)
        time.sleep_ms(on_ms)
        set_all(COLOR_OFF)
        time.sleep_ms(off_ms)


def show_stoplight(red_on=False, yellow_on=False, green_on=False):
    set_all(COLOR_OFF)
    if red_on:
        pixels[LED_RED] = COLOR_RED
    if yellow_on:
        pixels[LED_YELLOW] = COLOR_YELLOW
    if green_on:
        pixels[LED_GREEN] = COLOR_GREEN
    pixels.write()


def enter_green(now_ms):
    global state, phase_deadline_ms, green_started_ms
    state = "green"
    green_started_ms = now_ms
    phase_deadline_ms = time.ticks_add(
        now_ms, random.randint(GREEN_MIN_MS, GREEN_MAX_MS)
    )
    show_stoplight(green_on=True)
    print("GREEN! Press now.")


def enter_yellow(now_ms):
    global state, phase_deadline_ms
    state = "yellow"
    phase_deadline_ms = time.ticks_add(
        now_ms, random.randint(YELLOW_MIN_MS, YELLOW_MAX_MS)
    )
    show_stoplight(yellow_on=True)
    print("YELLOW! Get ready...")


def enter_red(now_ms):
    global state, phase_deadline_ms
    state = "red"
    phase_deadline_ms = time.ticks_add(now_ms, random.randint(RED_MIN_MS, RED_MAX_MS))
    show_stoplight(red_on=True)
    print("RED! Do not press.")


def start_round(now_ms):
    global state, score, windows_done, best_reaction_ms
    score = 0
    windows_done = 0
    best_reaction_ms = None
    state = "starting"
    print(f"\nNew round started. Complete {TARGET_GREEN_HITS} reaction windows.")
    enter_red(now_ms)


def end_round(message):
    global state
    state = "round_over"
    if score == TARGET_GREEN_HITS:
        flash((0, 20, 0), 3, on_ms=160, off_ms=70)
    else:
        flash((20, 0, 0), 3, on_ms=160, off_ms=70)

    print(message)
    print(f"Final score: {score}/{TARGET_GREEN_HITS}")
    print(f"Misses: {TARGET_GREEN_HITS - score}")
    if best_reaction_ms is None:
        print("Best reaction: n/a")
    else:
        print(f"Best reaction: {best_reaction_ms} ms")
    print("Press button to start a new round.")

    show_stoplight(yellow_on=True)


def handle_press(now_ms):
    global score, windows_done, best_reaction_ms

    if state == "idle" or state == "round_over":
        start_round(now_ms)
        return

    if state == "green":
        reaction_ms = time.ticks_diff(now_ms, green_started_ms)
        score += 1
        windows_done += 1
        if best_reaction_ms is None or reaction_ms < best_reaction_ms:
            best_reaction_ms = reaction_ms
        print(f"Nice! Reaction: {reaction_ms} ms | Score: {score}/{TARGET_GREEN_HITS}")
        flash((0, 25, 0), 1, on_ms=80, off_ms=40)
        if windows_done >= TARGET_GREEN_HITS:
            if score == TARGET_GREEN_HITS:
                end_round("You win! Perfect round.")
            else:
                end_round("Round complete.")
            return
        enter_red(time.ticks_ms())
        return

    if state == "red" or state == "yellow":
        end_round("Too early! You pressed before GREEN.")


def on_button_press(_):
    handle_press(time.ticks_ms())


def tick_state(now_ms):
    global windows_done

    if state == "red" and time.ticks_diff(now_ms, phase_deadline_ms) >= 0:
        enter_yellow(now_ms)
    elif state == "yellow" and time.ticks_diff(now_ms, phase_deadline_ms) >= 0:
        enter_green(now_ms)
    elif state == "green" and time.ticks_diff(now_ms, phase_deadline_ms) >= 0:
        windows_done += 1
        print(f"Missed GREEN window. Progress: {windows_done}/{TARGET_GREEN_HITS}")
        if windows_done >= TARGET_GREEN_HITS:
            if score == TARGET_GREEN_HITS:
                end_round("You win! Perfect round.")
            else:
                end_round("Round complete.")
            return
        enter_red(now_ms)


print("Project 3: Red Light, Green Light")
print("Button on GPIO21 (active-low with pull-up), LEDs on GPIO20")
print("Press button to start.")
show_idle()
button = DebouncedButton(PIN_BUTTON, on_button_press)

try:
    while True:
        now = time.ticks_ms()
        tick_state(now)
        time.sleep_ms(LOOP_DELAY_MS)
except KeyboardInterrupt:
    set_all(COLOR_OFF)
    print("Game stopped.")
