"""Press each button / move D-pad. Prints index + hat values. Ctrl-C to quit."""
import pygame, time, sys

pygame.init()
pygame.joystick.init()
if pygame.joystick.get_count() == 0:
    sys.exit("No gamepad detected.")

j = pygame.joystick.Joystick(0); j.init()
print(f"Pad: {j.get_name()}  buttons={j.get_numbuttons()}  hats={j.get_numhats()}  axes={j.get_numaxes()}")
print("Press each button. Move D-pad. Ctrl-C to quit.\n")

last_btns = [0] * j.get_numbuttons()
last_hat  = (0, 0)

try:
    while True:
        pygame.event.pump()
        for i in range(j.get_numbuttons()):
            v = j.get_button(i)
            if v and not last_btns[i]:
                print(f"button {i:>2} pressed")
            last_btns[i] = v
        if j.get_numhats() > 0:
            h = j.get_hat(0)
            if h != last_hat and h != (0, 0):
                print(f"hat {h}")
            last_hat = h
        time.sleep(0.05)
except KeyboardInterrupt:
    pass
