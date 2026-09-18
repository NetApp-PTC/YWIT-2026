"""
This class is meant to be used with a button where
it is desirable to prevent the physical bouncing from
calling the IRQ callback multiple times in a short period
of time.
"""

from machine import Pin
import utime


class DebouncedButton:
    def __init__(self, pin_num, action, debounce_ms=300):
        self.pin = Pin(pin_num, mode=Pin.IN, pull=Pin.PULL_UP)
        self.action = action
        self.debounce_ms = debounce_ms
        self.pin.irq(handler=self._action, trigger=Pin.IRQ_FALLING)
        self.last_triggered = utime.ticks_ms()

    def _action(self, pin):
        if utime.ticks_diff(utime.ticks_ms(), self.last_triggered) < self.debounce_ms:
            return
        self.action(pin)
        self.last_triggered = utime.ticks_ms()
