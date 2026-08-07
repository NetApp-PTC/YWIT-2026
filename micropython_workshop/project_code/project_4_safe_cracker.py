"""
Safe Cracker — dial a secret combination using a rotary encoder.
Spin to select digits 0-9; audio hints get warmer as you approach the target.
Press the encoder button to lock in each digit.
"""

import random
import time
from machine import Pin, PWM

from debounced_button import DebouncedButton
from rotary_irq import RotaryIRQ

CODE_LENGTH = 3
DIAL_MIN = 0
DIAL_MAX = 9

PIN_CLK = 10
PIN_DT = 9
PIN_SW = 8
PIN_SPEAKER = 20

DUTY = 512

TONE_EXACT = (880, 80)
TONE_WARM = (550, 50)
TONE_COLD = (220, 30)
TONE_BUZZ = (150, 200)
VICTORY_NOTES = [(262, 150), (330, 150), (392, 150), (523, 400)]

secret_code = []
dial_index = 0
knob = None


def play_tone(freq, duration_ms):
    pwm = PWM(Pin(PIN_SPEAKER), freq=freq, duty=DUTY)
    time.sleep_ms(duration_ms)
    pwm.deinit()


def dial_distance(current, target):
    diff = abs(current - target)
    return min(diff, 10 - diff)


def play_hint(distance):
    if distance == 0:
        freq, duration = TONE_EXACT
    elif distance <= 2:
        freq, duration = TONE_WARM
    else:
        freq, duration = TONE_COLD
    play_tone(freq, duration)


def play_buzz():
    freq, duration = TONE_BUZZ
    play_tone(freq, duration)


def play_victory():
    for freq, duration in VICTORY_NOTES:
        play_tone(freq, duration)
        time.sleep_ms(50)


def new_game():
    global secret_code, dial_index
    secret_code = [random.randint(DIAL_MIN, DIAL_MAX) for _ in range(CODE_LENGTH)]
    dial_index = 0
    print("New safe code:", secret_code)
    print_status()


def print_status():
    current = knob.value()
    target = secret_code[dial_index]
    distance = dial_distance(current, target)
    print(
        "Dial {}/{}: showing {} (target {}, distance {})".format(
            dial_index + 1, CODE_LENGTH, current, target, distance
        )
    )


def on_dial_change():
    print_status()
    play_hint(dial_distance(knob.value(), secret_code[dial_index]))


def on_enter(_):
    global dial_index

    current = knob.value()
    target = secret_code[dial_index]

    if current != target:
        print("Wrong! Expected {}, got {}".format(target, current))
        play_buzz()
        print_status()
        return

    print("Correct digit: {}".format(current))
    play_tone(*TONE_EXACT)
    dial_index += 1

    if dial_index >= CODE_LENGTH:
        print("Safe cracked!")
        play_victory()
        new_game()
        return

    print_status()
    play_hint(dial_distance(knob.value(), secret_code[dial_index]))


def setup_components():
    global knob

    knob = RotaryIRQ(
        pin_num_clk=PIN_CLK,
        pin_num_dt=PIN_DT,
        min_val=DIAL_MIN,
        max_val=DIAL_MAX,
        reverse=True,
        pull_up=True,
        range_mode=RotaryIRQ.RANGE_BOUNDED,
    )
    knob.add_listener(on_dial_change)
    DebouncedButton(PIN_SW, on_enter)


setup_components()
new_game()

try:
    while True:
        time.sleep_ms(100)
except KeyboardInterrupt:
    print("Safe Cracker stopped.")
