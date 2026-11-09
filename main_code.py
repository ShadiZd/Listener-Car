
try:
    import json
except ImportError:  # pragma: no cover - MicroPython fallback
    import ujson as json

from pybricks.hubs import PrimeHub
from pybricks.iodevices import XboxController
from pybricks.parameters import Button, Color, Direction, Port
from pybricks.pupdevices import Motor, UltrasonicSensor
from pybricks.tools import StopWatch, read_input_byte, wait


# ==================================================
# 1. INITIALIZATION
# ==================================================

hub = PrimeHub()

left_motor = Motor(Port.A)
right_motor = Motor(Port.E, Direction.COUNTERCLOCKWISE)
steering_motor = Motor(Port.B)

back_sensor = UltrasonicSensor(Port.C)
front_sensor = UltrasonicSensor(Port.F)

# Xbox manual control is optional. Set this to True only when a controller is
# paired AND already connected: XboxController() BLOCKS until it finds one, so
# leaving it on would freeze startup and the car would never answer the laptop.
USE_CONTROLLER = False

if USE_CONTROLLER:
    try:
        controller = XboxController()
    except Exception:
        controller = None
else:
    controller = None  # voice-only use

steering_motor.reset_angle(0)


# ==================================================
# 2. CONFIGURATION
# ==================================================

AVOID_DISTANCE = 100
RELEASE_DISTANCE = 130
AVOID_POWER = 50
STICK_DEADZONE = 8

# Obstacle warning (ultrasonic distances are in mm).
# The beep is deliberately low, soft and slow so it warns without being noisy.
OBSTACLE_WARN_MM = 400        # start warning inside 40 cm
OBSTACLE_CLEAR_MM = 500       # stop warning once past 50 cm (hysteresis)
OBSTACLE_BEEP_FREQ = 440      # one octave below the old 880 Hz
OBSTACLE_BEEP_MS = 60
OBSTACLE_BEEP_VOLUME = 35

# Steering motor (port B): the wheels point straight ahead at this angle.
# Left/right commands are offsets from this center, e.g. center -10 plus a
# 45 degree right turn targets +35.
STEERING_CENTER = -10

ENGINE_MIN_FREQ = 64
ENGINE_RUMBLE = 1.25
ENGINE_MIN_VOL = 15
ENGINE_DEADZONE = 5

FORWARD_POWER = -AVOID_POWER
REVERSE_POWER = AVOID_POWER

# Autonomous mode state
current_mode = "autonomous" if controller is None else "manual"

# Autonomous driving state
drive_power = 0
steering_angle = STEERING_CENTER

# Autonomous action timer
action_timer = StopWatch()
action_duration = 0
action_active = False

# Laptop link (JSON lines over the Pybricks stdin/stdout channel)
WATCHDOG_MS = 2000          # stop the car if the laptop goes silent this long
LINE_MAX_BYTES = 512
DEFAULT_DURATION_MS = 8000
MAX_DURATION_MS = 15000     # hard time limit: 15 seconds

# Speed scale used by the AI plan: 800 = normal, 1000 = full speed.
# The motors take a percentage, so speed is divided by this factor.
SPEED_MAX = 1000
SPEED_NORMAL = 800
SPEED_POWER_DIVISOR = 10

input_line = ""
last_input = StopWatch()
active_command_id = None


# ==================================================
# 3. SENSOR STATE
# ==================================================

front_distance = 2000
back_distance = 2000

front_close = False
back_close = False

read_front_next = True

beep_timer = StopWatch()
last_beep_time = 0
obstacle_warned = False
obstacle_warn_side = ""


# ==================================================
# 4. ENGINE STATE
# ==================================================

engine_on = False
rumble_flip = False
a_was_pressed = False


# ==================================================
# 5. BASIC ROBOT FUNCTIONS
# ==================================================

def speed_to_power(speed):
    """Convert the AI speed scale (1-1000) to a motor power percent (1-100)."""
    try:
        value = abs(int(speed))
    except (TypeError, ValueError):
        value = SPEED_NORMAL
    value = max(1, min(SPEED_MAX, value))
    return max(1, min(100, value // SPEED_POWER_DIVISOR))


def drive_forward(speed=SPEED_NORMAL):
    global drive_power
    drive_power = -speed_to_power(speed)


def drive_reverse(speed=SPEED_NORMAL):
    global drive_power
    drive_power = speed_to_power(speed)


def stop_car():
    global drive_power
    drive_power = 0


def steer_left(angle=45):
    global steering_angle
    steering_angle = STEERING_CENTER - abs(angle)


def steer_right(angle=45):
    global steering_angle
    steering_angle = STEERING_CENTER + abs(angle)


def center_steering():
    global steering_angle
    steering_angle = STEERING_CENTER


def honk():
    hub.speaker.beep(392, 100)
    hub.speaker.beep(330, 100)


def hold_car():
    global drive_power
    drive_power = 0
    left_motor.hold()
    right_motor.hold()


# ==================================================
# 6. AUTONOMOUS ACTION FUNCTIONS
# ==================================================

def execute_action(action, parameters=None):
    global action_duration, action_active

    if parameters is None:
        parameters = {}

    if action == "forward":
        drive_forward(parameters.get("speed", SPEED_NORMAL))

    elif action == "reverse":
        drive_reverse(parameters.get("speed", SPEED_NORMAL))

    elif action == "left":
        steer_left(parameters.get("angle", 45))

    elif action == "right":
        steer_right(parameters.get("angle", 45))

    elif action == "straight":
        center_steering()

    elif action == "stop":
        stop_car()
        center_steering()

    elif action == "horn":
        honk()

    elif action == "drive":
        drive_power = parameters.get("power", 0)
        globals()["drive_power"] = max(-100, min(100, drive_power))

        steering = parameters.get("steering", 0)
        globals()["steering_angle"] = max(-100, min(100,
                                                       STEERING_CENTER + steering))

    else:
        return False

    duration = parameters.get("duration_ms", 0)

    if duration > 0:
        action_duration = duration
        action_timer.reset()
        action_active = True
    else:
        action_active = False

    return True


def update_action():
    global action_active, active_command_id

    if action_active and action_timer.time() >= action_duration:
        stop_car()
        center_steering()
        action_active = False
        if active_command_id is not None:
            send({"id": active_command_id, "type": "done"})
            active_command_id = None


# ==================================================
# 7. LAPTOP LINK (JSON lines over stdin/stdout)
# ==================================================

def send(payload):
    print(json.dumps(payload))


def clamp_int(value, low, high, default):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def check_watchdog():
    global action_active, active_command_id

    if action_active and last_input.time() > WATCHDOG_MS:
        stop_car()
        center_steering()
        action_active = False
        if active_command_id is not None:
            send({"id": active_command_id, "type": "error",
                  "error": "watchdog timeout (host silent)"})
            active_command_id = None


def handle_command(msg):
    global active_command_id, action_active

    cid = msg.get("id")
    action = msg.get("action")

    if cid is None or action is None:
        send({"type": "error", "error": "command needs id and action"})
        return

    if current_mode != "autonomous":
        send({"id": cid, "type": "error", "error": "car is in manual mode"})
        return

    # A stop command always wins: cancel whatever is running.
    if action == "stop" and action_active:
        stop_car()
        center_steering()
        action_active = False
        if active_command_id is not None:
            send({"id": active_command_id, "type": "error",
                  "error": "interrupted by stop"})
            active_command_id = None

    if action_active:
        send({"id": cid, "type": "error", "error": "busy with another command"})
        return

    raw_params = msg.get("parameters")
    if not isinstance(raw_params, dict):
        raw_params = {}

    if action in ("forward", "reverse"):
        parameters = {
            "speed": clamp_int(raw_params.get("speed", SPEED_NORMAL),
                               1, SPEED_MAX, SPEED_NORMAL),
            "duration_ms": clamp_int(raw_params.get("duration_ms",
                                                    DEFAULT_DURATION_MS),
                                     100, MAX_DURATION_MS,
                                     DEFAULT_DURATION_MS),
        }
    elif action in ("left", "right"):
        angle = abs(clamp_int(raw_params.get("angle", 45), -45, 45, 45))
        angle = max(1, min(45, angle))
        parameters = {"angle": -angle if action == "left" else angle}
    elif action in ("straight", "stop", "horn"):
        parameters = {}
    else:
        send({"id": cid, "type": "error",
              "error": "unknown action " + str(action)})
        return

    if not execute_action(action, parameters):
        send({"id": cid, "type": "error", "error": "action failed"})
        return

    active_command_id = cid
    send({"id": cid, "type": "ack"})

    if action in ("forward", "reverse") or parameters.get("duration_ms", 0) > 0:
        return  # update_action() sends "done" when the timer expires

    send({"id": cid, "type": "done"})
    active_command_id = None


def handle_line(text):
    # `text` is already a str: the main loop builds it with chr() because
    # Pybricks MicroPython's bytes object has no .decode() method.
    last_input.reset()

    if text == "ping":
        return

    try:
        msg = json.loads(text)
    except ValueError:
        send({"type": "error", "error": "bad JSON line"})
        return

    if not isinstance(msg, dict):
        send({"type": "error", "error": "message must be a JSON object"})
        return

    kind = msg.get("type")

    if kind == "ping":
        return

    if kind == "mode":
        mode = msg.get("mode")
        if mode in ("manual", "autonomous"):
            set_mode(mode)
            send({"type": "mode", "mode": current_mode})
        else:
            send({"type": "error", "error": "mode must be manual or autonomous"})
        return

    if "action" in msg:
        handle_command(msg)
    else:
        send({"type": "error", "error": "unknown message"})


# ==================================================
# 8. MODE MANAGEMENT
# ==================================================

def set_mode(mode):
    global current_mode, action_active, active_command_id

    if mode not in ("manual", "autonomous"):
        return

    current_mode = mode

    stop_car()
    center_steering()

    if action_active:
        action_active = False
        if active_command_id is not None:
            send({"id": active_command_id, "type": "error",
                  "error": "mode changed"})
            active_command_id = None


def toggle_mode():
    if current_mode == "manual":
        set_mode("autonomous")
    else:
        set_mode("manual")


# ==================================================
# 9. SENSOR FUNCTIONS
# ==================================================

def update_sensors():
    global front_distance, back_distance
    global front_close, back_close
    global read_front_next

    if read_front_next:
        front_distance = front_sensor.distance()
    else:
        back_distance = back_sensor.distance()

    read_front_next = not read_front_next

    if front_distance <= AVOID_DISTANCE:
        front_close = True
    elif front_distance > RELEASE_DISTANCE:
        front_close = False

    if back_distance <= AVOID_DISTANCE:
        back_close = True
    elif back_distance > RELEASE_DISTANCE:
        back_close = False


def obstacle_avoidance():
    global last_beep_time, obstacle_warned, obstacle_warn_side

    nearest_distance = min(front_distance, back_distance)

    if nearest_distance < OBSTACLE_WARN_MM:

        side = "front" if front_distance <= back_distance else "back"

        # Tell the laptop when the warning starts (or changes side) so it can
        # speak "Obstacle ahead." / "Obstacle behind.".
        if not obstacle_warned or side != obstacle_warn_side:
            obstacle_warned = True
            obstacle_warn_side = side
            send({"type": "alert", "kind": "obstacle", "side": side,
                  "distance": int(nearest_distance)})

        if nearest_distance < 150:
            interval = 700
        elif nearest_distance < 250:
            interval = 1100
        else:
            interval = 1600

        if beep_timer.time() - last_beep_time >= interval:
            hub.speaker.volume(OBSTACLE_BEEP_VOLUME)
            hub.speaker.beep(OBSTACLE_BEEP_FREQ, OBSTACLE_BEEP_MS)
            hub.speaker.volume(100)
            last_beep_time = beep_timer.time()

    elif obstacle_warned and nearest_distance > OBSTACLE_CLEAR_MM:
        obstacle_warned = False
        obstacle_warn_side = ""
        send({"type": "alert", "kind": "clear"})

    if front_close and back_close:
        left_motor.hold()
        right_motor.hold()
        return True

    elif front_close:
        left_motor.dc(REVERSE_POWER)
        right_motor.dc(REVERSE_POWER)
        return True

    elif back_close:
        left_motor.dc(FORWARD_POWER)
        right_motor.dc(FORWARD_POWER)
        return True

    return False


# ==================================================
# 10. ENGINE SOUND
# ==================================================

def update_engine_sound(rt):
    global engine_on, rumble_flip

    if rt > ENGINE_DEADZONE:
        rumble_flip = not rumble_flip

        base = ENGINE_MIN_FREQ + rt

        freq = int(
            base * (ENGINE_RUMBLE if rumble_flip else 1.0)
        )

        hub.speaker.volume(ENGINE_MIN_VOL + int(rt * 0.85))
        hub.speaker.beep(freq, -1)

        engine_on = True

    elif engine_on:
        hub.speaker.beep(ENGINE_MIN_FREQ, 1)
        hub.speaker.volume(100)
        engine_on = False


# ==================================================
# 11. MANUAL CONTROL
# ==================================================

def manual_control(buttons, lt, rt):
    power = lt - rt

    update_engine_sound(rt)

    horizontal, _ = controller.joystick_left()

    if abs(horizontal) < STICK_DEADZONE:
        horizontal = 0

    angle = int(horizontal * 0.4)

    if Button.Y in buttons:
        left_motor.hold()
        right_motor.hold()

    else:
        if Button.B in buttons or Button.RB in buttons:
            right_motor.brake()
        else:
            right_motor.dc(power)

        if Button.X in buttons or Button.LB in buttons:
            left_motor.brake()
        else:
            left_motor.dc(power)

    if Button.RB in buttons or Button.LB in buttons:
        steering_motor.brake()
    else:
        steering_motor.run_target(1000, STEERING_CENTER + angle, wait=False)


# ==================================================
# 12. AUTONOMOUS CONTROL
# ==================================================

def autonomous_control():
    steering_motor.run_target(
        1000,
        steering_angle,
        wait=False
    )

    if drive_power == 0:
        left_motor.brake()
        right_motor.brake()
    else:
        left_motor.dc(drive_power)
        right_motor.dc(drive_power)


# ==================================================
# 13. MAIN LOOP
# ==================================================

# Startup signal for the laptop link
hub.speaker.beep(1000, 100)
hub.light.on(Color.GREEN)
send({"type": "ready"})


while True:

    # 1. Read JSON lines from the laptop (heartbeat + commands)
    fetched = True
    while fetched:
        byte = read_input_byte()
        if byte is None:
            fetched = False
        elif byte == 10 or byte == 13:
            if input_line:
                handle_line(input_line)
                input_line = ""
        elif len(input_line) < LINE_MAX_BYTES:
            input_line += chr(byte)

    # 2. Timers and link safety (also while avoiding obstacles)
    update_action()
    check_watchdog()

    # 3. Controller input (optional hardware; manual mode only)
    if controller is not None:
        buttons = controller.buttons.pressed()
        lt, rt = controller.triggers()
        if Button.MENU in buttons:
            toggle_mode()
            wait(300)
    else:
        buttons = ()
        lt = rt = 0

    # Update sensors in both modes
    update_sensors()

    # Obstacle avoidance always has priority
    if obstacle_avoidance():
        wait(20)
        continue

    # Execute the selected control mode
    if current_mode == "manual" and controller is not None:
        manual_control(buttons, lt, rt)

    elif current_mode == "autonomous":
        autonomous_control()

    # Xbox A: horn
    a_is_pressed = Button.A in buttons

    if a_is_pressed and not a_was_pressed:
        honk()

    a_was_pressed = a_is_pressed

    wait(20)
