"""
Project 7: Desk Companion

A Wi-Fi desk clock, weather display, and alarm built from an SSD1306 OLED,
a KY-040 rotary encoder, and a small speaker.

The board joins your Wi-Fi network and downloads the current conditions for
your town, setting its clock from the same reply. Turn the knob to move between
screens and press it to select. Your alarm is written to a file on the board, so
it is still there after the microcontroller is unplugged.

Before running this, change WIFI_SSID, WIFI_PASSWORD, and CITY below to match
your own network and town.
"""

import network
import socket
import time
import ujson
from machine import I2C, Pin, PWM, RTC

from debounced_button import DebouncedButton
from rotary_irq import RotaryIRQ
from ssd1306 import SSD1306_I2C

# --- Things you should change ------------------------------------------------

WIFI_SSID = "YWIT-Workshop"
WIFI_PASSWORD = "changeme123"

# Include a state, province, or country when cities share a name. For example:
# "Pittsburgh, PA", "Paris, France", or "Springfield, Illinois".
CITY = "Pittsburgh, PA"

# "fahrenheit" or "celsius"
TEMPERATURE_UNIT = "fahrenheit"

# Used until the first weather reading arrives, because the weather service
# also tells us the time offset for the city above. -4 is US Eastern
# Daylight Time; use -5 in the winter.
FALLBACK_UTC_OFFSET_HOURS = -4

# --- Hardware ---------------------------------------------------------------

PIN_SCL = 7
PIN_SDA = 6
PIN_CLK = 10
PIN_DT = 9
PIN_SW = 8
PIN_SPEAKER = 20

OLED_WIDTH = 128
OLED_HEIGHT = 64
OLED_ADDR = 0x3C

# --- Program settings -------------------------------------------------------

SETTINGS_FILE = "desk_companion.json"
DEFAULT_SETTINGS = {
    "alarm_hour": 7,
    "alarm_minute": 0,
    "alarm_on": False,
    "location": None,
}

WEATHER_HOST = "api.open-meteo.com"
GEOCODING_HOST = "geocoding-api.open-meteo.com"

WEATHER_REFRESH_MS = 15 * 60 * 1000
RETRY_MS = 60 * 1000
DRAW_INTERVAL_MS = 250
SNOOZE_MINUTES = 9
ALARM_TIMEOUT_MS = 60 * 1000

DUTY = 512
ALARM_TONE_HZ = 1760
BEEP_ON_MS = 180
BEEP_OFF_MS = 220
CLICK_TONE = (1200, 12)

WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
MONTHS = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)

# HTTP headers always spell the month this way, whatever language the server is in.
HTTP_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

# The weather service reports conditions as a WMO code. These are the ones that
# actually show up day to day; anything else falls back to the code number.
# The screen fits 16 letters per line, so keep these to 12 or fewer.
WEATHER_CODES = {
    0: "CLEAR",
    1: "MOSTLY CLEAR",
    2: "PART CLOUDY",
    3: "CLOUDY",
    45: "FOG",
    48: "ICY FOG",
    51: "LT DRIZZLE",
    53: "DRIZZLE",
    55: "HVY DRIZZLE",
    61: "LIGHT RAIN",
    63: "RAIN",
    65: "HEAVY RAIN",
    71: "LIGHT SNOW",
    73: "SNOW",
    75: "HEAVY SNOW",
    77: "SNOW GRAINS",
    80: "RAIN SHOWER",
    81: "RAIN SHOWER",
    82: "HVY SHOWERS",
    85: "SNOW SHOWER",
    86: "SNOW SHOWER",
    95: "THUNDER",
    96: "T-STORM HAIL",
    99: "T-STORM HAIL",
}

# Which of the seven segments each digit lights up, as in a real clock radio.
SEGMENTS = {
    "0": "ABCDEF",
    "1": "BC",
    "2": "ABDEG",
    "3": "ABCDG",
    "4": "BCFG",
    "5": "ACDFG",
    "6": "ACDEFG",
    "7": "ABC",
    "8": "ABCDEFG",
    "9": "ABCDFG",
}


# --- Saving settings to the board's filesystem ------------------------------


def load_settings():
    """Read the saved settings file, falling back to the defaults."""
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r") as handle:
            saved = ujson.loads(handle.read())
    except (OSError, ValueError):
        print("No saved settings yet; using defaults.")
        return settings

    # Only copy over keys we know about, so an old or hand-edited file cannot
    # put a surprise value into the program.
    for key in DEFAULT_SETTINGS:
        if key in saved:
            settings[key] = saved[key]
    print("Loaded settings:", settings)
    return settings


def save_settings(settings):
    """Write the settings back to flash so they survive a power cycle."""
    with open(SETTINGS_FILE, "w") as handle:
        handle.write(ujson.dumps(settings))
    print("Saved settings:", settings)


# --- Sound ------------------------------------------------------------------


class Beeper:
    """Makes sound without ever blocking the main loop.

    Project 3 played tones with time.sleep_ms(), which stops everything else
    while the note sounds. An alarm has to keep redrawing the screen and
    watching the knob while it rings, so this class switches the PWM on and off
    from the main loop instead of waiting.
    """

    def __init__(self, pin_num):
        self.pin_num = pin_num
        self.pwm = None
        self.ringing = False
        self.sounding = False
        self.next_change_ms = 0

    def _on(self, freq):
        if self.pwm is None:
            self.pwm = PWM(Pin(self.pin_num), freq=freq, duty=DUTY)
        else:
            self.pwm.freq(freq)
            self.pwm.duty(DUTY)
        self.sounding = True

    def _off(self):
        if self.pwm is not None:
            self.pwm.deinit()
            self.pwm = None
        self.sounding = False

    def click(self, freq=CLICK_TONE[0], duration_ms=CLICK_TONE[1]):
        """A tick short enough that blocking for it is not noticeable."""
        self._on(freq)
        time.sleep_ms(duration_ms)
        self._off()

    def start_ringing(self):
        self.ringing = True
        self.next_change_ms = time.ticks_ms()

    def stop_ringing(self):
        self.ringing = False
        self._off()

    def tick(self, now_ms):
        """Called every time round the main loop to keep the beeps going."""
        if not self.ringing:
            return
        if time.ticks_diff(now_ms, self.next_change_ms) < 0:
            return
        if self.sounding:
            self._off()
            self.next_change_ms = time.ticks_add(now_ms, BEEP_OFF_MS)
        else:
            self._on(ALARM_TONE_HZ)
            self.next_change_ms = time.ticks_add(now_ms, BEEP_ON_MS)


# --- Drawing ----------------------------------------------------------------


def draw_digit(oled, x, y, character, width=17, height=30, thickness=4):
    """Draw one seven-segment digit out of rectangles."""
    half = height // 2
    segments = {
        "A": (x, y, width, thickness),
        "B": (x + width - thickness, y, thickness, half),
        "C": (x + width - thickness, y + half, thickness, half),
        "D": (x, y + height - thickness, width, thickness),
        "E": (x, y + half, thickness, half),
        "F": (x, y, thickness, half),
        "G": (x, y + half - thickness // 2, width, thickness),
    }
    for name in SEGMENTS.get(character, ""):
        oled.fill_rect(*segments[name], 1)


def draw_clock_face(oled, hour, minute, x, y, colon=True):
    """Draw HH:MM in large digits, with the hour's leading zero left off."""
    text = f"{hour:2d}{minute:02d}"
    for index, character in enumerate(text):
        if character == " ":
            continue
        digit_x = x + index * 20 + (10 if index >= 2 else 0)
        draw_digit(oled, digit_x, y, character)
    if colon:
        colon_x = x + 2 * 20 + 2
        oled.fill_rect(colon_x, y + 8, 3, 3, 1)
        oled.fill_rect(colon_x, y + 20, 3, 3, 1)


def draw_bell(oled, x, y):
    """A tiny alarm-armed bell, drawn a few rectangles at a time."""
    oled.fill_rect(x + 2, y, 3, 2, 1)
    oled.fill_rect(x + 1, y + 2, 5, 3, 1)
    oled.fill_rect(x, y + 5, 7, 1, 1)
    oled.fill_rect(x + 3, y + 6, 1, 1, 1)


def centered(oled, text, y):
    """Print text centred on the 128-pixel-wide screen (8 pixels per letter)."""
    x = max(0, (OLED_WIDTH - len(text) * 8) // 2)
    oled.text(text, x, y, 1)


# --- Screens ----------------------------------------------------------------


class Screen:
    """One page of the interface.

    Each screen decides what to draw and what the knob and button do while it
    is showing. The application below keeps a list of these and shows one at a
    time, which is what stops the program from turning into one giant loop of
    if-statements.
    """

    title = "SCREEN"

    def __init__(self, app):
        self.app = app

    @property
    def editing(self):
        """True while the screen wants the knob for itself."""
        return False

    def draw(self, oled):
        raise NotImplementedError

    def turn(self, steps):
        """Knob moved by the given number of clicks while editing."""

    def press(self):
        """Knob button pressed."""


class ClockScreen(Screen):
    """The default screen: big time, date, and a one-line weather summary."""

    title = "CLOCK"

    def draw(self, oled):
        local = self.app.local_time()
        oled.text("ONLINE" if self.app.online else "OFFLINE", 0, 0, 1)
        # The big digits are a 12-hour face, so say which half of the day it is.
        oled.text("AM" if local[3] < 12 else "PM", 64, 0, 1)
        if self.app.settings["alarm_on"]:
            draw_bell(oled, 120, 0)

        display_hour = local[3] % 12
        if display_hour == 0:
            display_hour = 12
        # Blinking the colon once a second is how you can tell the clock is
        # running rather than frozen on a stale frame.
        draw_clock_face(oled, display_hour, local[4], 16, 12, colon=local[5] % 2 == 0)

        date = f"{WEEKDAYS[local[6]]} {MONTHS[local[1] - 1]} {local[2]}"
        centered(oled, date, 46)
        centered(oled, self.app.weather_summary(), 56)

    def press(self):
        self.app.next_screen()


class WeatherScreen(Screen):
    """Current conditions, today's high and low, and how fresh they are."""

    title = "WEATHER"

    def draw(self, oled):
        oled.text(self.app.location_title(), 0, 0, 1)
        oled.hline(0, 10, OLED_WIDTH, 1)

        weather = self.app.weather
        if weather is None:
            centered(oled, "NO DATA YET", 28)
            centered(oled, "CHECK WI-FI", 40)
            return

        unit = "F" if TEMPERATURE_UNIT == "fahrenheit" else "C"
        centered(oled, f"{weather['temperature']:.0f} {unit}", 16)
        centered(oled, weather["description"], 28)
        centered(
            oled,
            f"HI {weather['high']:.0f}  LO {weather['low']:.0f}",
            40,
        )

        age_minutes = time.ticks_diff(time.ticks_ms(), weather["fetched_ms"]) // 60000
        centered(oled, f"{age_minutes} MIN AGO", 54)

    def press(self):
        self.app.next_screen()


class TimeEditScreen(Screen):
    """Shared behaviour for the two screens that edit an hour and a minute.

    Setting the alarm and setting the clock need the same knob dance: press once
    to start on the hour, press again for the minute, and turn to change
    whichever one is underlined. Writing it here once, and letting both screens
    inherit it, is what saves the two of them from being near-copies.
    """

    FIELDS = (None, "hour", "minute")

    def __init__(self, app):
        super().__init__(app)
        self.field_index = 0

    @property
    def editing(self):
        return self.field_index != 0

    @property
    def field(self):
        return self.FIELDS[self.field_index]

    def hour_minute(self):
        """The value being edited. Each screen keeps it somewhere different."""
        raise NotImplementedError

    def set_hour_minute(self, hour, minute):
        raise NotImplementedError

    def start_editing(self):
        """Called on the first press, for screens that preload their fields."""

    def finish(self):
        """Called after the last field, for screens that act on the result."""

    def draw_underline(self, oled):
        """Underline whichever field the knob is about to change."""
        if self.field == "hour":
            oled.hline(16, 48, 37, 1)
        elif self.field == "minute":
            oled.hline(66, 48, 37, 1)

    def turn(self, steps):
        hour, minute = self.hour_minute()
        if self.field == "hour":
            self.set_hour_minute((hour + steps) % 24, minute)
        elif self.field == "minute":
            self.set_hour_minute(hour, (minute + steps) % 60)

    def press(self):
        self.field_index += 1
        if self.field_index >= len(self.FIELDS):
            self.field_index = 0
            self.finish()
            self.app.next_screen()
        elif self.field_index == 1:
            self.start_editing()


class AlarmScreen(TimeEditScreen):
    """Set the alarm time, then arm it."""

    title = "ALARM"

    # One field more than the base class: the alarm can also be switched on.
    FIELDS = (None, "hour", "minute", "armed")

    def hour_minute(self):
        return self.app.settings["alarm_hour"], self.app.settings["alarm_minute"]

    def set_hour_minute(self, hour, minute):
        self.app.settings["alarm_hour"] = hour
        self.app.settings["alarm_minute"] = minute

    def finish(self):
        self.app.save()

    def turn(self, steps):
        if self.field == "armed":
            # Clockwise arms it, counter-clockwise switches it off, so the knob
            # always does the same thing rather than flipping back and forth.
            self.app.settings["alarm_on"] = steps > 0
        else:
            super().turn(steps)

    def draw(self, oled):
        settings = self.app.settings
        oled.text("ALARM", 0, 0, 1)
        oled.hline(0, 10, OLED_WIDTH, 1)
        draw_clock_face(oled, settings["alarm_hour"], settings["alarm_minute"], 16, 16)
        self.draw_underline(oled)

        state = "ARMED" if settings["alarm_on"] else "OFF"
        if self.field == "armed":
            state = ">" + state + "<"
        centered(oled, state, 54)


class SetTimeScreen(TimeEditScreen):
    """Set the clock by hand, for when there is no Wi-Fi to sync with."""

    title = "SET TIME"

    def __init__(self, app):
        super().__init__(app)
        self.hour = 12
        self.minute = 0

    def hour_minute(self):
        return self.hour, self.minute

    def set_hour_minute(self, hour, minute):
        self.hour = hour
        self.minute = minute

    def start_editing(self):
        # Start from the time already showing so small corrections are easy.
        local = self.app.local_time()
        self.set_hour_minute(local[3], local[4])

    def finish(self):
        self.app.set_clock(self.hour, self.minute)

    def draw(self, oled):
        oled.text("SET TIME", 0, 0, 1)
        oled.hline(0, 10, OLED_WIDTH, 1)
        draw_clock_face(oled, self.hour, self.minute, 16, 16)
        self.draw_underline(oled)
        centered(oled, "PRESS TO SET" if self.editing else "24 HOUR CLOCK", 54)


class StatusScreen(Screen):
    """What the board is connected to, for when something is not working."""

    title = "STATUS"

    def draw(self, oled):
        oled.text("STATUS", 0, 0, 1)
        oled.hline(0, 10, OLED_WIDTH, 1)
        oled.text("NET " + ("UP" if self.app.online else "DOWN"), 0, 16, 1)
        oled.text(self.app.ip_address[:16], 0, 26, 1)
        oled.text(f"UP {self.app.uptime_minutes()} MIN", 0, 36, 1)
        oled.text("PRESS FOR CLOCK", 0, 56, 1)

    def press(self):
        self.app.next_screen()


# --- Networking -------------------------------------------------------------


def http_get(host, path, timeout=8):
    """Fetch a page over plain HTTP, returning (status, header bytes, body bytes).

    This is the other half of Project 5. There, the board was the web *server*
    and your phone was the client. Here the board is the client: it opens a
    socket to someone else's server and asks for a page.
    """
    address = socket.getaddrinfo(host, 80)[0][-1]
    sock = socket.socket()
    sock.settimeout(timeout)
    chunks = []
    try:
        sock.connect(address)
        request = f"GET {path} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n"
        sock.send(request.encode())
        while True:
            chunk = sock.recv(512)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()

    raw = b"".join(chunks)
    headers, _, body = raw.partition(b"\r\n\r\n")
    # The first line looks like "HTTP/1.0 200 OK"; the middle word is the code.
    status = int(headers.split(b" ")[1])
    return status, headers, body


def parse_http_date(headers):
    """Read the server's own clock out of an HTTP Date header.

    Every reply carries one, accurate to the second, always in UTC and always in
    the same shape: "Sun, 20 Sep 2026 15:17:37 GMT". Since the board is already
    asking this server for the weather, that is where it gets the time too.
    """
    for line in headers.split(b"\r\n"):
        if line[:5].lower() != b"date:":
            continue
        # ["Sun,", "20", "Sep", "2026", "15:17:37", "GMT"]
        fields = line[5:].strip().decode().split(" ")
        if len(fields) < 5:
            return None
        try:
            day = int(fields[1])
            month = HTTP_MONTHS.index(fields[2]) + 1
            year = int(fields[3])
            hour, minute, second = (int(part) for part in fields[4].split(":"))
        except (ValueError, IndexError):
            return None
        return (year, month, day, hour, minute, second)
    return None


def url_encode(text):
    """Percent-encode text so spaces and punctuation are safe in a URL."""
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    encoded = []
    for byte in text.encode("utf-8"):
        encoded.append(chr(byte) if byte in safe else "%%%02X" % byte)
    return "".join(encoded)


def geocoding_path():
    """Build a city search request for Open-Meteo's geocoding service."""
    return f"/v1/search?name={url_encode(CITY)}&count=1&language=en&format=json"


def weather_path(latitude, longitude):
    """Build the query string the weather service expects."""
    return (
        f"/v1/forecast?latitude={latitude:.4f}&longitude={longitude:.4f}"
        "&current=temperature_2m,weather_code"
        "&daily=temperature_2m_max,temperature_2m_min&forecast_days=1"
        f"&temperature_unit={TEMPERATURE_UNIT}&timezone=auto"
    )


def describe_weather_code(code):
    return WEATHER_CODES.get(code, f"CODE {code}")


# --- The application --------------------------------------------------------


class DeskCompanion:
    """Owns the hardware, the current screen, and the once-per-loop timers."""

    def __init__(self, oled, knob, beeper):
        self.oled = oled
        self.knob = knob
        self.beeper = beeper
        self.rtc = RTC()

        self.settings = load_settings()
        self.screens = [
            ClockScreen(self),
            WeatherScreen(self),
            AlarmScreen(self),
            SetTimeScreen(self),
            StatusScreen(self),
        ]
        self.screen_index = 0

        self.wlan = None
        self.online = False
        self.ip_address = "NO ADDRESS"
        self.utc_offset_seconds = FALLBACK_UTC_OFFSET_HOURS * 3600

        self.location = self.cached_location()
        self.weather = None
        self.started_ms = time.ticks_ms()
        # Deadlines rather than "time since last attempt", so a failed fetch can
        # be retried in a minute while a good one waits the full refresh period.
        self.next_weather_ms = self.started_ms
        self.next_join_ms = self.started_ms

        self.ringing = False
        self.ringing_since_ms = 0
        self.alarm_fired_this_minute = False
        self.snooze_until = None

        self.knob_value = knob.value()
        self.button_pressed = False

    # -- time ---------------------------------------------------------------

    def local_time(self):
        """The current time as a time.localtime() tuple, in local time."""
        return time.localtime(time.time() + self.utc_offset_seconds)

    def set_clock(self, hour, minute):
        """Write a hand-set time into the real-time clock.

        The RTC keeps UTC, so the offset we would add for display gets
        subtracted back out here.
        """
        local = self.local_time()
        utc = time.localtime(
            time.mktime(
                (local[0], local[1], local[2], hour, minute, 0, local[6], local[7])
            )
            - self.utc_offset_seconds
        )
        self.rtc.datetime((utc[0], utc[1], utc[2], utc[6], utc[3], utc[4], utc[5], 0))
        print(f"Clock set by hand to {hour:02d}:{minute:02d} local")

    def uptime_minutes(self):
        return time.ticks_diff(time.ticks_ms(), self.started_ms) // 60000

    # -- screens ------------------------------------------------------------

    @property
    def screen(self):
        return self.screens[self.screen_index]

    def next_screen(self):
        self.screen_index = (self.screen_index + 1) % len(self.screens)

    def save(self):
        save_settings(self.settings)

    def cached_location(self):
        """Return a valid cached match, but only for the current CITY setting."""
        location = self.settings.get("location")
        if not isinstance(location, dict) or location.get("query") != CITY:
            return None
        if not isinstance(location.get("latitude"), (int, float)):
            return None
        if not isinstance(location.get("longitude"), (int, float)):
            return None
        print("Using cached location:", self.location_description(location))
        return location

    def location_description(self, location=None):
        """A readable version of the geocoding result for serial output."""
        location = location or self.location
        if location is None:
            return CITY
        parts = [location["name"]]
        for field in ("admin1", "country"):
            value = location.get(field)
            if value and value not in parts:
                parts.append(value)
        return ", ".join(parts)

    def location_title(self):
        """The matched city name, shortened to the OLED's 16-character line."""
        if self.location is None:
            return "WEATHER"
        # The OLED's built-in font is ASCII-only. Keep accented city names from
        # sending unsupported characters to framebuf.text().
        name = self.location["name"].upper()
        return "".join(
            character if 32 <= ord(character) <= 126 else "?" for character in name
        )[:16]

    def weather_summary(self):
        if self.weather is None:
            return "NO WEATHER"
        unit = "F" if TEMPERATURE_UNIT == "fahrenheit" else "C"
        return f"{self.weather['temperature']:.0f}{unit} {self.weather['description']}"

    def draw(self):
        self.oled.fill(0)
        if self.ringing:
            self.draw_alarm_banner()
        else:
            self.screen.draw(self.oled)
        self.oled.show()

    def draw_alarm_banner(self):
        local = self.local_time()
        # Flash the whole banner so the alarm is obvious from across the room.
        if local[5] % 2 == 0:
            self.oled.fill_rect(0, 0, OLED_WIDTH, 14, 1)
            self.oled.text("ALARM", 40, 3, 0)
        else:
            self.oled.rect(0, 0, OLED_WIDTH, 14, 1)
            self.oled.text("ALARM", 40, 3, 1)
        draw_clock_face(self.oled, local[3] % 12 or 12, local[4], 16, 16)
        centered(self.oled, "PRESS = OFF", 48)
        centered(self.oled, "TURN = SNOOZE", 57)

    # -- input --------------------------------------------------------------

    def on_button(self, _pin):
        """Interrupt handler: record the press and get straight back out."""
        self.button_pressed = True

    def handle_input(self):
        steps = self.knob.value() - self.knob_value
        if steps:
            self.knob_value = self.knob.value()
            if self.ringing:
                self.snooze()
            elif self.screen.editing:
                self.screen.turn(steps)
            else:
                # Not editing anything, so the knob pages through the screens.
                self.screen_index = (self.screen_index + steps) % len(self.screens)
                self.beeper.click()

        if self.button_pressed:
            self.button_pressed = False
            if self.ringing:
                self.dismiss()
            else:
                self.beeper.click()
                self.screen.press()

    # -- alarm --------------------------------------------------------------

    def check_alarm(self, now_ms):
        if self.ringing:
            # Stop by itself eventually, so a board left on a desk does not beep
            # all day at nobody.
            if time.ticks_diff(now_ms, self.ringing_since_ms) > ALARM_TIMEOUT_MS:
                self.dismiss()
            return
        if not self.settings["alarm_on"]:
            return

        local = self.local_time()
        minute_stamp = (local[3], local[4])
        target = self.snooze_until or (
            self.settings["alarm_hour"],
            self.settings["alarm_minute"],
        )

        # The loop runs hundreds of times within the target minute, so the flag
        # is what keeps the alarm from restarting straight after being silenced.
        # Clearing it once the minute has passed re-arms it for tomorrow.
        if minute_stamp != target:
            self.alarm_fired_this_minute = False
        elif not self.alarm_fired_this_minute:
            self.snooze_until = None
            self.start_ringing(minute_stamp)

    def start_ringing(self, minute_stamp):
        self.ringing = True
        self.ringing_since_ms = time.ticks_ms()
        self.alarm_fired_this_minute = True
        self.beeper.start_ringing()
        print(f"Alarm ringing at {minute_stamp[0]:02d}:{minute_stamp[1]:02d}")

    def dismiss(self):
        self.ringing = False
        self.snooze_until = None
        self.beeper.stop_ringing()
        print("Alarm dismissed.")

    def snooze(self):
        local = self.local_time()
        total = local[3] * 60 + local[4] + SNOOZE_MINUTES
        self.snooze_until = ((total // 60) % 24, total % 60)
        self.ringing = False
        self.alarm_fired_this_minute = False
        self.beeper.stop_ringing()
        print(f"Snoozing until {self.snooze_until[0]:02d}:{self.snooze_until[1]:02d}")

    # -- network ------------------------------------------------------------

    def connect_wifi(self):
        self.oled.fill(0)
        centered(self.oled, "JOINING", 20)
        centered(self.oled, WIFI_SSID[:16], 34)
        self.oled.show()

        self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)
        if not self.wlan.isconnected():
            # Re-running the program leaves the radio half-joined from last time,
            # so the same careful reconnect the main loop uses is used here too.
            self.rejoin_wifi()
            for _ in range(100):  # up to ten seconds
                if self.wlan.isconnected():
                    break
                time.sleep_ms(100)

        self.online = self.wlan.isconnected()
        if self.online:
            self.ip_address = self.wlan.ifconfig()[0]
            print("Connected to", WIFI_SSID, "as", self.ip_address)
        else:
            print("Could not join", WIFI_SSID, "- running offline.")
        return self.online

    def rejoin_wifi(self):
        """Ask the radio to join the network again after it has dropped.

        A radio that has been in and out for hours can get into a state where
        connect() refuses with "Wifi Internal State Error" instead of trying.
        Disconnecting first usually avoids that, and turning the interface off and
        on clears it when it happens anyway. An unattended clock must not die
        because the network went away, so nothing in here is allowed to raise.
        """
        try:
            self.wlan.disconnect()
            self.wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        except OSError as error:
            print("Rejoin failed:", error, "- resetting the radio.")
            try:
                self.wlan.active(False)
                time.sleep_ms(200)
                self.wlan.active(True)
                self.wlan.connect(WIFI_SSID, WIFI_PASSWORD)
            except OSError as reset_error:
                print("Radio reset failed:", reset_error, "- retrying in a minute.")

    def set_clock_utc(self, utc_fields):
        """Set the hardware clock from a (year, month, day, hour, min, sec) UTC time."""
        year, month, day, hour, minute, second = utc_fields
        # Round-tripping through mktime fills in the weekday and tidies up any
        # values that need carrying, like second 60.
        utc = time.localtime(time.mktime((year, month, day, hour, minute, second, 0, 0)))
        self.rtc.datetime((utc[0], utc[1], utc[2], utc[6], utc[3], utc[4], utc[5], 0))
        print("Clock set from the weather server; UTC is", time.localtime())

    def resolve_location(self):
        """Turn CITY into coordinates, caching the matched location on flash."""
        if self.location is not None:
            return True
        if not self.online:
            return False

        try:
            status, _, body = http_get(GEOCODING_HOST, geocoding_path())
            if status != 200:
                print("Location service returned", status, body[:80])
                return False

            results = ujson.loads(body).get("results", [])
            if not results:
                print("No location found for:", CITY)
                return False

            match = results[0]
            self.location = {
                "query": CITY,
                "name": match["name"],
                "admin1": match.get("admin1", ""),
                "country": match.get("country", ""),
                "latitude": match["latitude"],
                "longitude": match["longitude"],
            }
            self.settings["location"] = self.location
            self.save()
        except Exception as error:
            print("Location lookup failed:", error)
            return False

        print(
            "Matched location:",
            self.location_description(),
            f"({self.location['latitude']:.4f}, {self.location['longitude']:.4f})",
        )
        return True

    def fetch_weather(self):
        """Download the current conditions and keep the last good reading."""
        if not self.online:
            return False
        if not self.resolve_location():
            return False

        try:
            status, headers, body = http_get(
                WEATHER_HOST,
                weather_path(
                    self.location["latitude"],
                    self.location["longitude"],
                ),
            )
            if status != 200:
                # A free public service is allowed to say no. Printing what it
                # said makes that obvious instead of looking like our bug, and
                # the screen keeps showing the last good reading either way.
                print("Weather service returned", status, body[:80])
                return False
            payload = ujson.loads(body)
            current = payload["current"]
            daily = payload["daily"]
            self.weather = {
                "temperature": current["temperature_2m"],
                "description": describe_weather_code(current["weather_code"]),
                "high": daily["temperature_2m_max"][0],
                "low": daily["temperature_2m_min"][0],
                "fetched_ms": time.ticks_ms(),
            }
            # The service works out the time zone for our coordinates, so the
            # clock gets daylight saving right without us hardcoding it.
            self.utc_offset_seconds = payload["utc_offset_seconds"]

            # The reply also says what time the server thinks it is, so the clock
            # is set from the same fetch rather than from a separate service.
            server_utc = parse_http_date(headers)
            if server_utc is not None:
                self.set_clock_utc(server_utc)
        except Exception as error:
            print("Weather fetch failed:", error)
            return False

        print("Weather:", self.weather)
        return True

    def service_network(self, now_ms):
        """Rejoin the network when it drops and refresh the weather when it is due.

        Everything here is on a deadline so the main loop never sits waiting for
        the network. A board that cannot reach Wi-Fi keeps working as a clock and
        quietly tries again every minute.
        """
        if self.online and not self.wlan.isconnected():
            self.online = False
            print("Wi-Fi dropped.")

        if not self.online:
            if time.ticks_diff(now_ms, self.next_join_ms) >= 0:
                self.next_join_ms = time.ticks_add(now_ms, RETRY_MS)
                self.rejoin_wifi()
            elif self.wlan.isconnected():
                self.online = True
                self.ip_address = self.wlan.ifconfig()[0]
                self.next_weather_ms = now_ms
                print("Wi-Fi back as", self.ip_address)
            return

        if time.ticks_diff(now_ms, self.next_weather_ms) >= 0:
            succeeded = self.fetch_weather()
            self.next_weather_ms = time.ticks_add(
                now_ms, WEATHER_REFRESH_MS if succeeded else RETRY_MS
            )

    # -- main loop ----------------------------------------------------------

    def run(self):
        last_draw_ms = 0
        while True:
            now_ms = time.ticks_ms()

            self.handle_input()
            self.check_alarm(now_ms)
            self.beeper.tick(now_ms)
            self.service_network(now_ms)

            # Everything above is quick, so the screen is redrawn on a timer
            # rather than every pass. Pushing 1 KB to the OLED is the slowest
            # thing this program does.
            if time.ticks_diff(now_ms, last_draw_ms) > DRAW_INTERVAL_MS:
                self.draw()
                last_draw_ms = now_ms

            time.sleep_ms(10)


def main():
    print("--- Starting Project 7: Desk Companion ---")

    i2c = I2C(0, scl=Pin(PIN_SCL), sda=Pin(PIN_SDA), freq=400000)
    devices = i2c.scan()
    print("I2C bus scanned. Found addresses:", [hex(device) for device in devices])
    if OLED_ADDR not in devices:
        print(f"Warning: OLED ({hex(OLED_ADDR)}) not detected on I2C bus!")

    oled = SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, i2c, addr=OLED_ADDR)
    knob = RotaryIRQ(
        pin_num_clk=PIN_CLK,
        pin_num_dt=PIN_DT,
        reverse=True,
        pull_up=True,
        range_mode=RotaryIRQ.RANGE_UNBOUNDED,
    )
    beeper = Beeper(PIN_SPEAKER)

    app = DeskCompanion(oled, knob, beeper)
    DebouncedButton(PIN_SW, app.on_button)

    # The clock and the weather both come from the first fetch, which the main
    # loop already treats as due, so nothing is downloaded here.
    if not app.connect_wifi():
        oled.fill(0)
        centered(oled, "NO WI-FI", 18)
        centered(oled, "SET TIME BY", 34)
        centered(oled, "HAND", 44)
        oled.show()
        time.sleep_ms(2000)

    app.run()


if __name__ == "__main__":
    main()
