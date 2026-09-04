"""
This is meant to be a very simple example of using Python
to control a piece of hardware. Here we read a light sensor
(LDR) connected to an analog input pin, then drive an LED
with PWM so its brightness tracks how dark the room is.
Cover the sensor with your hand and the LED should get brighter.
"""

import time
from machine import Pin, ADC, PWM

# 12-bit ADC readings on the ESP32 range from 0 to 4095.
# PWM duty on this board ranges from 0 (off) to 1023 (fully on).
ADC_MAX = 4095
PWM_MAX = 1023

# GPIO 2 (A0) reads the voltage divider made by the LDR and 10k resistor.
# ATTN_11DB lets the pin measure up to about 3.3V.
sensor = ADC(Pin(2))
sensor.atten(ADC.ATTN_11DB)

# GPIO 20 drives the LED. PWM at 1000 Hz is fast enough that the LED
# looks like a steady light whose brightness we can change.
led = PWM(Pin(20), freq=1000)

try:
    while True:
        # Higher readings mean more light on the LDR with the wiring
        # in the project guide. Invert so darkness makes the LED brighter.
        level = sensor.read()
        duty = (ADC_MAX - level) * PWM_MAX // ADC_MAX
        led.duty(duty)

        # Print both numbers so you can see the mapping in the IDE.
        print(level, duty)

        time.sleep(0.05)
except KeyboardInterrupt:
    # Catch ctrl+c from the IDE stop button / REPL and turn the LED off.
    led.duty(0)
    led.deinit()
