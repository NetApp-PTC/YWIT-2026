"""
Project 7: Tilting Ball Maze

A handheld maze game using an MPU-6050 accelerometer and an SSD1306 OLED display.
Tilt the breadboard to roll the ball sprite through the maze to the goal!
"""

import time
from machine import I2C, Pin
from mpu6050 import MPU6050
from ssd1306 import SSD1306_I2C

# Pin assignments for shared I2C bus
PIN_SCL = 7
PIN_SDA = 6

# Display and sensor configuration
OLED_WIDTH = 128
OLED_HEIGHT = 64
OLED_ADDR = 0x3C
MPU_ADDR = 0x68

# Physics tuning constants
INVERT_X = False
INVERT_Y = True
TILT_SCALE = 2.2
DEADZONE = 0.04
FRICTION = 0.90
MAX_SPEED = 4.0
BALL_RADIUS = 2
CALIBRATION_SAMPLES = 100

# Maze start and goal definitions
START_X = 8.0
START_Y = 8.0
GOAL_X = 68
GOAL_Y = 33
GOAL_W = 12
GOAL_H = 12

# Maze walls: list of (x, y, width, height).
# This is a 6-by-4 perfect maze: every area is reachable, but there is only
# one route from the starting cell to the goal cell.
WALLS = (
    # Outer boundaries
    (0, 0, 128, 2),    # Top border
    (0, 62, 128, 2),   # Bottom border
    (0, 0, 2, 64),     # Left border
    (126, 0, 2, 64),   # Right border
    # Internal vertical wall segments
    (21, 0, 2, 18),
    (21, 31, 2, 18),
    (42, 16, 2, 17),
    (42, 31, 2, 18),
    (63, 16, 2, 17),
    (84, 16, 2, 17),
    (84, 31, 2, 18),
    (105, 31, 2, 18),
    (105, 47, 2, 17),
    # Internal horizontal wall segments
    (21, 16, 23, 2),
    (63, 16, 23, 2),
    (84, 16, 23, 2),
    (0, 31, 23, 2),
    (42, 47, 23, 2),
    (63, 47, 23, 2),
)


def hits_wall(bx, by, r, walls):
    """Check if a circle at (bx, by) with radius r intersects any wall rectangle."""
    for wx, wy, ww, wh in walls:
        if (
            bx + r >= wx
            and bx - r <= wx + ww
            and by + r >= wy
            and by - r <= wy + wh
        ):
            return True
    return False


def is_in_goal(bx, by):
    """Check if the ball has reached the goal zone."""
    return (
        bx >= GOAL_X
        and bx <= GOAL_X + GOAL_W
        and by >= GOAL_Y
        and by <= GOAL_Y + GOAL_H
    )


def draw_maze(oled, walls):
    """Render all maze walls on the OLED screen."""
    for wx, wy, ww, wh in walls:
        oled.fill_rect(wx, wy, ww, wh, 1)


def draw_goal(oled):
    """Draw a checkered goal target at the finish."""
    oled.rect(GOAL_X, GOAL_Y, GOAL_W, GOAL_H, 1)
    # Checkered pattern inside goal
    for gx in range(GOAL_X + 2, GOAL_X + GOAL_W - 2, 3):
        for gy in range(GOAL_Y + 2, GOAL_Y + GOAL_H - 2, 3):
            oled.pixel(gx, gy, 1)


def draw_ball(oled, cx, cy, r=BALL_RADIUS):
    """Draw a filled circular ball sprite."""
    x = int(cx)
    y = int(cy)
    oled.fill_rect(x - r, y - 1, 2 * r + 1, 3, 1)
    oled.fill_rect(x - 1, y - r, 3, 2 * r + 1, 1)


def play_victory_screen(oled):
    """Show a victory animation when the maze is completed."""
    oled.fill(0)
    oled.rect(10, 12, 108, 40, 1)
    oled.text("YOU WIN!", 32, 22, 1)
    oled.text("MAZE CLEARED", 18, 36, 1)
    oled.show()
    time.sleep_ms(2000)


def clamp(val, low, high):
    return max(low, min(high, val))


def calibrate_accelerometer(mpu, oled):
    """Measure the sensor's X/Y readings while the board is held level."""
    oled.fill(0)
    oled.text("CALIBRATING", 20, 18, 1)
    oled.text("Hold level...", 16, 36, 1)
    oled.show()

    # Give the sensor and the user's hands a moment to settle.
    time.sleep_ms(500)
    total_x = 0.0
    total_y = 0.0
    for _ in range(CALIBRATION_SAMPLES):
        ax, ay, _ = mpu.read_accel()
        total_x += ax
        total_y += ay
        time.sleep_ms(10)

    return total_x / CALIBRATION_SAMPLES, total_y / CALIBRATION_SAMPLES


def main():
    print("--- Starting Project 7: Tilting Ball Maze ---")

    # Initialize shared I2C bus
    i2c = I2C(0, scl=Pin(PIN_SCL), sda=Pin(PIN_SDA), freq=400000)
    devices = i2c.scan()
    print("I2C bus scanned. Found addresses:", [hex(d) for d in devices])

    if OLED_ADDR not in devices:
        print(f"Warning: OLED ({hex(OLED_ADDR)}) not detected on I2C bus!")
    if MPU_ADDR not in devices:
        print(f"Warning: MPU-6050 ({hex(MPU_ADDR)}) not detected on I2C bus!")

    # Initialize OLED display and MPU-6050
    oled = SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, i2c, addr=OLED_ADDR)
    mpu = MPU6050(i2c, addr=MPU_ADDR)

    # Calibrate the neutral X/Y reading before starting the game.
    ax_offset, ay_offset = calibrate_accelerometer(mpu, oled)
    print("Accelerometer offsets:", ax_offset, ay_offset)

    oled.fill(0)
    oled.text("TILTING MAZE", 18, 18, 1)
    oled.text("Tilt to roll!", 16, 36, 1)
    oled.show()
    time.sleep_ms(750)

    # Ball physics state
    ball_x = START_X
    ball_y = START_Y
    vx = 0.0
    vy = 0.0

    while True:
        # 1. Read tilt from accelerometer
        try:
            ax, ay, _ = mpu.read_accel()
        except Exception as e:
            print("Error reading accelerometer:", e)
            time.sleep_ms(50)
            continue

        ax -= ax_offset
        ay -= ay_offset

        if INVERT_X:
            ax = -ax
        if INVERT_Y:
            ay = -ay

        # 2. Apply deadzone so ball stays still on flat table
        if abs(ax) < DEADZONE:
            ax = 0.0
        if abs(ay) < DEADZONE:
            ay = 0.0

        # 3. Integrate acceleration into velocity with friction
        vx = clamp((vx + ax * TILT_SCALE) * FRICTION, -MAX_SPEED, MAX_SPEED)
        vy = clamp((vy + ay * TILT_SCALE) * FRICTION, -MAX_SPEED, MAX_SPEED)

        # 4. Move X and resolve wall collisions
        new_x = ball_x + vx
        if not hits_wall(new_x, ball_y, BALL_RADIUS, WALLS):
            ball_x = new_x
        else:
            vx = 0.0

        # 5. Move Y and resolve wall collisions
        new_y = ball_y + vy
        if not hits_wall(ball_x, new_y, BALL_RADIUS, WALLS):
            ball_y = new_y
        else:
            vy = 0.0

        # 6. Check for goal condition
        if is_in_goal(ball_x, ball_y):
            play_victory_screen(oled)
            ball_x = START_X
            ball_y = START_Y
            vx = 0.0
            vy = 0.0

        # 7. Render frame
        oled.fill(0)
        draw_goal(oled)
        draw_maze(oled, WALLS)
        draw_ball(oled, ball_x, ball_y)
        oled.show()

        time.sleep_ms(20)


if __name__ == "__main__":
    main()
