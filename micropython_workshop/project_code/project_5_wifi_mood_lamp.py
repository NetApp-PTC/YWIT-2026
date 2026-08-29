"""
Project 5: Wi-Fi Mood Lamp

ESP32-C3 hosts a password-protected AP and a local web page to control
3 chained WS2812B LEDs on GPIO20.
"""

import machine
import network
import neopixel
import socket
import time
import errno
import ubinascii
import ujson

LED_PIN = 20
LED_COUNT = 3

AP_PASSWORD = "moodlamp123"
AP_CHANNEL = 6

HTTP_PORT = 80
REQUEST_BUFFER_SIZE = 4096
SOCKET_TIMEOUT_SEC = 0.05
MAIN_LOOP_DELAY_MS = 10
CONTROL_PAGE_FILE = "resources/project_5_wifi_mood_lamp.html"

AVAILABLE_ANIMATIONS = ["breathing", "rainbow", "wipe", "blink"]
NONBLOCKING_SOCKET_ERRNOS = (errno.ETIMEDOUT, errno.EAGAIN, 11, 110)

pixels = neopixel.NeoPixel(machine.Pin(LED_PIN, machine.Pin.OUT), LED_COUNT)

state = {
    "mode": "global",
    "global": {"r": 80, "g": 20, "b": 120, "brightness": 40},
    "leds": [
        {"r": 80, "g": 20, "b": 120, "brightness": 40},
        {"r": 80, "g": 20, "b": 120, "brightness": 40},
        {"r": 80, "g": 20, "b": 120, "brightness": 40},
    ],
    "animation": {"running": False, "name": "", "speed": 50, "step": 0, "last_ms": 0},
}

ap_ssid = ""
ap_ip = ""
control_page_html = ""


def clamp_int(value, minimum, maximum, field_name):
    if not isinstance(value, int):
        raise ValueError("{} must be an integer".format(field_name))
    if value < minimum or value > maximum:
        raise ValueError("{} must be in [{}..{}]".format(field_name, minimum, maximum))
    return value


def parse_rgb_brightness(payload):
    return (
        clamp_int(payload.get("r"), 0, 255, "r"),
        clamp_int(payload.get("g"), 0, 255, "g"),
        clamp_int(payload.get("b"), 0, 255, "b"),
        clamp_int(payload.get("brightness"), 0, 100, "brightness"),
    )


def brightness_scale(color, brightness):
    return (
        (color[0] * brightness) // 100,
        (color[1] * brightness) // 100,
        (color[2] * brightness) // 100,
    )


def clear_pixels():
    for i in range(LED_COUNT):
        pixels[i] = (0, 0, 0)
    pixels.write()


def render_manual():
    if state["mode"] == "global":
        global_led = state["global"]
        color = (global_led["r"], global_led["g"], global_led["b"])
        scaled = brightness_scale(color, global_led["brightness"])
        for i in range(LED_COUNT):
            pixels[i] = scaled
    else:
        for i in range(LED_COUNT):
            led = state["leds"][i]
            color = (led["r"], led["g"], led["b"])
            pixels[i] = brightness_scale(color, led["brightness"])
    pixels.write()


def unique_ssid():
    uid_hex = ubinascii.hexlify(machine.unique_id()).decode().upper()
    return "MoodLamp-{}".format(uid_hex[-6:])


def start_access_point():
    global ap_ssid, ap_ip
    ap_ssid = unique_ssid()

    ap = network.WLAN(network.AP_IF)
    ap.active(False)
    ap.active(True)
    ap.config(
        essid=ap_ssid,
        password=AP_PASSWORD,
        authmode=network.AUTH_WPA_WPA2_PSK,
        channel=AP_CHANNEL,
    )

    for _ in range(50):
        if ap.active():
            break
        time.sleep_ms(100)
    if not ap.active():
        raise RuntimeError("Failed to start access point")

    ap_ip = ap.ifconfig()[0]
    print("AP ready")
    print("SSID:", ap_ssid)
    print("Password:", AP_PASSWORD)
    print("Open: http://{}/".format(ap_ip))


def wheel(pos):
    p = 255 - (pos % 256)
    if p < 85:
        return (255 - p * 3, 0, p * 3)
    if p < 170:
        p -= 85
        return (0, p * 3, 255 - p * 3)
    p -= 170
    return (p * 3, 255 - p * 3, 0)


def speed_to_interval_ms(speed, slow_ms, fast_ms):
    # speed=1 -> slow_ms, speed=100 -> fast_ms
    return slow_ms - ((speed - 1) * (slow_ms - fast_ms)) // 99


def set_animation(name, speed):
    if name not in AVAILABLE_ANIMATIONS:
        raise ValueError("animation must be one of {}".format(AVAILABLE_ANIMATIONS))
    state["animation"]["running"] = True
    state["animation"]["name"] = name
    state["animation"]["speed"] = clamp_int(speed, 1, 100, "speed")
    state["animation"]["step"] = 0
    state["animation"]["last_ms"] = time.ticks_ms()


def stop_animation():
    state["animation"]["running"] = False
    state["animation"]["name"] = ""
    state["animation"]["step"] = 0
    render_manual()


def update_animation(now_ms):
    animation = state["animation"]
    if not animation["running"]:
        return

    name = animation["name"]
    speed = animation["speed"]

    if name == "breathing":
        interval = speed_to_interval_ms(speed, 55, 12)
    elif name == "rainbow":
        interval = speed_to_interval_ms(speed, 80, 12)
    elif name == "wipe":
        interval = speed_to_interval_ms(speed, 200, 30)
    else:
        interval = speed_to_interval_ms(speed, 600, 80)

    if time.ticks_diff(now_ms, animation["last_ms"]) < interval:
        return

    animation["last_ms"] = now_ms
    animation["step"] += 1
    step = animation["step"]

    if name == "breathing":
        base = state["global"]
        tri = step % 512
        if tri > 255:
            tri = 511 - tri
        effective = max(1, (base["brightness"] * tri) // 255)
        color = brightness_scale((base["r"], base["g"], base["b"]), effective)
        for i in range(LED_COUNT):
            pixels[i] = color
        pixels.write()
        return

    if name == "rainbow":
        for i in range(LED_COUNT):
            raw = wheel(step * 5 + (i * 256 // LED_COUNT))
            pixels[i] = brightness_scale(raw, state["global"]["brightness"])
        pixels.write()
        return

    if name == "wipe":
        active = step % LED_COUNT
        clear_pixels()
        base = state["global"]
        pixels[active] = brightness_scale(
            (base["r"], base["g"], base["b"]),
            base["brightness"],
        )
        pixels.write()
        return

    if name == "blink":
        on = (step % 2) == 0
        base = state["global"]
        if on:
            c = brightness_scale((base["r"], base["g"], base["b"]), base["brightness"])
        else:
            c = (0, 0, 0)
        for i in range(LED_COUNT):
            pixels[i] = c
        pixels.write()
        return


def json_response(conn, status, payload):
    body = ujson.dumps(payload)
    conn.send("HTTP/1.1 {}\r\n".format(status))
    conn.send("Content-Type: application/json\r\n")
    conn.send("Cache-Control: no-store\r\n")
    conn.send("Connection: close\r\n")
    conn.send("Content-Length: {}\r\n\r\n".format(len(body)))
    conn.send(body)


def html_response(conn, html):
    conn.send("HTTP/1.1 200 OK\r\n")
    conn.send("Content-Type: text/html; charset=utf-8\r\n")
    conn.send("Cache-Control: no-store\r\n")
    conn.send("Connection: close\r\n")
    conn.send("Content-Length: {}\r\n\r\n".format(len(html)))
    conn.send(html)


def load_control_page():
    global control_page_html
    try:
        with open(CONTROL_PAGE_FILE, "r") as handle:
            control_page_html = handle.read()
    except OSError:
        raise RuntimeError("Missing dashboard file: {}".format(CONTROL_PAGE_FILE))


def state_payload():
    return {
        "ok": True,
        "ssid": ap_ssid,
        "ip": ap_ip,
        "mode": state["mode"],
        "global": state["global"],
        "leds": state["leds"],
        "animation": state["animation"],
        "availableAnimations": AVAILABLE_ANIMATIONS,
    }


def parse_http_request(raw):
    split_idx = raw.find(b"\r\n\r\n")
    if split_idx == -1:
        raise ValueError("invalid HTTP request framing")

    head = raw[:split_idx].decode()
    body = raw[split_idx + 4 :]
    lines = head.split("\r\n")
    first = lines[0].split(" ")
    if len(first) < 2:
        raise ValueError("invalid request line")
    method = first[0].upper()
    path = first[1].split("?")[0]

    return method, path, body


def parse_json_body(body_bytes):
    if not body_bytes:
        raise ValueError("request body required")
    try:
        payload = ujson.loads(body_bytes.decode())
    except ValueError:
        raise ValueError("body must be valid JSON")
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def apply_set_global(payload):
    r, g, b, brightness = parse_rgb_brightness(payload)
    state["global"]["r"] = r
    state["global"]["g"] = g
    state["global"]["b"] = b
    state["global"]["brightness"] = brightness
    state["mode"] = "global"
    stop_animation()
    render_manual()


def apply_set_led(payload):
    index = clamp_int(payload.get("index"), 0, LED_COUNT - 1, "index")
    r, g, b, brightness = parse_rgb_brightness(payload)
    state["leds"][index]["r"] = r
    state["leds"][index]["g"] = g
    state["leds"][index]["b"] = b
    state["leds"][index]["brightness"] = brightness
    state["mode"] = "per_led"
    stop_animation()
    render_manual()


def apply_set_mode(payload):
    mode = payload.get("mode")
    if mode not in ("global", "per_led"):
        raise ValueError("mode must be global or per_led")
    state["mode"] = mode
    stop_animation()
    render_manual()


def apply_set_animation(payload):
    name = payload.get("name")
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    speed = clamp_int(payload.get("speed"), 1, 100, "speed")
    set_animation(name, speed)


def route_request(method, path, body, conn):
    if method == "GET" and path == "/":
        html_response(conn, control_page_html)
        return

    if method == "GET" and path == "/state":
        json_response(conn, "200 OK", state_payload())
        return

    if method == "POST" and path == "/set/global":
        payload = parse_json_body(body)
        apply_set_global(payload)
        json_response(conn, "200 OK", state_payload())
        return

    if method == "POST" and path == "/set/led":
        payload = parse_json_body(body)
        apply_set_led(payload)
        json_response(conn, "200 OK", state_payload())
        return

    if method == "POST" and path == "/set/mode":
        payload = parse_json_body(body)
        apply_set_mode(payload)
        json_response(conn, "200 OK", state_payload())
        return

    if method == "POST" and path == "/set/animation":
        payload = parse_json_body(body)
        apply_set_animation(payload)
        json_response(conn, "200 OK", state_payload())
        return

    if method == "POST" and path == "/set/stop":
        parse_json_body(body)
        stop_animation()
        json_response(conn, "200 OK", state_payload())
        return

    json_response(conn, "404 Not Found", {"ok": False, "error": "route not found"})


def handle_client(conn):
    try:
        raw = conn.recv(REQUEST_BUFFER_SIZE)
        if not raw:
            raise ValueError("empty request")
        method, path, body = parse_http_request(raw)
        route_request(method, path, body, conn)
    except ValueError as e:
        json_response(conn, "400 Bad Request", {"ok": False, "error": str(e)})
    finally:
        conn.close()


def run_server():
    load_control_page()
    start_access_point()
    render_manual()

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", HTTP_PORT))
    server.listen(2)
    server.settimeout(SOCKET_TIMEOUT_SEC)

    print("HTTP server listening on port", HTTP_PORT)

    try:
        while True:
            update_animation(time.ticks_ms())
            try:
                conn, _ = server.accept()
                conn.settimeout(1)
                handle_client(conn)
            except OSError as e:
                code = e.args[0] if e.args else None
                if code not in NONBLOCKING_SOCKET_ERRNOS:
                    raise
            time.sleep_ms(MAIN_LOOP_DELAY_MS)
    finally:
        server.close()
        clear_pixels()


print("Project 5: Wi-Fi Mood Lamp")
print("LEDs on GPIO20 (3 chained WS2812B)")
print("Starting...")
run_server()
